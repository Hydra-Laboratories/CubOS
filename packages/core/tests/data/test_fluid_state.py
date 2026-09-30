"""Focused tests for durable deck-associated fluid state."""

from __future__ import annotations

import sqlite3

import pytest

from cubos.data import (
    DataStore,
    FLUID_STATE_API_VERSION,
    FluidStateReader,
    FluidStateDeckMismatchError,
    FluidStateError,
    FluidStateReconciliationRequiredError,
    load_initial_fluids,
    load_replacement_state,
)
from cubos.deck.deck import DeckLabwareTarget
from cubos.deck.loader import load_deck_from_yaml_safe


DECK_YAML = """\
labware:
  source:
    type: vial
    name: source
    model_name: source_vial
    height: 50.0
    diameter: 20.0
    location: {x: 5.0, y: 5.0, z: 20.0}
    capacity_ul: 500.0
    working_volume_ul: 400.0

  plate:
    type: well_plate
    name: destination_plate
    model_name: two_well_plate
    length: 30.0
    width: 20.0
    height: 14.0
    well_depth: 10.0
    rows: 1
    columns: 2
    calibration:
      a1: {x: 20.0, y: 20.0, z: 15.0}
      a2: {x: 29.0, y: 20.0, z: 15.0}
    x_offset: 9.0
    y_offset: 9.0
    capacity_ul: 200.0
    working_volume_ul: 150.0

  holder:
    type: vial_holder
    name: nested_holder
    location: {x: 100.0, y: 100.0, z: 20.0}
    vials:
      nested:
        name: nested
        model_name: nested_vial
        height: 40.0
        diameter: 15.0
        location: {x: 0.0, y: 0.0}
        capacity_ul: 250.0
        working_volume_ul: 200.0
      reserve:
        name: reserve
        model_name: reserve_vial
        height: 45.0
        diameter: 18.0
        location: {x: 20.0, y: 0.0}
        capacity_ul: 600.0
        working_volume_ul: 500.0

  untracked:
    type: well_plate
    name: geometry_only_plate
    rows: 1
    columns: 1
    calibration:
      a1: {x: 60.0, y: 20.0, z: 15.0}
      a2: {x: 69.0, y: 20.0, z: 15.0}
    x_offset: 9.0
    y_offset: 9.0
"""


VIAL_GRID_YAML = """\
labware:
  reagents:
    type: vial_grid
    name: reagents
    model_name: reagent_grid
    rows: 1
    columns: 2
    calibration:
      a1: {x: 10.0, y: 20.0, z: 30.0}
      a2: {x: 20.0, y: 20.0, z: 30.0}
    x_offset: 10.0
    y_offset: 10.0
    vial_model_name: reagent_vial
    vial_height: 40.0
    vial_diameter: 12.0
    capacity_ul: 500.0
    working_volume_ul: 400.0
    aliases:
      buffer: A1
      product: A2
"""


def _write_deck(tmp_path, text: str = DECK_YAML):
    path = tmp_path / "deck.yaml"
    path.write_text(text, encoding="utf-8")
    return path, load_deck_from_yaml_safe(path)


def _container(snapshot, target: str):
    exact = next(
        (
            container
            for container in snapshot["containers"]
            if container["labware_key"] == target
            and container["location_id"] == ""
        ),
        None,
    )
    if exact is not None:
        return exact
    if "." in target:
        labware_key, location_id = target.rsplit(".", 1)
    else:
        labware_key, location_id = target, ""
    return next(
        container
        for container in snapshot["containers"]
        if container["labware_key"] == labware_key
        and container["location_id"] == location_id
    )


def _create_seeded_state(store, deck_path, deck):
    return store.create_fluid_state(
        deck_path,
        deck,
        label="mixture",
        initial_fluids={
            "source": {
                "volume_ul": 100.0,
                "composition": {"water": 60.0, "ethanol": 40.0},
            }
        },
    )


def _create_linked_campaign(store, state_id, description="fluid test"):
    return store.create_campaign(description, fluid_state_id=state_id)


def _database_schema(path):
    connection = sqlite3.connect(path)
    try:
        objects = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "ORDER BY type, name"
        ).fetchall()
        version = connection.execute("PRAGMA schema_version").fetchone()[0]
        return objects, version
    finally:
        connection.close()


def test_fluid_state_public_api_version_is_one():
    assert FLUID_STATE_API_VERSION == 1


def test_create_snapshot_links_deck_geometry_and_campaign(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")

    state_id = store.create_fluid_state(
        deck_path,
        deck,
        label="demo",
        initial_fluids={
            "source": {"volume_ul": 75.0},
            "holder.nested": {"volume_ul": 25.0},
        },
    )
    campaign_id = store.create_campaign(
        "fluid demo", deck_config=str(deck_path), fluid_state_id=state_id,
    )
    snapshot = store.get_fluid_snapshot(state_id)

    assert snapshot["label"] == "demo"
    assert snapshot["deck_path"] == str(deck_path.resolve())
    assert len(snapshot["deck_fingerprint"]) == 64
    assert snapshot["deck_snapshot"]["labware"]["source"]["type"] == "vial"
    assert [entry["labware_key"] for entry in snapshot["layout"]["containers"]] == [
        "holder__vials",
        "plate",
        "source",
    ]
    holder_layout = next(
        item for item in snapshot["layout"]["containers"]
        if item["labware_key"] == "holder__vials"
    )
    assert holder_layout["labware_type"] == "vial_grid"
    assert [location["location_id"] for location in holder_layout["locations"]] == [
        "nested",
        "reserve",
    ]
    plate_layout = next(
        item for item in snapshot["layout"]["containers"]
        if item["labware_key"] == "plate"
    )
    assert plate_layout["geometry"] == {
        "diameter": None,
        "height": 14.0,
        "length": 30.0,
        "well_depth": 10.0,
        "width": 20.0,
    }
    assert plate_layout["locations"] == [
        {"location_id": "A1", "x": 20.0, "y": 20.0, "z": 15.0},
        {"location_id": "A2", "x": 29.0, "y": 20.0, "z": 15.0},
    ]
    assert _container(snapshot, "source")["composition"] == {"unknown": 75.0}
    nested = _container(snapshot, "holder__vials.nested")
    reserve = _container(snapshot, "holder__vials.reserve")
    assert nested["composition"] == {
        "unknown": 25.0
    }
    assert (nested["capacity_ul"], nested["working_volume_ul"]) == (250.0, 200.0)
    assert (reserve["capacity_ul"], reserve["working_volume_ul"]) == (600.0, 500.0)
    assert not any(
        container["labware_key"] == "holder.nested"
        for container in snapshot["containers"]
    )
    assert all(
        container["labware_key"] != "untracked"
        for container in snapshot["containers"]
    )
    assert store._conn.execute(
        "SELECT fluid_state_id FROM campaigns WHERE id = ?", (campaign_id,)
    ).fetchone()[0] == state_id
    store.close()


def test_list_fluid_states_is_latest_first_counted_and_transactional(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    older_id = _create_seeded_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, older_id)
    store.begin_fluid_transfer(
        older_id,
        "summary-transfer",
        "source",
        "plate.A1",
        10.0,
        campaign_id=campaign_id,
    )
    store.complete_fluid_transfer("summary-transfer")
    newer_id = store.create_fluid_state(deck_path, deck, label="newest")
    older_snapshot = store.get_fluid_snapshot(older_id)

    statements = []
    store._conn.set_trace_callback(statements.append)
    summaries = store.list_fluid_states()
    store._conn.set_trace_callback(None)

    assert [summary["id"] for summary in summaries] == [newer_id, older_id]
    assert set(summaries[0]) == {
        "id",
        "label",
        "deck_path",
        "deck_fingerprint",
        "created_at",
        "updated_at",
        "container_count",
        "operation_count",
    }
    assert summaries[0]["label"] == "newest"
    assert summaries[0]["deck_path"] == str(deck_path.resolve())
    assert summaries[0]["deck_fingerprint"] == older_snapshot["deck_fingerprint"]
    assert summaries[0]["created_at"]
    assert summaries[0]["updated_at"]
    assert summaries[0]["container_count"] == 5
    assert summaries[0]["operation_count"] == 0
    assert summaries[1]["label"] == "mixture"
    assert summaries[1]["container_count"] == 5
    assert summaries[1]["operation_count"] == 1
    assert older_snapshot["operations"][0]["id"] > 0
    assert statements[0] == "BEGIN"
    assert statements[-1] == "ROLLBACK"
    store.close()


def test_fluid_state_reader_requires_existing_database(tmp_path):
    missing = tmp_path / "missing.db"

    with pytest.raises(sqlite3.OperationalError):
        FluidStateReader(missing)

    assert not missing.exists()


def test_fluid_state_reader_is_query_only_and_does_not_mutate_database(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    database = tmp_path / "reader.db"
    store = DataStore(database)
    state_id = _create_seeded_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)
    store.begin_fluid_transfer(
        state_id,
        "reader-transfer",
        "source",
        "plate.A1",
        10.0,
        campaign_id=campaign_id,
    )
    store.complete_fluid_transfer("reader-transfer")
    store.close()

    before_bytes = database.read_bytes()
    before_schema = _database_schema(database)
    with FluidStateReader(database) as reader:
        assert reader._conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert reader._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1000
        assert reader.list_fluid_states()[0]["id"] == state_id
        snapshot = reader.get_fluid_snapshot(state_id)
        assert snapshot["operations"][0]["id"] > 0
        assert snapshot["operations"][0]["operation_key"] == "reader-transfer"
        with pytest.raises(sqlite3.OperationalError):
            reader._conn.execute(
                "UPDATE fluid_state_sessions SET label = 'forbidden' WHERE id = ?",
                (state_id,),
            )

    assert database.read_bytes() == before_bytes
    assert _database_schema(database) == before_schema


def test_close_reopen_and_resume_with_canonical_same_deck(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    db_path = tmp_path / "state.db"
    store = DataStore(db_path)
    state_id = _create_seeded_state(store, deck_path, deck)
    original_fingerprint = store.get_fluid_snapshot(state_id)["deck_fingerprint"]
    store.close()

    # Comments and presentation do not change the canonical resolved config.
    deck_path.write_text("# operator note\n" + DECK_YAML, encoding="utf-8")
    resumed_deck = load_deck_from_yaml_safe(deck_path)
    reopened = DataStore(db_path)

    assert reopened.resume_fluid_state(state_id, deck_path, resumed_deck) == state_id
    assert reopened.get_fluid_snapshot(state_id)["deck_fingerprint"] == original_fingerprint
    assert _container(reopened.get_fluid_snapshot(state_id), "source")[
        "current_volume_ul"
    ] == 100.0
    reopened.close()


def test_resume_rejects_resolved_deck_mismatch(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = store.create_fluid_state(deck_path, deck)

    changed = DECK_YAML.replace("working_volume_ul: 400.0", "working_volume_ul: 399.0")
    deck_path.write_text(changed, encoding="utf-8")
    changed_deck = load_deck_from_yaml_safe(deck_path)

    with pytest.raises(FluidStateDeckMismatchError, match="fingerprint"):
        store.resume_fluid_state(state_id, deck_path, changed_deck)
    store.close()


def test_create_rejects_duplicate_keys_in_deck_provenance(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    deck_path.write_text(DECK_YAML + "\nlabware: {}\n", encoding="utf-8")
    store = DataStore(":memory:")

    with pytest.raises(FluidStateError, match="duplicate YAML key 'labware'"):
        store.create_fluid_state(deck_path, deck)

    assert store.list_fluid_states() == []
    store.close()


def test_load_initial_fluids_and_nested_target_seed(tmp_path):
    seed_path = tmp_path / "initial-fluids.yaml"
    seed_path.write_text(
        """\
fluids:
  holder.nested:
    volume_ul: 40.0
  plate.A2:
    volume_ul: 30.0
    composition:
      water: 10.0
      buffer: 20.0
""",
        encoding="utf-8",
    )
    initial = load_initial_fluids(seed_path)
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")

    state_id = store.create_fluid_state(
        deck_path, deck, initial_fluids=initial,
    )
    snapshot = store.get_fluid_snapshot(state_id)

    assert _container(snapshot, "holder__vials.nested")["composition"] == {
        "unknown": 40.0
    }
    assert _container(snapshot, "plate.A2")["composition"] == {
        "buffer": 20.0,
        "water": 10.0,
    }
    store.close()


def test_vial_grid_aliases_seed_only_canonical_position_rows(tmp_path):
    deck_path, deck = _write_deck(tmp_path, VIAL_GRID_YAML)
    store = DataStore(":memory:")

    state_id = store.create_fluid_state(
        deck_path,
        deck,
        initial_fluids={"reagents.buffer": {"volume_ul": 80.0}},
    )
    grid = deck.volume_labware["reagents"]
    store.seed_fluid(
        state_id,
        DeckLabwareTarget(
            labware_key="reagents",
            labware=grid,
            location_id="product",
        ),
        30.0,
    )

    snapshot = store.get_fluid_snapshot(state_id)
    assert [
        (container["labware_key"], container["location_id"])
        for container in snapshot["containers"]
    ] == [("reagents", "A1"), ("reagents", "A2")]
    assert _container(snapshot, "reagents.A1")["composition"] == {
        "unknown": 80.0
    }
    assert _container(snapshot, "reagents.A2")["composition"] == {
        "unknown": 30.0
    }
    assert all(
        container["capacity_ul"] == 500.0
        and container["working_volume_ul"] == 400.0
        and container["labware_type"] == "vial_grid"
        for container in snapshot["containers"]
    )
    layout = snapshot["layout"]["containers"][0]
    assert layout["labware_key"] == "reagents"
    assert [location["location_id"] for location in layout["locations"]] == [
        "A1",
        "A2",
    ]
    store.close()


def test_initial_fluids_reject_duplicate_aliases_before_creating_state(tmp_path):
    deck_path, deck = _write_deck(tmp_path, VIAL_GRID_YAML)
    store = DataStore(":memory:")

    with pytest.raises(
        FluidStateError,
        match=(
            "'reagents.buffer' and 'reagents.A1' both resolve to canonical "
            "container 'reagents.A1'"
        ),
    ):
        store.create_fluid_state(
            deck_path,
            deck,
            initial_fluids={
                "reagents.buffer": {"volume_ul": 80.0},
                "reagents.A1": {"volume_ul": 25.0},
            },
        )

    assert store._conn.execute(
        "SELECT COUNT(*) FROM fluid_state_sessions"
    ).fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM fluid_containers"
    ).fetchone()[0] == 0
    store.close()


def test_transfer_moves_proportional_mixture_and_is_idempotent(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_seeded_state(store, deck_path, deck)
    campaign_id = store.create_campaign("transfer", fluid_state_id=state_id)

    assert store.begin_fluid_transfer(
        state_id,
        "campaign:1/step:2/sub:0",
        "source",
        "plate.A1",
        50.0,
        campaign_id=campaign_id,
    ) is True
    store.complete_fluid_transfer("campaign:1/step:2/sub:0")
    assert store.begin_fluid_transfer(
        state_id,
        "campaign:1/step:2/sub:0",
        "source",
        "plate.A1",
        50.0,
        campaign_id=campaign_id,
    ) is False
    with pytest.raises(FluidStateError, match="different transfer parameters"):
        store.begin_fluid_transfer(
            state_id,
            "campaign:1/step:2/sub:0",
            "source",
            "plate.A1",
            10.0,
            campaign_id=campaign_id,
        )

    snapshot = store.get_fluid_snapshot(state_id)
    source = _container(snapshot, "source")
    destination = _container(snapshot, "plate.A1")
    assert source["current_volume_ul"] == pytest.approx(50.0)
    assert source["composition"] == pytest.approx({"ethanol": 20.0, "water": 30.0})
    assert destination["current_volume_ul"] == pytest.approx(50.0)
    assert destination["composition"] == pytest.approx(
        {"ethanol": 20.0, "water": 30.0}
    )
    assert source["version"] == 2  # initial seed + transfer
    assert destination["version"] == 1
    assert len(snapshot["operations"]) == 1
    operation = snapshot["operations"][0]
    assert operation["operation_key"] == "campaign:1/step:2/sub:0"
    assert operation["operation_type"] == "transfer"
    assert operation["source"] == "source"
    assert operation["destination"] == "plate.A1"
    assert operation["volume_ul"] == pytest.approx(50.0)
    assert operation["composition"] == pytest.approx(
        {"ethanol": 20.0, "water": 30.0}
    )
    assert operation["status"] == "applied"
    assert operation["campaign_id"] == campaign_id
    assert operation["detail"] is None
    assert operation["applied_at"] is not None
    store.close()


def test_begin_rejects_underflow_and_capacity_overfill_without_journal(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = store.create_fluid_state(
        deck_path,
        deck,
        initial_fluids={
            "source": {"volume_ul": 20.0},
            "plate.A1": {"volume_ul": 195.0},
        },
    )
    campaign_id = _create_linked_campaign(store, state_id)

    with pytest.raises(FluidStateError, match="only 20"):
        store.begin_fluid_transfer(
            state_id, "underflow", "source", "plate.A2", 25.0,
            campaign_id=campaign_id,
        )
    with pytest.raises(FluidStateError, match="overfill"):
        store.begin_fluid_transfer(
            state_id, "overfill", "source", "plate.A1", 10.0,
            campaign_id=campaign_id,
        )

    assert store._conn.execute("SELECT COUNT(*) FROM fluid_operations").fetchone()[0] == 0
    store.close()


def test_complete_rolls_back_both_containers_when_apply_status_fails(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_seeded_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)
    store.begin_fluid_transfer(
        state_id, "rollback", "source", "plate.A1", 50.0,
        campaign_id=campaign_id,
    )
    store._conn.execute(
        """
        CREATE TRIGGER fail_fluid_apply
        BEFORE UPDATE OF status ON fluid_operations
        WHEN NEW.status = 'applied'
        BEGIN
            SELECT RAISE(ABORT, 'forced apply failure');
        END
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced apply failure"):
        store.complete_fluid_transfer("rollback")

    snapshot = store.get_fluid_snapshot(state_id)
    assert _container(snapshot, "source")["current_volume_ul"] == 100.0
    assert _container(snapshot, "plate.A1")["current_volume_ul"] == 0.0
    assert snapshot["operations"][0]["status"] == "started"
    store.close()


def test_resume_refuses_started_and_reconciliation_operations(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_seeded_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)
    store.begin_fluid_transfer(
        state_id, "indeterminate", "source", "plate.A1", 10.0,
        campaign_id=campaign_id,
    )

    with pytest.raises(FluidStateReconciliationRequiredError, match="indeterminate"):
        store.resume_fluid_state(state_id, deck_path, deck)
    with pytest.raises(FluidStateReconciliationRequiredError, match="indeterminate"):
        store.begin_fluid_transfer(
            state_id, "must-not-start", "source", "plate.A2", 10.0,
            campaign_id=campaign_id,
        )

    store._conn.execute(
        "UPDATE fluid_state_sessions SET updated_at = '2000-01-01 00:00:00' "
        "WHERE id = ?",
        (state_id,),
    )
    store._conn.commit()
    store.mark_fluid_reconciliation_required(
        "indeterminate", "process stopped after pipette actuation",
    )
    with pytest.raises(FluidStateReconciliationRequiredError, match="reconciliation"):
        store.resume_fluid_state(state_id, deck_path, deck)
    operation = store.get_fluid_snapshot(state_id)["operations"][0]
    assert operation["status"] == "reconciliation_required"
    assert operation["detail"] == "process stopped after pipette actuation"
    assert store.get_fluid_snapshot(state_id)["updated_at"] != "2000-01-01 00:00:00"
    with pytest.raises(FluidStateReconciliationRequiredError, match="indeterminate"):
        store.begin_fluid_transfer(
            state_id, "still-must-not-start", "source", "plate.A2", 10.0,
            campaign_id=campaign_id,
        )
    store.close()


def test_seed_rejects_pending_operation_without_mutating_container(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_seeded_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)
    store.begin_fluid_transfer(
        state_id,
        "pending-before-seed",
        "source",
        "plate.A1",
        10.0,
        campaign_id=campaign_id,
    )

    with pytest.raises(FluidStateReconciliationRequiredError, match="cannot be seeded"):
        store.seed_fluid(state_id, "source", 80.0, {"water": 80.0})

    assert _container(store.get_fluid_snapshot(state_id), "source")[
        "current_volume_ul"
    ] == 100.0
    store.close()


def test_begin_requires_existing_campaign_linked_to_same_state(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    first_state = _create_seeded_state(store, deck_path, deck)
    second_state = _create_seeded_state(store, deck_path, deck)
    wrong_campaign = _create_linked_campaign(store, second_state)
    unlinked_campaign = store.create_campaign("unlinked")

    with pytest.raises(FluidStateError, match="require a campaign_id"):
        store.begin_fluid_transfer(
            first_state, "missing-campaign", "source", "plate.A1", 10.0,
        )
    with pytest.raises(FluidStateError, match="not found"):
        store.begin_fluid_transfer(
            first_state,
            "unknown-campaign",
            "source",
            "plate.A1",
            10.0,
            campaign_id=999,
        )
    with pytest.raises(FluidStateError, match="not linked"):
        store.begin_fluid_transfer(
            first_state,
            "unlinked-campaign",
            "source",
            "plate.A1",
            10.0,
            campaign_id=unlinked_campaign,
        )
    with pytest.raises(FluidStateError, match=f"fluid state {second_state}"):
        store.begin_fluid_transfer(
            first_state,
            "wrong-state",
            "source",
            "plate.A1",
            10.0,
            campaign_id=wrong_campaign,
        )
    assert store._conn.execute("SELECT COUNT(*) FROM fluid_operations").fetchone()[0] == 0
    store.close()


def test_operator_can_resolve_applied_cancelled_and_partial_operations(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_seeded_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    store.begin_fluid_transfer(
        state_id,
        "operator-applied",
        "source",
        "plate.A1",
        20.0,
        campaign_id=campaign_id,
    )
    store.mark_fluid_reconciliation_required(
        "operator-applied", "controller disconnected after dispense",
    )
    store.resolve_fluid_operation(
        "operator-applied",
        "applied",
        detail="operator confirmed source and destination menisci",
    )
    applied = store.get_fluid_snapshot(state_id)
    assert _container(applied, "source")["current_volume_ul"] == 80.0
    assert _container(applied, "plate.A1")["current_volume_ul"] == 20.0
    assert applied["operations"][0]["status"] == "applied"
    assert store.resume_fluid_state(state_id, deck_path, deck) == state_id

    store.begin_fluid_transfer(
        state_id,
        "operator-cancelled",
        "source",
        "plate.A2",
        10.0,
        campaign_id=campaign_id,
    )
    store.resolve_fluid_operation(
        "operator-cancelled",
        "not_applied",
        detail="operator confirmed aspiration never started",
    )
    cancelled = store.get_fluid_snapshot(state_id)
    assert _container(cancelled, "source")["current_volume_ul"] == 80.0
    assert _container(cancelled, "plate.A2")["current_volume_ul"] == 0.0
    assert cancelled["operations"][1]["status"] == "cancelled"
    assert store.resume_fluid_state(state_id, deck_path, deck) == state_id

    store.begin_fluid_transfer(
        state_id,
        "operator-partial",
        "source",
        "plate.A2",
        10.0,
        campaign_id=campaign_id,
    )
    with pytest.raises(FluidStateError, match="exact source and destination"):
        store.resolve_fluid_operation(
            "operator-partial",
            "partial",
            detail="partial dispense observed",
            source_volume_ul=75.0,
            source_composition={"ethanol": 30.0, "water": 45.0},
        )
    store.resolve_fluid_operation(
        "operator-partial",
        "partial",
        detail="operator measured exact post-failure state",
        source_volume_ul=75.0,
        source_composition={"ethanol": 30.0, "water": 45.0},
        destination_volume_ul=5.0,
        destination_composition={"ethanol": 2.0, "water": 3.0},
    )
    reconciled = store.get_fluid_snapshot(state_id)
    assert _container(reconciled, "source")["composition"] == {
        "ethanol": 30.0,
        "water": 45.0,
    }
    assert _container(reconciled, "plate.A2")["composition"] == {
        "ethanol": 2.0,
        "water": 3.0,
    }
    assert reconciled["operations"][2]["status"] == "reconciled"
    assert store.resume_fluid_state(state_id, deck_path, deck) == state_id
    store.close()


def test_mix_is_net_zero_journal_and_exact_replay_is_skipped(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_seeded_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    assert store.begin_fluid_mix(
        state_id,
        "mix-once",
        "source",
        25.0,
        4,
        20.0,
        campaign_id=campaign_id,
    ) is True
    before = _container(store.get_fluid_snapshot(state_id), "source")
    store.complete_fluid_mix("mix-once")
    after_snapshot = store.get_fluid_snapshot(state_id)
    after = _container(after_snapshot, "source")

    assert (after["current_volume_ul"], after["composition"], after["version"]) == (
        before["current_volume_ul"],
        before["composition"],
        before["version"],
    )
    operation = after_snapshot["operations"][0]
    assert operation["operation_type"] == "mix"
    assert operation["source"] == operation["destination"] == "source"
    assert operation["parameters"] == {
        "height": 0.0,
        "repetitions": 4,
        "speed": 20.0,
    }
    assert operation["status"] == "applied"
    assert store.begin_fluid_mix(
        state_id,
        "mix-once",
        "source",
        25.0,
        4,
        20.0,
        campaign_id=campaign_id,
    ) is False
    with pytest.raises(FluidStateError, match="different mix parameters"):
        store.begin_fluid_mix(
            state_id,
            "mix-once",
            "source",
            25.0,
            5,
            20.0,
            campaign_id=campaign_id,
        )
    store.close()


def test_pending_reservation_is_serialized_across_two_connections(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    db_path = tmp_path / "concurrent.db"
    first = DataStore(db_path)
    state_id = _create_seeded_state(first, deck_path, deck)
    campaign_id = _create_linked_campaign(first, state_id)
    second = DataStore(db_path)

    assert first.begin_fluid_transfer(
        state_id,
        "first-writer",
        "source",
        "plate.A1",
        10.0,
        campaign_id=campaign_id,
    ) is True
    with pytest.raises(FluidStateReconciliationRequiredError, match="first-writer"):
        second.begin_fluid_transfer(
            state_id,
            "second-writer",
            "source",
            "plate.A2",
            10.0,
            campaign_id=campaign_id,
        )
    indexes = second._conn.execute("PRAGMA index_list(fluid_operations)").fetchall()
    assert any(row[1] == "fluid_operations_one_pending_per_state" for row in indexes)
    second.resolve_fluid_operation(
        "first-writer",
        "cancelled",
        detail="operator confirmed no physical actuation",
    )
    assert second.resume_fluid_state(state_id, deck_path, deck) == state_id
    first.close()
    second.close()


def test_resume_uses_runtime_descriptor_not_path_or_display_label(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = store.create_fluid_state(deck_path, deck)
    original = store.get_fluid_snapshot(state_id)

    renamed = tmp_path / "renamed-deck.yaml"
    relabelled = DECK_YAML.replace(
        "name: destination_plate\n",
        "name: destination_plate\n    label: Operator-facing plate\n",
    )
    renamed.write_text(relabelled, encoding="utf-8")
    relabelled_deck = load_deck_from_yaml_safe(renamed)

    assert store.resume_fluid_state(state_id, renamed, relabelled_deck) == state_id
    current = store.get_fluid_snapshot(state_id)
    assert current["deck_fingerprint"] == original["deck_fingerprint"]
    assert current["deck_path"] == str(deck_path.resolve())
    assert "label" not in current["deck_snapshot"]["labware"]["plate"]
    store.close()


def test_resume_rejects_vial_grid_alias_retargeting(tmp_path):
    deck_path, deck = _write_deck(tmp_path, VIAL_GRID_YAML)
    store = DataStore(":memory:")
    state_id = store.create_fluid_state(deck_path, deck)
    snapshot = store.get_fluid_snapshot(state_id)
    assert snapshot["deck_descriptor"]["target_aliases"] == {
        "reagents.buffer": "reagents.A1",
        "reagents.product": "reagents.A2",
    }

    retargeted_path = tmp_path / "retargeted-deck.yaml"
    retargeted_path.write_text(
        VIAL_GRID_YAML.replace("buffer: A1", "buffer: A2"),
        encoding="utf-8",
    )
    retargeted_deck = load_deck_from_yaml_safe(retargeted_path)

    with pytest.raises(FluidStateDeckMismatchError, match="fingerprint"):
        store.resume_fluid_state(state_id, retargeted_path, retargeted_deck)
    store.close()


def test_snapshot_queries_share_one_sqlite_read_generation(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    db_path = tmp_path / "snapshot-generation.db"
    store = DataStore(db_path)
    state_id = store.create_fluid_state(
        deck_path,
        deck,
        initial_fluids={"source": {"volume_ul": 25.0}},
    )
    store._conn.execute("PRAGMA journal_mode = WAL").fetchone()
    with store._conn:
        store._conn.execute(
            "UPDATE fluid_state_sessions SET updated_at = 'before' WHERE id = ?",
            (state_id,),
        )

    writer = sqlite3.connect(db_path)
    callback_errors: list[BaseException] = []
    write_committed = False

    def commit_between_snapshot_queries(statement: str) -> None:
        nonlocal write_committed
        if write_committed or not statement.lstrip().startswith(
            "SELECT labware_key, location_id, labware_type"
        ):
            return
        write_committed = True
        try:
            writer.execute(
                "UPDATE fluid_containers SET current_volume_ul = 50.0, "
                "composition_json = '{\"unknown\":50.0}', version = version + 1 "
                "WHERE fluid_state_id = ? AND labware_key = 'source'",
                (state_id,),
            )
            writer.execute(
                "UPDATE fluid_state_sessions SET updated_at = 'after' WHERE id = ?",
                (state_id,),
            )
            writer.execute(
                "INSERT INTO fluid_operations ("
                "fluid_state_id, operation_key, operation_type, "
                "source_labware_key, source_location_id, "
                "destination_labware_key, destination_location_id, volume_ul, "
                "composition_json, parameters_json, source_version, "
                "destination_version, status) "
                "VALUES (?, 'concurrent-commit', 'transfer', 'source', '', "
                "'plate', 'A1', 1.0, '{\"unknown\":1.0}', '{}', 1, 1, "
                "'applied')",
                (state_id,),
            )
            writer.commit()
        except BaseException as exc:  # trace callbacks otherwise swallow errors
            callback_errors.append(exc)
            writer.rollback()

    store._conn.set_trace_callback(commit_between_snapshot_queries)
    before = store.get_fluid_snapshot(state_id)
    store._conn.set_trace_callback(None)

    assert write_committed is True
    assert callback_errors == []
    assert before["updated_at"] == "before"
    assert _container(before, "source")["current_volume_ul"] == 25.0
    assert before["operations"] == []

    after = store.get_fluid_snapshot(state_id)
    assert after["updated_at"] == "after"
    assert _container(after, "source")["current_volume_ul"] == 50.0
    assert [operation["operation_key"] for operation in after["operations"]] == [
        "concurrent-commit"
    ]
    writer.close()
    store.close()


def test_resume_rejects_tampered_container_identity_or_metadata(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = store.create_fluid_state(deck_path, deck)
    store._conn.execute(
        "UPDATE fluid_containers SET capacity_ul = 499.0 "
        "WHERE fluid_state_id = ? AND labware_key = 'source'",
        (state_id,),
    )
    store._conn.commit()

    with pytest.raises(FluidStateDeckMismatchError, match="metadata_mismatch"):
        store.resume_fluid_state(state_id, deck_path, deck)
    store.close()


def test_invalid_or_untrackable_initial_target_rolls_back_session(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")

    with pytest.raises(FluidStateError, match="not registered"):
        store.create_fluid_state(
            deck_path,
            deck,
            initial_fluids={"untracked.A1": {"volume_ul": 10.0}},
        )
    assert store._conn.execute(
        "SELECT COUNT(*) FROM fluid_state_sessions"
    ).fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM fluid_containers"
    ).fetchone()[0] == 0
    store.close()


def test_existing_database_migrates_campaign_fluid_state_link(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            deck_config TEXT,
            board_config TEXT,
            gantry_config TEXT,
            protocol_config TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            status TEXT NOT NULL DEFAULT 'running',
            finished_at TEXT
        )
        """
    )
    connection.commit()
    connection.close()

    store = DataStore(db_path)
    columns = {
        row[1] for row in store._conn.execute("PRAGMA table_info(campaigns)")
    }

    assert "fluid_state_id" in columns
    assert store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name = 'fluid_state_sessions'"
    ).fetchone() == ("fluid_state_sessions",)
    store.close()


@pytest.mark.parametrize(
    "seed_yaml",
    [
        "source:\n  volume_ul: 10\n",
        "fluids: []\n",
        "fluids:\n  source:\n    composition: {water: 10}\n",
        "fluids:\n  source:\n    volume_ul: 10\n    composition: {water: 9}\n",
    ],
)
def test_load_initial_fluids_rejects_invalid_shapes(tmp_path, seed_yaml):
    path = tmp_path / "bad.yaml"
    path.write_text(seed_yaml, encoding="utf-8")
    with pytest.raises(FluidStateError):
        load_initial_fluids(path)


@pytest.mark.parametrize(
    "seed_yaml",
    [
        """\
fluids:
  source:
    volume_ul: 10
  source:
    volume_ul: 20
""",
        """\
fluids:
  source:
    volume_ul: 10
    composition:
      water: 5
      water: 5
""",
    ],
    ids=("duplicate-target", "duplicate-composition-component"),
)
def test_load_initial_fluids_rejects_duplicate_yaml_keys(tmp_path, seed_yaml):
    path = tmp_path / "duplicate.yaml"
    path.write_text(seed_yaml, encoding="utf-8")

    with pytest.raises(FluidStateError, match="duplicate YAML key"):
        load_initial_fluids(path)


def test_load_replacement_state_returns_normalized_exact_endpoints(tmp_path):
    path = tmp_path / "replacement.yaml"
    path.write_text(
        """\
source:
  volume_ul: 75
  composition: {water: 45, ethanol: 30}
destination:
  volume_ul: 5
  composition: {water: 3, ethanol: 2}
""",
        encoding="utf-8",
    )

    assert load_replacement_state(path) == {
        "source": {
            "volume_ul": 75.0,
            "composition": {"ethanol": 30.0, "water": 45.0},
        },
        "destination": {
            "volume_ul": 5.0,
            "composition": {"ethanol": 2.0, "water": 3.0},
        },
    }


@pytest.mark.parametrize(
    "replacement_yaml",
    [
        """\
source: {volume_ul: 1, composition: {water: 1}}
source: {volume_ul: 1, composition: {water: 1}}
destination: {volume_ul: 0, composition: {}}
""",
        """\
source:
  volume_ul: 1
  volume_ul: 1
  composition: {water: 1}
destination: {volume_ul: 0, composition: {}}
""",
        """\
source:
  volume_ul: 1
  composition:
    water: 0.5
    water: 0.5
destination: {volume_ul: 0, composition: {}}
""",
    ],
    ids=("duplicate-endpoint", "duplicate-field", "duplicate-component"),
)
def test_load_replacement_state_rejects_duplicate_yaml_keys(
    tmp_path,
    replacement_yaml,
):
    path = tmp_path / "duplicate-replacement.yaml"
    path.write_text(replacement_yaml, encoding="utf-8")

    with pytest.raises(FluidStateError, match="duplicate YAML key"):
        load_replacement_state(path)


@pytest.mark.parametrize(
    "replacement_yaml, message",
    [
        (
            "source: {}\ndestination: {}\nextra: {}\n",
            "exactly `source` and `destination`",
        ),
        (
            "source: {volume_ul: 1}\n"
            "destination: {volume_ul: 0, composition: {}}\n",
            "`source` must contain exactly",
        ),
        (
            "source: {volume_ul: 1, composition: {water: 0.5}}\n"
            "destination: {volume_ul: 0, composition: {}}\n",
            "totals 0.5 uL",
        ),
        (
            "source: {volume_ul: .nan, composition: {}}\n"
            "destination: {volume_ul: 0, composition: {}}\n",
            "must be a finite non-negative number",
        ),
        (
            "source: {volume_ul: 1, composition: {water: .inf}}\n"
            "destination: {volume_ul: 0, composition: {}}\n",
            "must be a finite non-negative number",
        ),
    ],
    ids=(
        "top-level-shape",
        "endpoint-shape",
        "composition-sum",
        "non-finite-volume",
        "non-finite-component",
    ),
)
def test_load_replacement_state_rejects_invalid_contract(
    tmp_path,
    replacement_yaml,
    message,
):
    path = tmp_path / "bad-replacement.yaml"
    path.write_text(replacement_yaml, encoding="utf-8")

    with pytest.raises(FluidStateError, match=message):
        load_replacement_state(path)
