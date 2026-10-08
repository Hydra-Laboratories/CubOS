"""Focused tests for durable deck-associated tip and pipette-attachment state.

Mirrors the organization of ``tests/data/test_fluid_state.py``: tip/pipette
state hangs off the same ``fluid_state_sessions`` row a campaign's fluid
state uses, sharing its two-phase begin/complete operation journal and
single-pending-operation-per-session safety posture.
"""

from __future__ import annotations

import sqlite3

import pytest

from cubos.data import (
    DataStore,
    FluidStateReconciliationRequiredError,
    TipStateConflictError,
    TipStateDeckMismatchError,
    TipStateError,
    TipStateNotFoundError,
    TipStateReconciliationRequiredError,
)
from cubos.deck.loader import load_deck_from_yaml_safe


DECK_YAML = """\
labware:
  reagent:
    type: vial
    name: reagent
    model_name: reagent_vial
    height: 40.0
    diameter: 15.0
    location: {x: 5.0, y: 5.0, z: 20.0}
    capacity_ul: 500.0
    working_volume_ul: 400.0

  waste:
    type: vial
    name: waste
    model_name: waste_vial
    height: 40.0
    diameter: 15.0
    location: {x: 20.0, y: 5.0, z: 20.0}
    capacity_ul: 500.0
    working_volume_ul: 400.0

  tip_rack:
    load_name: ursa_tip_rack
    name: test_tip_rack
    tip_length: 59.3
    pickup_z: 64.7
    drop_z: 34.0
    calibration:
      a1: {x: 210.0, y: 230.0}
      a2: {x: 218.5, y: 230.0}
"""


def _write_deck(tmp_path, text: str = DECK_YAML, *, pre_consumed: tuple[str, ...] = ("B2",)):
    """Write *text* to disk and load it, pre-marking *pre_consumed* tip slots.

    The YAML ``tip_present`` field replaces the whole per-tip map rather than
    merging with the auto-filled default (any tip absent from a supplied map
    defaults to *not present*), so pre-consuming just one slot is done on the
    loaded runtime object instead of via YAML.
    """
    path = tmp_path / "deck.yaml"
    path.write_text(text, encoding="utf-8")
    deck = load_deck_from_yaml_safe(path)
    rack = deck.labware.get("tip_rack")
    if rack is not None:
        for slot_id in pre_consumed:
            rack.mark_tip_used(slot_id)
    return path, deck


def _create_state(store, deck_path, deck, label="tip demo"):
    return store.create_fluid_state(deck_path, deck, label=label)


def _create_linked_campaign(store, state_id, description="tip test"):
    return store.create_campaign(description, fluid_state_id=state_id)


def _slot(snapshot, rack_key: str, slot_id: str) -> dict:
    return next(
        container
        for container in snapshot["containers"]
        if container["rack_key"] == rack_key and container["slot_id"] == slot_id
    )


class _FakePipette:
    """Minimal double implementing the two tip-extension setters used here."""

    def __init__(self) -> None:
        self.extension_mm: float | None = None
        self.clear_calls = 0

    def set_attached_tip_extension(self, extension_mm: float) -> None:
        self.extension_mm = extension_mm

    def clear_attached_tip_extension(self) -> None:
        self.extension_mm = None
        self.clear_calls += 1


# ── Schema / seeding round-trips ────────────────────────────────────────────


def test_schema_has_tip_tables_and_pending_index(tmp_path):
    db_path = tmp_path / "state.db"
    store = DataStore(db_path)
    store.close()

    connection = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "tip_containers",
            "tip_operations",
            "tip_refill_operations",
            "pipette_attachment",
        } <= tables
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "tip_operations_one_pending_per_state" in indexes
    finally:
        connection.close()


def test_create_seeds_tip_containers_from_deck_tip_present_and_pipette_row(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)

    snapshot = store.get_tip_snapshot(state_id)
    assert snapshot["fluid_state_id"] == state_id
    assert len(snapshot["containers"]) == 30  # 15 rows x 2 columns
    assert _slot(snapshot, "tip_rack", "A1")["status"] == "available"
    assert _slot(snapshot, "tip_rack", "A2")["status"] == "available"
    # Seeded from the deck's tip_present override.
    assert _slot(snapshot, "tip_rack", "B2")["status"] == "consumed"
    assert all(
        container["tip_length_mm"] == pytest.approx(59.3)
        for container in snapshot["containers"]
    )
    assert snapshot["operations"] == []
    pipette = snapshot["pipette"]
    assert pipette == {
        "pipette_key": "pipette",
        "rack_key": None,
        "slot_id": None,
        "tip_extension_mm": None,
        "contents_known_empty": True,
        "attachment_uncertain": False,
        "updated_at": pipette["updated_at"],
    }
    store.close()


def test_operator_refill_resets_tips_and_preserves_fluid_history(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)
    store.seed_fluid(state_id, "reagent", 100.0)
    store.begin_pick_up_tip(
        state_id, "refill-pick", "tip_rack", "A1", 59.3, campaign_id=campaign_id,
    )
    store.complete_pick_up_tip("refill-pick")
    store.begin_fluid_transfer(
        state_id, "refill-pending-fluid", "reagent", "waste", 10.0,
        campaign_id=campaign_id,
    )

    before = store.get_fluid_snapshot(state_id)
    refill = store.refill_tip_rack(
        state_id,
        "refill-rack-1",
        "tip_rack",
        operator="alexc",
        reason="operator confirmed a new full rack and bare pipette",
        pipette_bare_confirmed=True,
    )
    after = store.get_fluid_snapshot(state_id)
    snapshot = store.get_tip_snapshot(state_id)

    assert refill["changed_slots"] == ["A1", "B2"]
    assert refill["preserved_slots"] == []
    assert after["containers"] == before["containers"]
    assert [op["operation_key"] for op in after["operations"]] == [
        "refill-pending-fluid"
    ]
    assert all(slot["status"] == "available" for slot in snapshot["containers"])
    assert snapshot["pipette"]["rack_key"] is None
    assert snapshot["pipette"]["attachment_uncertain"] is False
    assert snapshot["refills"][0]["operation_key"] == "refill-rack-1"
    store.close()


def test_refill_does_not_clear_pending_tip_operation(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)
    store.begin_pick_up_tip(
        state_id, "pending-refill-pick", "tip_rack", "A1", 59.3,
        campaign_id=campaign_id,
    )

    with pytest.raises(TipStateReconciliationRequiredError, match="pending-refill-pick"):
        store.refill_tip_rack(
            state_id,
            "blocked-refill",
            "tip_rack",
            operator="alexc",
            reason="operator confirmed a new rack",
            pipette_bare_confirmed=True,
        )

    snapshot = store.get_tip_snapshot(state_id)
    assert _slot(snapshot, "tip_rack", "A1")["status"] == "reserved"
    assert snapshot["refills"] == []
    store.close()


def test_refill_operation_key_cannot_cross_fluid_states(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    first_state = _create_state(store, deck_path, deck, label="first")
    second_state = _create_state(store, deck_path, deck, label="second")
    kwargs = {
        "operator": "alexc",
        "reason": "operator confirmed a new rack",
        "pipette_bare_confirmed": True,
    }
    store.refill_tip_rack(first_state, "same-key", "tip_rack", **kwargs)

    with pytest.raises(TipStateConflictError, match="belongs to fluid state"):
        store.refill_tip_rack(second_state, "same-key", "tip_rack", **kwargs)
    store.close()


def test_get_tip_snapshot_requires_existing_state(tmp_path):
    store = DataStore(":memory:")
    with pytest.raises(TipStateNotFoundError):
        store.get_tip_snapshot(999)
    store.close()


def test_untracked_deck_without_tip_racks_seeds_no_containers(tmp_path):
    no_tips_yaml = """\
labware:
  reagent:
    type: vial
    name: reagent
    model_name: reagent_vial
    height: 40.0
    diameter: 15.0
    location: {x: 5.0, y: 5.0, z: 20.0}
    capacity_ul: 500.0
    working_volume_ul: 400.0
"""
    deck_path, deck = _write_deck(tmp_path, no_tips_yaml)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)

    snapshot = store.get_tip_snapshot(state_id)
    assert snapshot["containers"] == []
    assert snapshot["pipette"]["rack_key"] is None
    store.close()


# ── pick_up_tip journal ──────────────────────────────────────────────────────


def test_pick_up_tip_reserves_before_motion_and_attaches_after_complete(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    should_execute, slot_id, extension = store.begin_pick_up_tip(
        state_id, "pick-a1", "tip_rack", "A1", 59.3, campaign_id=campaign_id,
    )
    assert (should_execute, slot_id, extension) == (True, "A1", 59.3)
    reserved = _slot(store.get_tip_snapshot(state_id), "tip_rack", "A1")
    assert reserved["status"] == "reserved"

    store.complete_pick_up_tip("pick-a1")
    attached_snapshot = store.get_tip_snapshot(state_id)
    attached = _slot(attached_snapshot, "tip_rack", "A1")
    assert attached["status"] == "attached"
    pipette = attached_snapshot["pipette"]
    assert pipette["rack_key"] == "tip_rack"
    assert pipette["slot_id"] == "A1"
    assert pipette["tip_extension_mm"] == pytest.approx(59.3)
    assert pipette["contents_known_empty"] is True
    assert pipette["attachment_uncertain"] is False
    operation = attached_snapshot["operations"][0]
    assert operation["operation_key"] == "pick-a1"
    assert operation["operation_type"] == "pick_up_tip"
    assert operation["status"] == "applied"
    assert operation["applied_at"] is not None
    store.close()


def test_pick_up_tip_next_available_selection_follows_rack_order(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    _, slot_id, _ = store.begin_pick_up_tip(
        state_id, "pick-next-1", "tip_rack", None, 59.3, campaign_id=campaign_id,
    )
    assert slot_id == "A1"
    store.complete_pick_up_tip("pick-next-1")
    store.begin_drop_tip(state_id, "drop-1", campaign_id=campaign_id)
    store.complete_drop_tip("drop-1")

    _, slot_id_2, _ = store.begin_pick_up_tip(
        state_id, "pick-next-2", "tip_rack", None, 59.3, campaign_id=campaign_id,
    )
    assert slot_id_2 == "A2"  # A1 is now consumed, B2 was pre-seeded consumed
    store.close()


def test_pick_up_tip_rejects_tip_length_mismatch(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    with pytest.raises(TipStateError, match="tip-length mismatch"):
        store.begin_pick_up_tip(
            state_id, "bad-length", "tip_rack", "A1", 12.0, campaign_id=campaign_id,
        )
    assert store.get_tip_snapshot(state_id)["operations"] == []
    store.close()


def test_pick_up_tip_rejects_unavailable_slot(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    with pytest.raises(TipStateError, match="consumed, not available"):
        store.begin_pick_up_tip(
            state_id, "pick-b2", "tip_rack", "B2", 59.3, campaign_id=campaign_id,
        )
    store.close()


def test_reapplying_applied_pick_step_does_not_double_consume(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    store.begin_pick_up_tip(
        state_id, "repeatable-pick", "tip_rack", "A1", 59.3, campaign_id=campaign_id,
    )
    store.complete_pick_up_tip("repeatable-pick")

    should_execute, slot_id, extension = store.begin_pick_up_tip(
        state_id, "repeatable-pick", "tip_rack", "A1", 59.3, campaign_id=campaign_id,
    )
    assert (should_execute, slot_id, extension) == (False, "A1", 59.3)

    snapshot = store.get_tip_snapshot(state_id)
    assert len(snapshot["operations"]) == 1
    assert _slot(snapshot, "tip_rack", "A1")["status"] == "attached"
    # A2 was never touched by the replay.
    assert _slot(snapshot, "tip_rack", "A2")["status"] == "available"
    store.close()


def test_reapplying_pick_step_with_different_parameters_raises(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    store.begin_pick_up_tip(
        state_id, "reused-key", "tip_rack", "A1", 59.3, campaign_id=campaign_id,
    )
    store.complete_pick_up_tip("reused-key")

    with pytest.raises(TipStateError, match="different pick_up_tip parameters"):
        store.begin_pick_up_tip(
            state_id, "reused-key", "tip_rack", "A2", 59.3, campaign_id=campaign_id,
        )
    store.close()


# ── Two-process restart lifecycle: never pick the same tip twice ────────────


def test_two_process_restarts_pick_distinct_tips_and_never_repeat(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    db_path = tmp_path / "restart.db"

    first = DataStore(db_path)
    state_id = _create_state(first, deck_path, deck)
    campaign_id = _create_linked_campaign(first, state_id)
    first.begin_pick_up_tip(
        state_id, "run1-pick", "tip_rack", "A1", 59.3, campaign_id=campaign_id,
    )
    first.complete_pick_up_tip("run1-pick")
    first.begin_drop_tip(state_id, "run1-drop", campaign_id=campaign_id)
    first.complete_drop_tip("run1-drop")
    first.close()  # Simulate a process restart.

    second = DataStore(db_path)
    resumed_id = second.resume_fluid_state(state_id, deck_path, deck)
    assert resumed_id == state_id

    # A fresh Deck load still reports every tip as `tip_present: True` in
    # memory; the durable DB state (not the deck) must win.
    fresh_deck = load_deck_from_yaml_safe(deck_path)
    assert fresh_deck.labware["tip_rack"].is_tip_present("A1") is True

    campaign_2 = second.create_campaign("run 2", fluid_state_id=state_id)
    with pytest.raises(TipStateError, match="consumed, not available"):
        second.begin_pick_up_tip(
            state_id, "run2-reuse-a1", "tip_rack", "A1", 59.3,
            campaign_id=campaign_2,
        )

    should_execute, slot_id, _ = second.begin_pick_up_tip(
        state_id, "run2-pick", "tip_rack", None, 59.3, campaign_id=campaign_2,
    )
    assert should_execute is True
    assert slot_id == "A2"
    second.complete_pick_up_tip("run2-pick")

    snapshot = second.get_tip_snapshot(state_id)
    assert _slot(snapshot, "tip_rack", "A1")["status"] == "consumed"
    assert _slot(snapshot, "tip_rack", "A2")["status"] == "attached"
    second.close()


# ── Failure handling before/after motion ────────────────────────────────────


def test_cancel_before_motion_returns_slot_to_available(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    store.begin_pick_up_tip(
        state_id, "aborted-before-motion", "tip_rack", "A1", 59.3,
        campaign_id=campaign_id,
    )
    assert _slot(store.get_tip_snapshot(state_id), "tip_rack", "A1")["status"] == (
        "reserved"
    )

    store.resolve_tip_operation(
        "aborted-before-motion", "cancelled",
        detail="engage failed before pick_up_tip motion was sent",
    )

    snapshot = store.get_tip_snapshot(state_id)
    assert _slot(snapshot, "tip_rack", "A1")["status"] == "available"
    assert snapshot["operations"][0]["status"] == "cancelled"
    assert snapshot["pipette"]["rack_key"] is None
    store.close()


def test_uncertain_pickup_requires_reconciliation_and_blocks_liquid_handling(
    tmp_path,
):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    store.begin_pick_up_tip(
        state_id, "uncertain-pick", "tip_rack", "A1", 59.3, campaign_id=campaign_id,
    )
    store.mark_tip_reconciliation_required(
        "uncertain-pick", "controller disconnected mid-pickup",
    )

    snapshot = store.get_tip_snapshot(state_id)
    assert _slot(snapshot, "tip_rack", "A1")["status"] == "reconciliation_required"
    assert snapshot["operations"][0]["status"] == "reconciliation_required"
    assert snapshot["pipette"]["attachment_uncertain"] is True

    # Further tip operations are blocked...
    with pytest.raises(TipStateReconciliationRequiredError, match="uncertain-pick"):
        store.begin_pick_up_tip(
            state_id, "must-not-start", "tip_rack", "A2", 59.3,
            campaign_id=campaign_id,
        )
    # ...and so is liquid handling on the SAME shared session.
    store.seed_fluid(state_id, "reagent", 100.0)
    with pytest.raises(FluidStateReconciliationRequiredError, match="uncertain-pick"):
        store.begin_fluid_transfer(
            state_id, "must-not-transfer", "reagent", "waste", 1.0,
            campaign_id=campaign_id,
        )
    store.close()


def test_pending_fluid_operation_blocks_new_tip_operation(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)
    store.seed_fluid(state_id, "reagent", 100.0)

    # A dead-end transfer (no second container) still journals a `started`
    # fluid operation before failing later steps; use two containers via a
    # second vial-less approach: mark reconciliation required directly to
    # simulate an in-flight fluid operation.
    store.begin_fluid_transfer(
        state_id, "pending-fluid", "reagent", "waste", 1.0,
        campaign_id=campaign_id,
    )

    with pytest.raises(TipStateReconciliationRequiredError, match="pending-fluid"):
        store.begin_pick_up_tip(
            state_id, "blocked-pick", "tip_rack", "A1", 59.3,
            campaign_id=campaign_id,
        )
    store.close()


# ── drop_tip journal ─────────────────────────────────────────────────────────


def test_drop_tip_clears_extension_and_marks_slot_consumed(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    store.begin_pick_up_tip(
        state_id, "pick-for-drop", "tip_rack", "A1", 59.3, campaign_id=campaign_id,
    )
    store.complete_pick_up_tip("pick-for-drop")

    should_execute, rack_key, slot_id = store.begin_drop_tip(
        state_id, "drop-it", campaign_id=campaign_id,
    )
    assert (should_execute, rack_key, slot_id) == (True, "tip_rack", "A1")
    assert _slot(store.get_tip_snapshot(state_id), "tip_rack", "A1")["status"] == (
        "reserved"
    )

    store.complete_drop_tip("drop-it")
    snapshot = store.get_tip_snapshot(state_id)
    assert _slot(snapshot, "tip_rack", "A1")["status"] == "consumed"
    pipette = snapshot["pipette"]
    assert pipette["rack_key"] is None
    assert pipette["slot_id"] is None
    assert pipette["tip_extension_mm"] is None
    assert pipette["contents_known_empty"] is True
    store.close()


def test_drop_tip_with_no_attached_tip_raises(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    with pytest.raises(TipStateError, match="nothing to drop"):
        store.begin_drop_tip(state_id, "drop-nothing", campaign_id=campaign_id)
    store.close()


def test_uncertain_drop_requires_reconciliation(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    store.begin_pick_up_tip(
        state_id, "pick-then-uncertain-drop", "tip_rack", "A1", 59.3,
        campaign_id=campaign_id,
    )
    store.complete_pick_up_tip("pick-then-uncertain-drop")
    store.begin_drop_tip(state_id, "uncertain-drop", campaign_id=campaign_id)
    store.mark_tip_reconciliation_required(
        "uncertain-drop", "controller disconnected mid-drop",
    )

    snapshot = store.get_tip_snapshot(state_id)
    assert _slot(snapshot, "tip_rack", "A1")["status"] == "reconciliation_required"
    assert snapshot["pipette"]["attachment_uncertain"] is True
    store.close()


# ── Operator reconciliation ──────────────────────────────────────────────────


def test_operator_can_resolve_uncertain_pickup_as_applied(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    store.begin_pick_up_tip(
        state_id, "operator-applied", "tip_rack", "A1", 59.3,
        campaign_id=campaign_id,
    )
    store.mark_tip_reconciliation_required(
        "operator-applied", "controller disconnected after motion completed",
    )
    store.resolve_tip_operation(
        "operator-applied", "applied",
        detail="operator confirmed the tip is physically attached",
    )

    snapshot = store.get_tip_snapshot(state_id)
    assert _slot(snapshot, "tip_rack", "A1")["status"] == "attached"
    assert snapshot["pipette"]["rack_key"] == "tip_rack"
    assert snapshot["pipette"]["attachment_uncertain"] is False
    assert snapshot["operations"][0]["status"] == "applied"
    assert store.resume_fluid_state(state_id, deck_path, deck) == state_id
    store.close()


def test_operator_can_resolve_uncertain_drop_as_partial_available(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    store.begin_pick_up_tip(
        state_id, "pick-for-partial-drop", "tip_rack", "A1", 59.3,
        campaign_id=campaign_id,
    )
    store.complete_pick_up_tip("pick-for-partial-drop")
    store.begin_drop_tip(state_id, "partial-drop", campaign_id=campaign_id)
    store.mark_tip_reconciliation_required(
        "partial-drop", "controller disconnected mid-drop",
    )

    with pytest.raises(TipStateError, match="final_slot_status"):
        store.resolve_tip_operation(
            "partial-drop", "partial", detail="operator inspected the tip",
        )

    store.resolve_tip_operation(
        "partial-drop", "partial",
        detail="operator confirmed the tip landed in the disposal",
        final_slot_status="consumed",
    )
    snapshot = store.get_tip_snapshot(state_id)
    assert _slot(snapshot, "tip_rack", "A1")["status"] == "consumed"
    assert snapshot["pipette"]["rack_key"] is None
    drop_operation = next(
        op for op in snapshot["operations"] if op["operation_key"] == "partial-drop"
    )
    assert drop_operation["status"] == "reconciled"
    assert store.resume_fluid_state(state_id, deck_path, deck) == state_id
    store.close()


def test_operator_can_cancel_uncertain_drop_leaving_tip_attached(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    store.begin_pick_up_tip(
        state_id, "pick-for-cancelled-drop", "tip_rack", "A1", 59.3,
        campaign_id=campaign_id,
    )
    store.complete_pick_up_tip("pick-for-cancelled-drop")
    store.begin_drop_tip(state_id, "cancelled-drop", campaign_id=campaign_id)
    store.mark_tip_reconciliation_required(
        "cancelled-drop", "controller disconnected before drop motion",
    )
    store.resolve_tip_operation(
        "cancelled-drop", "not_applied",
        detail="operator confirmed the drop never happened; tip still attached",
    )

    snapshot = store.get_tip_snapshot(state_id)
    assert _slot(snapshot, "tip_rack", "A1")["status"] == "attached"
    assert snapshot["pipette"]["attachment_uncertain"] is False
    assert snapshot["operations"][1]["status"] == "cancelled"
    assert store.resume_fluid_state(state_id, deck_path, deck) == state_id
    store.close()


# ── Resume: verify registry, restore extension, refuse when uncertain ───────


def test_resume_refuses_when_tip_operation_is_pending(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    store.begin_pick_up_tip(
        state_id, "unresolved-pick", "tip_rack", "A1", 59.3, campaign_id=campaign_id,
    )
    with pytest.raises(FluidStateReconciliationRequiredError, match="unresolved-pick"):
        store.resume_fluid_state(state_id, deck_path, deck)

    store.mark_tip_reconciliation_required(
        "unresolved-pick", "process stopped after motion",
    )
    with pytest.raises(FluidStateReconciliationRequiredError, match="unresolved-pick"):
        store.resume_fluid_state(state_id, deck_path, deck)
    store.close()


def test_resume_rejects_tampered_tip_length(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    store._conn.execute(
        "UPDATE tip_containers SET tip_length_mm = 12.0 "
        "WHERE fluid_state_id = ? AND rack_key = 'tip_rack' AND slot_id = 'A1'",
        (state_id,),
    )
    store._conn.commit()

    with pytest.raises(TipStateDeckMismatchError, match="tip_length_mismatch"):
        store.resume_fluid_state(state_id, deck_path, deck)
    store.close()


def test_restore_pipette_attachment_sets_extension_after_resume(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)
    store.begin_pick_up_tip(
        state_id, "pick-to-restore", "tip_rack", "A1", 59.3, campaign_id=campaign_id,
    )
    store.complete_pick_up_tip("pick-to-restore")

    assert store.resume_fluid_state(state_id, deck_path, deck) == state_id
    pipette = _FakePipette()
    store.restore_pipette_attachment(state_id, pipette)
    assert pipette.extension_mm == pytest.approx(59.3)
    store.close()


def test_restore_pipette_attachment_clears_extension_when_no_tip_attached(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)

    assert store.resume_fluid_state(state_id, deck_path, deck) == state_id
    pipette = _FakePipette()
    pipette.extension_mm = 59.3  # Stale in-memory state from a prior tip.
    store.restore_pipette_attachment(state_id, pipette)
    assert pipette.extension_mm is None
    assert pipette.clear_calls == 1
    store.close()


def test_restore_pipette_attachment_refuses_when_uncertain(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)
    store.begin_pick_up_tip(
        state_id, "uncertain-for-restore", "tip_rack", "A1", 59.3,
        campaign_id=campaign_id,
    )
    store.mark_tip_reconciliation_required(
        "uncertain-for-restore", "process stopped after motion",
    )
    # Directly exercise restore_pipette_attachment's own guard without going
    # through resume_fluid_state (which would already refuse to resume).
    pipette = _FakePipette()
    with pytest.raises(TipStateReconciliationRequiredError):
        store.restore_pipette_attachment(state_id, pipette)
    store.close()


# ── Conflict / version guards ────────────────────────────────────────────────


def test_complete_pick_up_tip_detects_version_conflict(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    store.begin_pick_up_tip(
        state_id, "conflicted-pick", "tip_rack", "A1", 59.3, campaign_id=campaign_id,
    )
    # Simulate an out-of-band mutation of the reserved slot between begin and
    # complete (e.g. a bug or manual DB edit).
    store._conn.execute(
        "UPDATE tip_containers SET version = version + 1 "
        "WHERE fluid_state_id = ? AND rack_key = 'tip_rack' AND slot_id = 'A1'",
        (state_id,),
    )
    store._conn.commit()

    with pytest.raises(TipStateConflictError):
        store.complete_pick_up_tip("conflicted-pick")
    store.close()


def test_complete_pick_up_tip_is_idempotent_when_already_applied(tmp_path):
    deck_path, deck = _write_deck(tmp_path)
    store = DataStore(":memory:")
    state_id = _create_state(store, deck_path, deck)
    campaign_id = _create_linked_campaign(store, state_id)

    store.begin_pick_up_tip(
        state_id, "double-complete", "tip_rack", "A1", 59.3, campaign_id=campaign_id,
    )
    store.complete_pick_up_tip("double-complete")
    store.complete_pick_up_tip("double-complete")  # No-op, must not raise.

    snapshot = store.get_tip_snapshot(state_id)
    assert len(snapshot["operations"]) == 1
    assert _slot(snapshot, "tip_rack", "A1")["status"] == "attached"
    store.close()
