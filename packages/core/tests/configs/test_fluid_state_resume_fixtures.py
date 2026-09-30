"""Offline contract tests for the persistent fluid-state fixture bundle."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path
import sqlite3

import pytest
import yaml

from cubos.data import DataStore
from cubos.deck.loader import load_deck_from_yaml_safe
from cubos.protocol_engine.setup import run_on_hardware
from cubos.protocol_engine.setup_validator import run_setup_validation


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/configs/fluid_state_resume"
PROTOCOL_PATHS = (
    FIXTURES / "01_load_plate.yaml",
    FIXTURES / "02_add_dye.yaml",
)


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _commands(document: dict, command_name: str) -> list[dict]:
    return [
        step[command_name]
        for step in document["protocol"]
        if command_name in step
    ]


@pytest.mark.parametrize("protocol_path", PROTOCOL_PATHS, ids=lambda path: path.stem)
def test_fluid_state_resume_fixture_triples_validate_offline(protocol_path: Path):
    result = run_setup_validation(
        FIXTURES / "gantry.yaml",
        FIXTURES / "deck.yaml",
        protocol_path,
    )

    assert result.passed, result.output
    assert "offline/mock - hardware not contacted" in result.output


def test_initial_fluids_seed_has_the_expected_shape():
    seed = _load_yaml(FIXTURES / "initial_fluids.yaml")

    assert seed == {
        "fluids": {
            "reagents.buffer": {
                "volume_ul": 1000.0,
                "composition": {"buffer": 1000.0},
            },
            "reagents.dye": {
                "volume_ul": 200.0,
                "composition": {"dye": 200.0},
            },
        }
    }
    for fluid in seed["fluids"].values():
        assert fluid["volume_ul"] == pytest.approx(
            sum(fluid["composition"].values())
        )


def test_deck_uses_named_flat_labware_and_canonical_vial_positions():
    document = _load_yaml(FIXTURES / "deck.yaml")
    assert document["labware"]["reagents"] == {
        "load_name": "ursa_9_vial_grid",
        "label": "Reagents",
        "calibration": {
            "a1": {"x": 140.0, "y": 15.0, "z": 30.0},
            "a2": {"x": 140.0, "y": 48.0, "z": 30.0},
        },
        "aliases": {"buffer": "A1", "dye": "A2"},
    }
    assert document["labware"]["assay_plate"]["load_name"] == (
        "sbs_96_wellplate"
    )
    assert document["labware"]["assay_plate"]["label"] == "Assay plate"

    deck = load_deck_from_yaml_safe(FIXTURES / "deck.yaml")
    assert deck.canonicalize_target("reagents.buffer") == "reagents.A1"
    assert deck.canonicalize_target("reagents.dye") == "reagents.A2"
    assert deck.resolve_coordinate("reagents.A1").x == pytest.approx(140.0)
    assert deck.resolve_coordinate("reagents.A1").y == pytest.approx(15.0)
    assert deck.resolve_coordinate("reagents.A2").x == pytest.approx(140.0)
    assert deck.resolve_coordinate("reagents.A2").y == pytest.approx(48.0)


def test_protocols_encode_distinct_tips_and_expected_fluid_math():
    documents = [_load_yaml(path) for path in PROTOCOL_PATHS]
    transfers = [
        transfer
        for document in documents
        for transfer in _commands(document, "transfer")
    ]
    tip_slots = [
        pickup["position"]
        for document in documents
        for pickup in _commands(document, "pick_up_tip")
    ]
    mixes = [
        mix
        for document in documents
        for mix in _commands(document, "mix")
    ]

    assert [
        (item["source"], item["destination"], item["volume_ul"])
        for item in transfers
    ] == [
        ("reagents.buffer", "assay_plate.A1", 100.0),
        ("reagents.buffer", "assay_plate.A2", 80.0),
        ("reagents.dye", "assay_plate.A1", 20.0),
        ("reagents.dye", "assay_plate.A2", 20.0),
    ]
    assert tip_slots == [
        "tip_rack.A1",
        "tip_rack.A2",
        "tip_rack.B1",
        "tip_rack.B2",
    ]
    assert len(tip_slots) == len(set(tip_slots))
    assert [
        (item["position"], item["volume_ul"], item["cycles"])
        for item in mixes
    ] == [
        ("assay_plate.A1", 60.0, 3),
        ("assay_plate.A2", 50.0, 3),
    ]

    seed = _load_yaml(FIXTURES / "initial_fluids.yaml")["fluids"]
    state = {
        location: deepcopy(details["composition"])
        for location, details in seed.items()
    }
    state.update({"assay_plate.A1": {}, "assay_plate.A2": {}})

    for transfer in transfers:
        source = state[transfer["source"]]
        destination = state[transfer["destination"]]
        volume_ul = transfer["volume_ul"]
        source_total = sum(source.values())
        assert source_total >= volume_ul

        for component, component_volume in list(source.items()):
            moved = volume_ul * component_volume / source_total
            source[component] -= moved
            destination[component] = destination.get(component, 0.0) + moved

    expected = {
        "reagents.buffer": {"buffer": 820.0},
        "reagents.dye": {"dye": 160.0},
        "assay_plate.A1": {"buffer": 100.0, "dye": 20.0},
        "assay_plate.A2": {"buffer": 80.0, "dye": 20.0},
    }
    assert state.keys() == expected.keys()
    for location, composition in expected.items():
        assert state[location] == pytest.approx(composition)

    assert {
        location: sum(composition.values())
        for location, composition in state.items()
    } == pytest.approx({
        "reagents.buffer": 820.0,
        "reagents.dye": 160.0,
        "assay_plate.A1": 120.0,
        "assay_plate.A2": 100.0,
    })


def test_two_mock_runs_persist_and_resume_one_fluid_state(tmp_path: Path):
    db_path = tmp_path / "fluid-state-resume.db"
    first_store = DataStore(db_path)
    try:
        first_results = run_on_hardware(
            FIXTURES / "gantry.yaml",
            FIXTURES / "deck.yaml",
            PROTOCOL_PATHS[0],
            mock_mode=True,
            data_store=first_store,
            initial_fluids=FIXTURES / "initial_fluids.yaml",
        )
    finally:
        first_store.close()

    with sqlite3.connect(db_path) as connection:
        state_ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM fluid_state_sessions ORDER BY id"
            )
        ]
    assert len(state_ids) == 1
    state_id = state_ids[0]

    resumed_store = DataStore(db_path)
    try:
        second_results = run_on_hardware(
            FIXTURES / "gantry.yaml",
            FIXTURES / "deck.yaml",
            PROTOCOL_PATHS[1],
            mock_mode=True,
            data_store=resumed_store,
            fluid_state_id=state_id,
        )
        snapshot = resumed_store.get_fluid_snapshot(state_id)
    finally:
        resumed_store.close()

    assert len(first_results) == 10
    assert len(second_results) == 12
    containers = {
        (
            container["labware_key"],
            container["location_id"],
        ): container
        for container in snapshot["containers"]
    }
    expected = {
        ("reagents", "A1"): (820.0, {"buffer": 820.0}),
        ("reagents", "A2"): (160.0, {"dye": 160.0}),
        ("assay_plate", "A1"): (
            120.0,
            {"buffer": 100.0, "dye": 20.0},
        ),
        ("assay_plate", "A2"): (
            100.0,
            {"buffer": 80.0, "dye": 20.0},
        ),
    }
    for target, (volume_ul, composition) in expected.items():
        assert containers[target]["current_volume_ul"] == pytest.approx(volume_ul)
        assert containers[target]["composition"] == pytest.approx(composition)

    assert len(snapshot["operations"]) == 6
    assert {operation["status"] for operation in snapshot["operations"]} == {
        "applied"
    }
    assert Counter(
        operation["operation_type"] for operation in snapshot["operations"]
    ) == {"transfer": 4, "mix": 2}
    transfers = [
        operation
        for operation in snapshot["operations"]
        if operation["operation_type"] == "transfer"
    ]
    assert {
        operation["source"] for operation in transfers
    } == {"reagents.A1", "reagents.A2"}
    assert {
        operation["destination"] for operation in transfers
    } == {"assay_plate.A1", "assay_plate.A2"}
    assert {
        (operation["source"], operation["destination"])
        for operation in snapshot["operations"]
        if operation["operation_type"] == "mix"
    } == {
        ("assay_plate.A1", "assay_plate.A1"),
        ("assay_plate.A2", "assay_plate.A2"),
    }

    with sqlite3.connect(db_path) as connection:
        campaign_rows = connection.execute(
            "SELECT fluid_state_id, status FROM campaigns ORDER BY id"
        ).fetchall()
    assert campaign_rows == [(state_id, "completed"), (state_id, "completed")]


def test_two_mock_runs_persist_tip_state_and_never_repick_a_consumed_tip(
    tmp_path: Path,
):
    """Durable per-slot tip state, seeded from this same fixture bundle.

    01_load_plate.yaml picks tip_rack.A1 then tip_rack.A2 (dropping each
    before the next pickup); 02_add_dye.yaml picks B1 then B2. A third,
    ad-hoc run against the same DB that tries to re-pick tip_rack.A1 must be
    refused by durable state even though a freshly loaded deck still reports
    every tip as present in memory.
    """
    db_path = tmp_path / "fluid-state-tip-repick.db"
    from cubos.protocol_engine.errors import ProtocolExecutionError

    first_store = DataStore(db_path)
    try:
        run_on_hardware(
            FIXTURES / "gantry.yaml",
            FIXTURES / "deck.yaml",
            PROTOCOL_PATHS[0],
            mock_mode=True,
            data_store=first_store,
            initial_fluids=FIXTURES / "initial_fluids.yaml",
        )
    finally:
        first_store.close()

    with sqlite3.connect(db_path) as connection:
        state_id = connection.execute(
            "SELECT id FROM fluid_state_sessions ORDER BY id"
        ).fetchone()[0]
        tip_status = dict(
            connection.execute(
                "SELECT slot_id, status FROM tip_containers "
                "WHERE fluid_state_id = ? AND rack_key = 'tip_rack'",
                (state_id,),
            ).fetchall()
        )
        operation_types = connection.execute(
            "SELECT operation_type, status FROM tip_operations "
            "WHERE fluid_state_id = ? ORDER BY id",
            (state_id,),
        ).fetchall()
    assert tip_status["A1"] == "consumed"
    assert tip_status["A2"] == "consumed"
    assert tip_status["B1"] == "available"
    assert operation_types == [
        ("pick_up_tip", "applied"),
        ("drop_tip", "applied"),
        ("pick_up_tip", "applied"),
        ("drop_tip", "applied"),
    ]

    second_store = DataStore(db_path)
    try:
        run_on_hardware(
            FIXTURES / "gantry.yaml",
            FIXTURES / "deck.yaml",
            PROTOCOL_PATHS[1],
            mock_mode=True,
            data_store=second_store,
            fluid_state_id=state_id,
        )
    finally:
        second_store.close()

    repick_protocol = tmp_path / "repick_a1.yaml"
    repick_protocol.write_text(
        "protocol:\n"
        "  - home:\n"
        "  - pick_up_tip:\n"
        "      position: tip_rack.A1\n",
        encoding="utf-8",
    )

    # A fresh deck load reports every tip present in memory; only the
    # durable DB state must gate a re-pick.
    fresh_deck = load_deck_from_yaml_safe(FIXTURES / "deck.yaml")
    assert fresh_deck.labware["tip_rack"].is_tip_present("A1") is True

    third_store = DataStore(db_path)
    try:
        with pytest.raises(ProtocolExecutionError, match="not available"):
            run_on_hardware(
                FIXTURES / "gantry.yaml",
                FIXTURES / "deck.yaml",
                repick_protocol,
                mock_mode=True,
                data_store=third_store,
                fluid_state_id=state_id,
            )
    finally:
        third_store.close()

    with sqlite3.connect(db_path) as connection:
        final_status = connection.execute(
            "SELECT status FROM tip_containers WHERE fluid_state_id = ? "
            "AND rack_key = 'tip_rack' AND slot_id = 'A1'",
            (state_id,),
        ).fetchone()[0]
    assert final_status == "consumed"
