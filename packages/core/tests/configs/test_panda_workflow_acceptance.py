"""Golden offline traces for the Feature 08 PANDA workflow cutover.

Runs the two committed PANDA acceptance protocols end-to-end in mock mode
through the real protocol engine + a real ``DataStore`` (SQLite in
``tmp_path``), and asserts the EXACT ordered durable operation journal and
the EXACT final fluid/tip state -- volumes, compositions, consumed tip
slots, no attached tip, campaign completion.

Uses the ``*_pipetting_mock`` gantry/deck pair, not the generated
``cub_xl_panda_home_origin_full.yaml`` + ``panda_imported_deck.yaml`` trio:
those imported configs cannot pass bounds validation for any real
``pipette``-instrument motion (safe_z -5.0 + pipette depth 100.0 already
exceeds working_volume z_max=0.0 before any deck coordinate is involved).
See ``configs/gantry/cub_xl_panda_pipetting_mock.yaml``'s header for the
full documented blocker. The mock pair keeps the imported labware
identities, roles, solutions, and capacities byte-identical, so fluid/tip
tracking below exercises the real production-imported data; only motion
coordinates differ.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cubos.data import DataStore, create_campaign_for_protocol_run
from cubos.protocol_engine.setup import run_on_hardware
from cubos.protocol_engine.setup_validator import run_setup_validation

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / "configs"
GANTRY = CONFIGS / "gantry" / "cub_xl_panda_pipetting_mock.yaml"
DECK = CONFIGS / "deck" / "panda_imported_deck_pipetting_mock.yaml"
INITIAL_FLUIDS = CONFIGS / "deck" / "panda_initial_fluids.yaml"
ACCEPTANCE_PROTOCOL = CONFIGS / "protocol" / "panda" / "vial_transfer_acceptance.yaml"
WELL_RINSE_PROTOCOL = CONFIGS / "protocol" / "panda" / "well_rinse_water.yaml"

# The production-imported seed (panda_initial_fluids.yaml), by container.
_SEED = {
    ("e1", ""): (15000.0, {"ebath": 15000.0}),
    ("s1", ""): (6500.0, {"water": 6500.0}),
    ("s2", ""): (2500.0, {"dmfc": 2500.0}),
    ("s3", ""): (4242.0, {"electrolyte": 4242.0}),
    ("s4", ""): (0.0, {}),
    ("s5", ""): (14400.0, {"dmf": 14400.0}),
    ("s6", ""): (2100.0, {"acn": 2100.0}),
    ("w1", ""): (3600.0, {"dmfc": 3600.0}),
    ("w2", ""): (19800.0, {"acn": 6300.0, "dmf": 13500.0}),
    ("w3", ""): (11700.0, {"acn": 3600.0, "dmf": 8100.0}),
}

_PLATE_WELLS = [
    f"{row}{column}" for row in "ABC" for column in range(1, 5)
]


def _expected_containers(overrides: dict) -> dict:
    """Full expected final-state table: seed + 12 empty wells + overrides."""
    expected = dict(_SEED)
    for well_id in _PLATE_WELLS:
        expected[("ito_pama_plate", well_id)] = (0.0, {})
    expected.update(overrides)
    return expected


def _run(protocol: Path, db_path: Path, **kwargs) -> tuple[list, int, int]:
    """Run one protocol in mock mode; return (results, campaign_id, state_id)."""
    store = DataStore(db_path)
    try:
        results = run_on_hardware(
            GANTRY, DECK, protocol,
            mock_mode=True,
            data_store=store,
            **kwargs,
        )
        with sqlite3.connect(db_path) as connection:
            campaign_id, state_id = connection.execute(
                "SELECT id, fluid_state_id FROM campaigns ORDER BY id DESC LIMIT 1"
            ).fetchone()
    finally:
        store.close()
    return results, int(campaign_id), int(state_id)


def _assert_containers(db_path: Path, state_id: int, expected: dict) -> None:
    store = DataStore(db_path)
    try:
        snapshot = store.get_fluid_snapshot(state_id)
    finally:
        store.close()
    actual = {
        (container["labware_key"], container["location_id"]): container
        for container in snapshot["containers"]
    }
    assert set(actual) == set(expected)
    for target, (volume_ul, composition) in expected.items():
        assert actual[target]["current_volume_ul"] == pytest.approx(volume_ul), target
        assert actual[target]["composition"] == pytest.approx(composition), target


def _fluid_trace(db_path: Path, state_id: int) -> list[tuple]:
    store = DataStore(db_path)
    try:
        snapshot = store.get_fluid_snapshot(state_id)
    finally:
        store.close()
    return [
        (
            operation["operation_key"],
            operation["operation_type"],
            operation["source"],
            operation["destination"],
            operation["volume_ul"],
            operation["status"],
        )
        for operation in snapshot["operations"]
    ]


def _tip_state(db_path: Path, state_id: int) -> tuple[list[tuple], dict, dict]:
    """Return (ordered tip-operation trace, consumed slots, pipette attachment)."""
    store = DataStore(db_path)
    try:
        snapshot = store.get_tip_snapshot(state_id)
    finally:
        store.close()
    trace = [
        (op["operation_key"], op["operation_type"], op["rack_key"], op["slot_id"], op["status"])
        for op in snapshot["operations"]
    ]
    consumed = {
        (c["rack_key"], c["slot_id"]): c["status"]
        for c in snapshot["containers"]
        if c["status"] != "available"
    }
    return trace, consumed, snapshot["pipette"]


@pytest.mark.parametrize(
    "protocol", [ACCEPTANCE_PROTOCOL, WELL_RINSE_PROTOCOL], ids=lambda p: p.stem,
)
def test_panda_protocols_validate_offline_with_the_generated_fluid_seed(protocol):
    result = run_setup_validation(GANTRY, DECK, protocol, INITIAL_FLUIDS)
    assert result.passed, result.output
    assert "offline/mock - hardware not contacted" in result.output


def test_vial_transfer_acceptance_golden_trace(tmp_path):
    """Tip pickup -> 200uL s1->w1 -> 400uL s1->w3 (2 strokes) -> drop tip."""
    db_path = tmp_path / "acceptance.db"
    results, cid, state_id = _run(
        ACCEPTANCE_PROTOCOL, db_path, initial_fluids=INITIAL_FLUIDS,
    )
    assert len(results) == 6  # home, pick_up_tip, transfer, transfer, drop_tip, home

    # Exact ordered fluid journal: the 400 uL transfer must split into
    # exactly two 200 uL strokes (p300_single_gen2 max_volume=200), each its
    # own applied operation.
    assert _fluid_trace(db_path, state_id) == [
        (
            f"campaign:{cid}:step:2:transfer:transfer",
            "transfer", "s1", "w1", 200.0, "applied",
        ),
        (
            f"campaign:{cid}:step:3:transfer:substep:stroke0:transfer",
            "transfer", "s1", "w3", 200.0, "applied",
        ),
        (
            f"campaign:{cid}:step:3:transfer:substep:stroke1:transfer",
            "transfer", "s1", "w3", 200.0, "applied",
        ),
    ]

    # Exact final volumes and compositions for every registered container.
    _assert_containers(db_path, state_id, _expected_containers({
        ("s1", ""): (5900.0, {"water": 5900.0}),
        ("w1", ""): (3800.0, {"dmfc": 3600.0, "water": 200.0}),
        ("w3", ""): (12100.0, {"acn": 3600.0, "dmf": 8100.0, "water": 400.0}),
    }))

    # Exact tip journal and final tip state: A1 consumed, nothing attached.
    tip_trace, consumed, pipette = _tip_state(db_path, state_id)
    assert tip_trace == [
        (
            f"campaign:{cid}:step:1:pick_up_tip:pick_up_tip",
            "pick_up_tip", "tip_rack", "A1", "applied",
        ),
        (
            f"campaign:{cid}:step:4:drop_tip:drop_tip",
            "drop_tip", "tip_rack", "A1", "applied",
        ),
    ]
    assert consumed == {("tip_rack", "A1"): "consumed"}
    assert pipette["rack_key"] is None
    assert pipette["slot_id"] is None
    assert pipette["attachment_uncertain"] is False

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status FROM campaigns WHERE id = ?", (cid,),
        ).fetchone()[0] == "completed"


def test_well_rinse_water_golden_trace(tmp_path):
    """3x rinse (auto stock=s1 / auto waste=w1) -> flush -> clear (no-op)."""
    db_path = tmp_path / "well-rinse.db"
    results, cid, state_id = _run(
        WELL_RINSE_PROTOCOL, db_path, initial_fluids=INITIAL_FLUIDS,
    )
    assert len(results) == 7

    # Exact ordered journal. Automatic selection resolves solution=water to
    # s1 (the only role=stock/solution=water vial) and waste to w1 (first in
    # sorted labware-key order among w1/w2/w3 with headroom) -- both choices
    # are part of the durable record via source/destination.
    step2 = f"campaign:{cid}:step:2:rinse_well:substep"
    assert _fluid_trace(db_path, state_id) == [
        (f"{step2}:rinse:cycle0:fill:transfer", "transfer",
         "s1", "ito_pama_plate.A1", 200.0, "applied"),
        (f"{step2}:rinse:cycle0:remove:transfer", "transfer",
         "ito_pama_plate.A1", "w1", 200.0, "applied"),
        (f"{step2}:rinse:cycle1:fill:transfer", "transfer",
         "s1", "ito_pama_plate.A1", 200.0, "applied"),
        (f"{step2}:rinse:cycle1:remove:transfer", "transfer",
         "ito_pama_plate.A1", "w1", 200.0, "applied"),
        (f"{step2}:rinse:cycle2:fill:transfer", "transfer",
         "s1", "ito_pama_plate.A1", 200.0, "applied"),
        (f"{step2}:rinse:cycle2:remove:transfer", "transfer",
         "ito_pama_plate.A1", "w1", 200.0, "applied"),
        (f"campaign:{cid}:step:3:flush_pipette:substep:flush:cycle0:transfer",
         "transfer", "s1", "w1", 200.0, "applied"),
        # clear_well (step 4) journals nothing: the final rinse removal
        # already drained A1 to exactly 0 uL, so it is a documented no-op.
    ]

    # 3 fills leave and re-enter A1; 800 uL of water total leaves s1
    # (3 x 200 rinse + 200 flush), and all of it ends in w1.
    _assert_containers(db_path, state_id, _expected_containers({
        ("s1", ""): (5700.0, {"water": 5700.0}),
        ("w1", ""): (4400.0, {"dmfc": 3600.0, "water": 800.0}),
    }))

    tip_trace, consumed, pipette = _tip_state(db_path, state_id)
    assert tip_trace == [
        (
            f"campaign:{cid}:step:1:pick_up_tip:pick_up_tip",
            "pick_up_tip", "tip_rack", "A2", "applied",
        ),
        (
            f"campaign:{cid}:step:5:drop_tip:drop_tip",
            "drop_tip", "tip_rack", "A2", "applied",
        ),
    ]
    assert consumed == {("tip_rack", "A2"): "consumed"}
    assert pipette["rack_key"] is None
    assert pipette["slot_id"] is None

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status FROM campaigns WHERE id = ?", (cid,),
        ).fetchone()[0] == "completed"


def test_restart_after_first_transfer_rejects_replay_without_repeating_liquid(
    tmp_path, monkeypatch,
):
    """A consumed pickup target blocks replay of a partially applied protocol."""
    from cubos.validation.errors import ProtocolSemanticValidationError

    from cubos.protocol_engine.commands import pipette as pipette_commands

    db_path = tmp_path / "restart.db"

    store = DataStore(db_path)
    try:
        cid = create_campaign_for_protocol_run(
            store,
            gantry_path=GANTRY,
            deck_path=DECK,
            gantry_file=str(GANTRY),
            deck_file=str(DECK),
            protocol_file=str(ACCEPTANCE_PROTOCOL),
            description="restart golden trace",
        )

        real_plan_strokes = pipette_commands.plan_strokes

        def crashing_plan_strokes(volume_ul, capacity, correction=None):
            if volume_ul == 400.0:
                raise RuntimeError("injected crash before the second transfer")
            return real_plan_strokes(volume_ul, capacity, correction)

        monkeypatch.setattr(
            pipette_commands, "plan_strokes", crashing_plan_strokes,
        )
        with pytest.raises(Exception, match="injected crash"):
            run_on_hardware(
                GANTRY, DECK, ACCEPTANCE_PROTOCOL,
                mock_mode=True,
                data_store=store,
                campaign_id=cid,
                initial_fluids=INITIAL_FLUIDS,
            )
        monkeypatch.setattr(pipette_commands, "plan_strokes", real_plan_strokes)

        state_id = store.get_campaign_fluid_state_id(cid)
        assert state_id is not None
    finally:
        store.close()

    # Mid-crash durable state: first transfer applied once, tip attached.
    trace = _fluid_trace(db_path, state_id)
    assert trace == [
        (
            f"campaign:{cid}:step:2:transfer:transfer",
            "transfer", "s1", "w1", 200.0, "applied",
        ),
    ]
    _, consumed, pipette = _tip_state(db_path, state_id)
    assert consumed == {("tip_rack", "A1"): "attached"}
    assert pipette["rack_key"] == "tip_rack"
    assert pipette["slot_id"] == "A1"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status FROM campaigns WHERE id = ?", (cid,),
        ).fetchone()[0] == "failed"

    resumed = DataStore(db_path)
    try:
        fluid_before = resumed.get_fluid_snapshot(state_id)
        tips_before = resumed.get_tip_snapshot(state_id)
        with pytest.raises(ProtocolSemanticValidationError, match="tip_rack.A1.*not available"):
            run_on_hardware(
                GANTRY, DECK, ACCEPTANCE_PROTOCOL,
                mock_mode=True,
                data_store=resumed,
                campaign_id=cid,
            )
        assert resumed.get_fluid_snapshot(state_id) == fluid_before
        assert resumed.get_tip_snapshot(state_id) == tips_before
    finally:
        resumed.close()

    assert _fluid_trace(db_path, state_id) == trace
    _assert_containers(db_path, state_id, _expected_containers({
        ("s1", ""): (6300.0, {"water": 6300.0}),
        ("w1", ""): (3800.0, {"dmfc": 3600.0, "water": 200.0}),
    }))
    tip_trace, consumed, pipette = _tip_state(db_path, state_id)
    assert tip_trace == [
        (
            f"campaign:{cid}:step:1:pick_up_tip:pick_up_tip",
            "pick_up_tip", "tip_rack", "A1", "applied",
        ),
    ]
    assert consumed == {("tip_rack", "A1"): "attached"}
    assert pipette["rack_key"] == "tip_rack"
    assert pipette["slot_id"] == "A1"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status FROM campaigns WHERE id = ?", (cid,),
        ).fetchone()[0] == "failed"
