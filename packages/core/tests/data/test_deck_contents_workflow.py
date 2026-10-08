"""Regression tests for the persistent deck-contents workflow."""

from __future__ import annotations

import pytest

from cubos.data import (
    DataStore,
    FluidStateError,
    FluidStateReconciliationRequiredError,
)
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
  destination:
    type: vial
    name: destination
    model_name: destination_vial
    height: 50.0
    diameter: 20.0
    location: {x: 40.0, y: 5.0, z: 20.0}
    capacity_ul: 500.0
    working_volume_ul: 400.0
"""


def _state(tmp_path, *, db_path=None):
    deck_path = tmp_path / "deck.yaml"
    deck_path.write_text(DECK_YAML, encoding="utf-8")
    deck = load_deck_from_yaml_safe(deck_path)
    store = DataStore(db_path or ":memory:")
    state_id = store.create_fluid_state(
        deck_path,
        deck,
        initial_fluids={
            "source": {
                "volume_ul": 100.0,
                "composition": {"water": 60.0, "ethanol": 40.0},
            },
            "destination": {"volume_ul": 20.0, "composition": {"water": 20.0}},
        },
    )
    return store, state_id


def _select(store: DataStore, state_id: int):
    return store.set_active_fluid_state(state_id)


def test_active_setup_compare_and_swap_is_rejected_across_connections(tmp_path):
    db_path = tmp_path / "state.sqlite"
    seed_store, state_id = _state(tmp_path, db_path=db_path)
    seed_store.close()
    first = DataStore(db_path)
    second = DataStore(db_path)
    try:
        assert first.set_active_fluid_state(state_id)["revision"] == 1
        with pytest.raises(ValueError, match="revision"):
            second.set_active_fluid_state(state_id, expected_revision=0)
        assert second.get_active_fluid_state()["revision"] == 1
    finally:
        first.close()
        second.close()


def test_manual_batch_rolls_back_when_a_later_action_fails(tmp_path):
    store, state_id = _state(tmp_path)
    _select(store, state_id)
    before = store.get_fluid_container(state_id, "source", "")

    with pytest.raises(FluidStateError):
        store.apply_manual_edits(
            state_id,
            [
                {"mode": "set", "labware_key": "source", "volume_ul": 80.0,
                 "composition": {"water": 80.0}},
                {"mode": "set", "labware_key": "missing", "volume_ul": 1.0},
            ],
        )

    after = store.get_fluid_container(state_id, "source", "")
    assert after["current_volume_ul"] == before["current_volume_ul"]
    assert after["version"] == before["version"]
    assert store.get_fluid_snapshot(state_id)["operations"] == []


def test_manual_transfer_rejects_stale_source_or_destination_versions(tmp_path):
    store, state_id = _state(tmp_path)
    _select(store, state_id)
    source = store.get_fluid_container(state_id, "source", "")
    destination = store.get_fluid_container(state_id, "destination", "")

    store.apply_manual_edits(
        state_id,
        [{"mode": "set", "labware_key": "source", "volume_ul": 90.0,
          "composition": {"water": 90.0}}],
        expected_revisions={"source": source["version"]},
    )
    with pytest.raises(ValueError, match="container revision"):
        store.apply_manual_edits(
            state_id,
            [{"mode": "transfer", "labware_key": "source",
              "destination_labware_key": "destination", "volume_ul": 10.0}],
            expected_revisions={
                "source": source["version"],
                "destination": destination["version"],
            },
        )
    assert store.get_fluid_container(state_id, "destination", "")["version"] == destination["version"]


def test_manual_transfer_conserves_composition_and_audits_both_endpoints(tmp_path):
    store, state_id = _state(tmp_path)
    _select(store, state_id)

    result = store.apply_manual_edits(
        state_id,
        [{"mode": "transfer", "labware_key": "source",
          "destination_labware_key": "destination", "volume_ul": 25.0}],
    )

    assert {row["labware_key"] for row in result} == {"source", "destination"}
    source = store.get_fluid_container(state_id, "source", "")
    destination = store.get_fluid_container(state_id, "destination", "")
    assert source["current_volume_ul"] + destination["current_volume_ul"] == pytest.approx(120.0)
    assert source["composition"] == {"water": 45.0, "ethanol": 30.0}
    assert destination["composition"] == {"water": 35.0, "ethanol": 10.0}
    edits = store._conn.execute(
        "SELECT labware_key, operation FROM fluid_manual_edits "
        "WHERE fluid_state_id = ? ORDER BY id", (state_id,)
    ).fetchall()
    assert edits == [("source", "transfer"), ("destination", "transfer")]


def test_manual_edit_rolls_back_contents_when_audit_insert_fails(tmp_path):
    store, state_id = _state(tmp_path)
    _select(store, state_id)
    before = store.get_fluid_container(state_id, "source", "")
    store._conn.execute(
        "CREATE TRIGGER reject_manual_audit BEFORE INSERT ON fluid_manual_edits "
        "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
    )
    store._conn.commit()

    with pytest.raises(Exception, match="audit unavailable"):
        store.apply_manual_edits(
            state_id,
            [{"mode": "set", "labware_key": "source", "volume_ul": 80.0,
              "composition": {"water": 80.0}}],
        )

    after = store.get_fluid_container(state_id, "source", "")
    assert after["current_volume_ul"] == before["current_volume_ul"]
    assert after["version"] == before["version"]


def test_legacy_fluid_transfer_journal_still_requires_campaign_and_is_conservative(tmp_path):
    store, state_id = _state(tmp_path)
    campaign_id = store.create_campaign("legacy", fluid_state_id=state_id)
    assert store.begin_fluid_transfer(
        state_id, "legacy-transfer", "source", "destination", 25.0,
        campaign_id=campaign_id,
    )
    store.complete_fluid_transfer("legacy-transfer")
    source = store.get_fluid_container(state_id, "source", "")
    destination = store.get_fluid_container(state_id, "destination", "")
    assert source["current_volume_ul"] == pytest.approx(75.0)
    assert destination["current_volume_ul"] == pytest.approx(45.0)
    assert source["composition"] == {"water": 45.0, "ethanol": 30.0}
    assert destination["composition"] == {"water": 35.0, "ethanol": 10.0}


def test_manual_edits_are_blocked_while_a_fluid_operation_is_pending(tmp_path):
    store, state_id = _state(tmp_path)
    campaign_id = store.create_campaign("pending", fluid_state_id=state_id)
    _select(store, state_id)
    store.begin_fluid_transfer(
        state_id, "pending-transfer", "source", "destination", 10.0,
        campaign_id=campaign_id,
    )

    with pytest.raises(FluidStateReconciliationRequiredError):
        store.apply_manual_edits(
            state_id,
            [{"mode": "set", "labware_key": "source", "volume_ul": 90.0,
              "composition": {"water": 90.0}}],
        )


def test_unknown_opt_in_blocks_transfer_and_explicit_empty_is_known(tmp_path):
    deck_path = tmp_path / "deck.yaml"
    deck_path.write_text(DECK_YAML, encoding="utf-8")
    deck = load_deck_from_yaml_safe(deck_path)
    store = DataStore(":memory:")
    state_id = store.create_fluid_state(deck_path, deck, omitted_volumes_unknown=True)
    store.set_active_fluid_state(state_id)
    with pytest.raises(ValueError, match="known source"):
        store.apply_manual_edits(state_id, [{"mode": "transfer", "labware_key": "source", "destination_labware_key": "destination", "volume_ul": 1}])
    empty = store.apply_manual_edits(state_id, [{"mode": "empty", "labware_key": "source"}])
    assert empty[0]["volume_known"] is True


def test_manual_edit_rejects_stale_active_revision_and_invalid_composition(tmp_path):
    store, state_id = _state(tmp_path)
    revision = _select(store, state_id)["revision"]
    with pytest.raises(ValueError, match="active setup revision"):
        store.apply_manual_edits(state_id, [{"mode": "set", "labware_key": "source", "volume_ul": 10, "composition": {"water": 10}}], expected_active_revision=revision - 1)
    with pytest.raises(ValueError, match="composition"):
        store.apply_manual_edits(state_id, [{"mode": "set", "labware_key": "source", "volume_ul": 10, "composition": {"water": 9}}])


def test_active_selection_rejects_unknown_state_and_history_is_empty_before_edit(tmp_path):
    store, state_id = _state(tmp_path)
    with pytest.raises(ValueError, match="does not exist"):
        store.set_active_fluid_state(999)
    assert store.get_active_fluid_state() is None
    assert store.list_fluid_manual_edits(state_id) == []


def test_manual_add_carries_supplied_components_and_blank_set_is_unknown(tmp_path):
    store, state_id = _state(tmp_path)
    _select(store, state_id)
    source = store.get_fluid_container(state_id, "source", "")
    store.apply_manual_edits(
        state_id,
        [{"mode": "add", "labware_key": "source", "volume_ul": 10,
          "composition": {"dye": 10}}],
        expected_revisions={"source": source["version"]},
    )
    added = store.get_fluid_container(state_id, "source", "")
    assert added["current_volume_ul"] == 110
    assert added["composition"]["dye"] == 10
    store.apply_manual_edits(
        state_id,
        [{"mode": "set", "labware_key": "source", "volume_ul": 25}],
        expected_revisions={"source": added["version"]},
    )
    assert store.get_fluid_container(state_id, "source", "")["composition"] == {"unknown": 25}


def test_manual_remove_preserves_composition_proportionally(tmp_path):
    store, state_id = _state(tmp_path)
    _select(store, state_id)
    source = store.get_fluid_container(state_id, "source", "")
    store.apply_manual_edits(
        state_id,
        [{"mode": "remove", "labware_key": "source", "volume_ul": 25}],
        expected_revisions={"source": source["version"]},
    )
    removed = store.get_fluid_container(state_id, "source", "")
    assert removed["current_volume_ul"] == 75
    assert removed["composition"] == {"water": 45, "ethanol": 30}


def test_single_container_adjust_without_composition_records_unknown_contents(tmp_path):
    store, state_id = _state(tmp_path)
    _select(store, state_id)

    adjusted = store.adjust_fluid_container(
        state_id, "source", "", 25.0, composition=None,
    )

    assert adjusted["volume_known"] is True
    assert adjusted["composition"] == {"unknown": 25.0}
