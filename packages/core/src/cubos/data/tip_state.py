"""Durable, deck-associated tip and attached-pipette state for CubOS.

Mirrors ``cubos.data.fluid_state``: static tip-rack geometry (rows, columns,
tip coordinates, tip length) stays in deck YAML; the mutable, durable state of
each physical tip slot and the currently attached pipette tip lives here in
SQLite, hung off the *same* ``fluid_state_sessions`` row a campaign's fluid
state uses (``fluid_state_id``). One durable session therefore carries both
fluids and consumables.

Tip slots move through the same two-phase begin/complete operation journal as
fluid operations: ``started`` (reserved before motion) -> ``applied``
(committed after physical success), or ``reconciliation_required`` when the
physical outcome is uncertain. ``cancelled``/``reconciled`` are the operator
resolution outcomes. Pending tip operations block new fluid operations and
vice versa (checked via :func:`pending_tip_operations` /
``fluid_state.pending_operations``), since only one physical pipetting action
can be in flight for a session at a time.
"""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, Optional, TypedDict

from cubos.deck.labware.tip_rack import TipRack

if TYPE_CHECKING:
    from cubos.deck.deck import Deck
    from cubos.instruments.pipette.interface import PipetteInstrument

_PENDING_STATUSES = ("started", "reconciliation_required")
_TERMINAL_STATUSES = ("applied", "cancelled", "reconciled")
_DEFAULT_PIPETTE_KEY = "pipette"


class TipStateError(ValueError):
    """Base exception for invalid or unavailable tip state."""


class TipStateNotFoundError(TipStateError):
    """Raised when a requested tip slot, operation, or session does not exist."""


class TipStateDeckMismatchError(TipStateError):
    """Raised when a session's persisted tip-rack registry no longer matches the deck."""


class TipStateReconciliationRequiredError(TipStateError):
    """Raised when physical and persisted tip/pipette state may have diverged."""


class TipStateConflictError(TipStateReconciliationRequiredError):
    """Raised when tip/pipette state changed after an operation was planned."""


class TipContainerSnapshot(TypedDict):
    rack_key: str
    slot_id: str
    status: str
    tip_length_mm: float
    version: int
    updated_at: str


class TipOperationSnapshot(TypedDict):
    id: int
    operation_key: str
    operation_type: str
    rack_key: str
    slot_id: str
    tip_extension_mm: float
    status: str
    campaign_id: Optional[int]
    detail: Optional[str]
    created_at: str
    updated_at: str
    applied_at: Optional[str]


class PipetteAttachmentSnapshot(TypedDict):
    pipette_key: str
    rack_key: Optional[str]
    slot_id: Optional[str]
    tip_extension_mm: Optional[float]
    contents_known_empty: bool
    attachment_uncertain: bool
    updated_at: str


class TipRefillSnapshot(TypedDict):
    id: int
    operation_key: str
    rack_key: str
    operator: str
    reason: str
    changed_slots: list[str]
    preserved_slots: list[str]
    created_at: str


class TipStateSnapshot(TypedDict):
    fluid_state_id: int
    containers: list[TipContainerSnapshot]
    operations: list[TipOperationSnapshot]
    refills: list[TipRefillSnapshot]
    pipette: PipetteAttachmentSnapshot


# ── Seeding & resume verification ───────────────────────────────────────────


def seed_tip_state(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    deck: "Deck",
) -> None:
    """Insert one ``tip_containers`` row per tip slot from every deck TipRack.

    Initial status is taken from the rack's ``tip_present`` map at session
    create time only; the DB row is the durable source of truth afterward.
    Insertion order follows each rack's ``tips`` iteration order, which
    ``begin_pick_up_tip`` relies on (via row id order) for next-available
    selection.
    """
    for rack_key, rack in _iter_tip_racks(deck):
        for slot_id in rack.tips:
            present = bool(rack.tip_present.get(slot_id, False))
            status = "available" if present else "consumed"
            connection.execute(
                "INSERT INTO tip_containers (fluid_state_id, rack_key, slot_id, "
                "tip_length_mm, status) VALUES (?, ?, ?, ?, ?)",
                (fluid_state_id, rack_key, slot_id, float(rack.tip_length), status),
            )
    _ensure_pipette_attachment_row(connection, fluid_state_id)


def verify_tip_container_registry(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    deck: "Deck",
) -> None:
    """Verify the persisted tip-rack registry still matches the current deck."""
    expected = {
        (rack_key, slot_id): float(rack.tip_length)
        for rack_key, rack in _iter_tip_racks(deck)
        for slot_id in rack.tips
    }
    actual = {
        (row[0], row[1]): float(row[2])
        for row in connection.execute(
            "SELECT rack_key, slot_id, tip_length_mm FROM tip_containers "
            "WHERE fluid_state_id = ?",
            (fluid_state_id,),
        )
    }
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        mismatched = sorted(
            key for key in set(actual) & set(expected) if actual[key] != expected[key]
        )
        raise TipStateDeckMismatchError(
            f"Tip state {fluid_state_id} tip-rack registry does not match its "
            f"current deck (missing={missing}, unexpected={unexpected}, "
            f"tip_length_mismatch={mismatched})."
        )


def pending_tip_operations(
    connection: sqlite3.Connection,
    fluid_state_id: int,
) -> list[tuple[str, str]]:
    """Return ``(operation_key, status)`` for every pending tip operation."""
    return connection.execute(
        "SELECT operation_key, status FROM tip_operations WHERE fluid_state_id = ? "
        "AND status IN (?, ?) ORDER BY id",
        (fluid_state_id, *_PENDING_STATUSES),
    ).fetchall()


def get_tip_snapshot(
    connection: sqlite3.Connection,
    fluid_state_id: int,
) -> TipStateSnapshot:
    """Return a deterministic JSON-ready snapshot of one session's tip state."""
    with _read_transaction(connection):
        exists = connection.execute(
            "SELECT 1 FROM fluid_state_sessions WHERE id = ?", (fluid_state_id,),
        ).fetchone()
        if exists is None:
            raise TipStateNotFoundError(f"Fluid state {fluid_state_id} not found.")

        container_rows = connection.execute(
            "SELECT rack_key, slot_id, status, tip_length_mm, version, updated_at "
            "FROM tip_containers WHERE fluid_state_id = ? ORDER BY rack_key, id",
            (fluid_state_id,),
        ).fetchall()
        operation_rows = connection.execute(
            "SELECT id, operation_key, operation_type, rack_key, slot_id, "
            "tip_extension_mm, status, campaign_id, detail, created_at, "
            "updated_at, applied_at FROM tip_operations WHERE fluid_state_id = ? "
            "ORDER BY id",
            (fluid_state_id,),
        ).fetchall()
        refill_rows = connection.execute(
            "SELECT id, operation_key, rack_key, operator, reason, "
            "changed_slots_json, preserved_slots_json, created_at "
            "FROM tip_refill_operations WHERE fluid_state_id = ? ORDER BY id",
            (fluid_state_id,),
        ).fetchall()
        pipette_row = _pipette_attachment_row(
            connection, fluid_state_id, _DEFAULT_PIPETTE_KEY,
        )

    containers: list[TipContainerSnapshot] = [
        {
            "rack_key": row[0],
            "slot_id": row[1],
            "status": row[2],
            "tip_length_mm": float(row[3]),
            "version": int(row[4]),
            "updated_at": row[5],
        }
        for row in container_rows
    ]
    operations: list[TipOperationSnapshot] = [
        {
            "id": int(row[0]),
            "operation_key": row[1],
            "operation_type": row[2],
            "rack_key": row[3],
            "slot_id": row[4],
            "tip_extension_mm": float(row[5]),
            "status": row[6],
            "campaign_id": row[7],
            "detail": row[8],
            "created_at": row[9],
            "updated_at": row[10],
            "applied_at": row[11],
        }
        for row in operation_rows
    ]
    refills: list[TipRefillSnapshot] = [
        {
            "id": int(row[0]),
            "operation_key": row[1],
            "rack_key": row[2],
            "operator": row[3],
            "reason": row[4],
            "changed_slots": json.loads(row[5]),
            "preserved_slots": json.loads(row[6]),
            "created_at": row[7],
        }
        for row in refill_rows
    ]
    pipette: PipetteAttachmentSnapshot = {
        "pipette_key": pipette_row["pipette_key"],
        "rack_key": pipette_row["rack_key"],
        "slot_id": pipette_row["slot_id"],
        "tip_extension_mm": (
            float(pipette_row["tip_extension_mm"])
            if pipette_row["tip_extension_mm"] is not None
            else None
        ),
        "contents_known_empty": bool(pipette_row["contents_known_empty"]),
        "attachment_uncertain": bool(pipette_row["attachment_uncertain"]),
        "updated_at": pipette_row["updated_at"],
    }
    return {
        "fluid_state_id": fluid_state_id,
        "containers": containers,
        "operations": operations,
        "refills": refills,
        "pipette": pipette,
    }


# ── pick_up_tip journal ─────────────────────────────────────────────────────


def begin_pick_up_tip(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    operation_key: str,
    rack_key: str,
    slot_id: str | None,
    tip_length_mm: float,
    campaign_id: int | None = None,
) -> tuple[bool, str, float]:
    """Preflight and journal a tip pickup before physical actuation.

    ``slot_id=None`` requests next-available selection within *rack_key*,
    using the durable per-slot status (not the deck's in-memory
    ``tip_present``). Returns ``(should_execute, resolved_slot_id,
    tip_extension_mm)``. ``should_execute=False`` means this exact campaign
    step was already applied and must not be replayed (no second tip is
    picked).
    """
    _validate_operation_key(operation_key)
    extension = _finite_nonnegative(tip_length_mm, "tip_length_mm")
    with _immediate_transaction(connection):
        _require_session(connection, fluid_state_id)
        _require_campaign_link(connection, fluid_state_id, campaign_id)

        existing = _check_existing_tip_operation(
            connection,
            operation_key,
            fluid_state_id=fluid_state_id,
            operation_type="pick_up_tip",
            rack_key=rack_key,
            slot_id=slot_id,
            campaign_id=campaign_id,
        )
        if existing is not None:
            return False, existing["slot_id"], float(existing["tip_extension_mm"])

        _require_no_pending_tip_operation(connection, fluid_state_id, operation_key)
        _require_no_pending_fluid_operation(connection, fluid_state_id, operation_key)

        resolved_slot_id = slot_id
        if resolved_slot_id is None:
            row = connection.execute(
                "SELECT slot_id FROM tip_containers WHERE fluid_state_id = ? "
                "AND rack_key = ? AND status = 'available' ORDER BY id LIMIT 1",
                (fluid_state_id, rack_key),
            ).fetchone()
            if row is None:
                raise TipStateError(
                    f"No available tip in rack {rack_key!r} of state {fluid_state_id}."
                )
            resolved_slot_id = row[0]

        slot = _tip_container_row(connection, fluid_state_id, rack_key, resolved_slot_id)
        if slot["status"] != "available":
            raise TipStateError(
                f"Tip {rack_key}.{resolved_slot_id} is {slot['status']}, not available."
            )
        if not math.isclose(
            float(slot["tip_length_mm"]), extension, rel_tol=1e-9, abs_tol=1e-9,
        ):
            raise TipStateError(
                f"Tip {rack_key}.{resolved_slot_id} tip_length is "
                f"{slot['tip_length_mm']:g} mm, but pickup requested "
                f"{extension:g} mm; deck/session tip-length mismatch."
            )

        cursor = connection.execute(
            "UPDATE tip_containers SET status = 'reserved', version = version + 1, "
            "updated_at = datetime('now') WHERE id = ? AND version = ? "
            "AND status = 'available'",
            (slot["id"], slot["version"]),
        )
        if cursor.rowcount != 1:
            raise TipStateConflictError(
                f"Tip {rack_key}.{resolved_slot_id} changed while reserving it."
            )
        reserved_version = int(slot["version"]) + 1

        try:
            connection.execute(
                "INSERT INTO tip_operations (fluid_state_id, operation_key, "
                "operation_type, rack_key, slot_id, tip_extension_mm, "
                "previous_slot_status, slot_version, status, campaign_id) "
                "VALUES (?, ?, 'pick_up_tip', ?, ?, ?, 'available', ?, 'started', ?)",
                (
                    fluid_state_id,
                    operation_key,
                    rack_key,
                    resolved_slot_id,
                    extension,
                    reserved_version,
                    campaign_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if pending_tip_operations(connection, fluid_state_id):
                raise TipStateReconciliationRequiredError(
                    f"Fluid state {fluid_state_id} already has a pending tip "
                    "operation."
                ) from exc
            raise
        _touch_session(connection, fluid_state_id)
    return True, resolved_slot_id, extension


def complete_pick_up_tip(connection: sqlite3.Connection, operation_key: str) -> None:
    """Atomically apply a started pickup: slot -> attached, pipette attaches."""
    with _immediate_transaction(connection):
        operation = _tip_operation_row(connection, operation_key)
        if operation["operation_type"] != "pick_up_tip":
            raise TipStateError(
                f"Operation {operation_key!r} is {operation['operation_type']}, "
                "not pick_up_tip."
            )
        if operation["status"] == "applied":
            return
        if operation["status"] != "started":
            raise TipStateReconciliationRequiredError(
                f"Operation {operation_key!r} is {operation['status']} and "
                "cannot be applied automatically."
            )
        _apply_pick_up(connection, operation, allowed_statuses=("started",))


# ── drop_tip journal ─────────────────────────────────────────────────────────


def begin_drop_tip(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    operation_key: str,
    campaign_id: int | None = None,
    pipette_key: str = _DEFAULT_PIPETTE_KEY,
) -> tuple[bool, str, str]:
    """Preflight and journal a tip drop before physical actuation.

    The tip being dropped is resolved from the currently attached tip on
    *pipette_key*. Returns ``(should_execute, rack_key, slot_id)``.
    """
    _validate_operation_key(operation_key)
    with _immediate_transaction(connection):
        _require_session(connection, fluid_state_id)
        _require_campaign_link(connection, fluid_state_id, campaign_id)

        row = connection.execute(
            "SELECT fluid_state_id, operation_type, status, rack_key, slot_id, "
            "campaign_id FROM tip_operations WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        if row is not None:
            if int(row[0]) != fluid_state_id:
                raise TipStateError(
                    f"Operation key {operation_key!r} belongs to fluid state {row[0]}."
                )
            if row[1] != "drop_tip":
                raise TipStateError(
                    f"Operation {operation_key!r} is {row[1]}, not drop_tip."
                )
            if row[2] == "applied":
                if row[5] != campaign_id:
                    raise TipStateError(
                        f"Applied operation key {operation_key!r} was reused "
                        "with a different campaign_id."
                    )
                return False, row[3], row[4]
            if row[2] in _PENDING_STATUSES:
                raise TipStateReconciliationRequiredError(
                    f"Operation {operation_key!r} is {row[2]}; reconcile "
                    "physical tip state before continuing."
                )
            raise TipStateError(
                f"Operation {operation_key!r} was operator-resolved as "
                f"{row[2]}; start a new campaign operation rather than "
                "reusing its key."
            )

        _require_no_pending_tip_operation(connection, fluid_state_id, operation_key)
        _require_no_pending_fluid_operation(connection, fluid_state_id, operation_key)

        attachment = _pipette_attachment_row(connection, fluid_state_id, pipette_key)
        if attachment["rack_key"] is None or attachment["slot_id"] is None:
            raise TipStateError(
                f"No tip is currently attached in state {fluid_state_id}; "
                "nothing to drop."
            )
        if attachment["attachment_uncertain"]:
            raise TipStateReconciliationRequiredError(
                f"Pipette attachment for state {fluid_state_id} is uncertain; "
                "reconcile before dropping."
            )
        rack_key, slot_id = attachment["rack_key"], attachment["slot_id"]
        slot = _tip_container_row(connection, fluid_state_id, rack_key, slot_id)
        if slot["status"] != "attached":
            raise TipStateConflictError(
                f"Tip {rack_key}.{slot_id} is {slot['status']}, not attached; "
                "cannot drop."
            )

        cursor = connection.execute(
            "UPDATE tip_containers SET status = 'reserved', version = version + 1, "
            "updated_at = datetime('now') WHERE id = ? AND version = ? "
            "AND status = 'attached'",
            (slot["id"], slot["version"]),
        )
        if cursor.rowcount != 1:
            raise TipStateConflictError(
                f"Tip {rack_key}.{slot_id} changed while reserving it for drop."
            )
        reserved_version = int(slot["version"]) + 1

        try:
            connection.execute(
                "INSERT INTO tip_operations (fluid_state_id, operation_key, "
                "operation_type, rack_key, slot_id, tip_extension_mm, "
                "previous_slot_status, slot_version, status, campaign_id) "
                "VALUES (?, ?, 'drop_tip', ?, ?, ?, 'attached', ?, 'started', ?)",
                (
                    fluid_state_id,
                    operation_key,
                    rack_key,
                    slot_id,
                    float(slot["tip_length_mm"]),
                    reserved_version,
                    campaign_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if pending_tip_operations(connection, fluid_state_id):
                raise TipStateReconciliationRequiredError(
                    f"Fluid state {fluid_state_id} already has a pending tip "
                    "operation."
                ) from exc
            raise
        _touch_session(connection, fluid_state_id)
    return True, rack_key, slot_id


def complete_drop_tip(connection: sqlite3.Connection, operation_key: str) -> None:
    """Atomically apply a started drop: slot -> consumed, pipette clears."""
    with _immediate_transaction(connection):
        operation = _tip_operation_row(connection, operation_key)
        if operation["operation_type"] != "drop_tip":
            raise TipStateError(
                f"Operation {operation_key!r} is {operation['operation_type']}, "
                "not drop_tip."
            )
        if operation["status"] == "applied":
            return
        if operation["status"] != "started":
            raise TipStateReconciliationRequiredError(
                f"Operation {operation_key!r} is {operation['status']} and "
                "cannot be applied automatically."
            )
        _apply_drop(connection, operation, allowed_statuses=("started",))


# ── Reconciliation ───────────────────────────────────────────────────────────


def mark_tip_reconciliation_required(
    connection: sqlite3.Connection,
    operation_key: str,
    detail: str,
) -> None:
    """Mark a non-applied tip operation as requiring operator reconciliation."""
    if not isinstance(detail, str) or not detail.strip():
        raise TipStateError("Reconciliation detail must be a non-empty string.")
    with _immediate_transaction(connection):
        operation = _tip_operation_row(connection, operation_key)
        if operation["status"] in _TERMINAL_STATUSES:
            raise TipStateError(
                f"Terminal operation {operation_key!r} ({operation['status']}) "
                "cannot require reconciliation."
            )
        if operation["status"] == "reconciliation_required":
            return

        cursor = connection.execute(
            "UPDATE tip_operations SET status = 'reconciliation_required', "
            "detail = ?, updated_at = datetime('now') "
            "WHERE operation_key = ? AND status = 'started'",
            (detail, operation_key),
        )
        if cursor.rowcount != 1:
            raise TipStateConflictError(
                f"Operation {operation_key!r} changed while marking "
                "reconciliation."
            )
        # Deliberately does not bump the slot's version: `slot_version` on
        # the operation row is the CAS anchor `_apply_pick_up`/`_apply_drop`/
        # `_revert_tip_operation`/`_reconcile_tip_operation` use to detect
        # out-of-band changes, and marking uncertainty is not itself a state
        # change those need to detect.
        connection.execute(
            "UPDATE tip_containers SET status = 'reconciliation_required', "
            "updated_at = datetime('now') "
            "WHERE fluid_state_id = ? AND rack_key = ? AND slot_id = ?",
            (
                operation["fluid_state_id"],
                operation["rack_key"],
                operation["slot_id"],
            ),
        )
        connection.execute(
            "UPDATE pipette_attachment SET attachment_uncertain = 1, "
            "contents_known_empty = 0, version = version + 1, "
            "updated_at = datetime('now') WHERE fluid_state_id = ?",
            (operation["fluid_state_id"],),
        )
        _touch_session(connection, operation["fluid_state_id"])


def resolve_tip_operation(
    connection: sqlite3.Connection,
    operation_key: str,
    resolution: str,
    *,
    detail: str,
    final_slot_status: str | None = None,
) -> None:
    """Resolve an uncertain tip operation with an auditable operator decision.

    ``resolution`` accepts ``applied``, ``not_applied``/``cancelled``, or
    ``partial``/``reconciled``. ``reconciled`` requires an explicit
    *final_slot_status* of ``available``, ``attached``, or ``consumed``,
    mirroring ``fluid_state.resolve_fluid_operation``'s exact-replacement
    contract.
    """
    if not isinstance(detail, str) or not detail.strip():
        raise TipStateError("Operator resolution detail must be a non-empty string.")
    normalized = {
        "applied": "applied",
        "not_applied": "cancelled",
        "not-applied": "cancelled",
        "cancelled": "cancelled",
        "partial": "reconciled",
        "reconciled": "reconciled",
    }.get(resolution)
    if normalized is None:
        raise TipStateError(
            "resolution must be applied, not_applied/cancelled, or "
            "partial/reconciled."
        )

    with _immediate_transaction(connection):
        operation = _tip_operation_row(connection, operation_key)
        if operation["status"] in _TERMINAL_STATUSES:
            if operation["status"] == normalized:
                return
            raise TipStateError(
                f"Operation {operation_key!r} is already terminal with status "
                f"{operation['status']}."
            )
        if operation["status"] not in _PENDING_STATUSES:
            raise TipStateError(
                f"Operation {operation_key!r} cannot be resolved from status "
                f"{operation['status']}."
            )

        if normalized == "applied":
            if final_slot_status is not None:
                raise TipStateError(
                    "Applied resolution does not accept final_slot_status."
                )
            _apply_tip_operation(connection, operation, allowed_statuses=_PENDING_STATUSES)
            return

        if normalized == "cancelled":
            if final_slot_status is not None:
                raise TipStateError(
                    "Cancelled resolution does not accept final_slot_status."
                )
            _revert_tip_operation(connection, operation, detail=detail)
            return

        if final_slot_status not in ("available", "attached", "consumed"):
            raise TipStateError(
                "Partial reconciliation requires final_slot_status of "
                "'available', 'attached', or 'consumed'."
            )
        _reconcile_tip_operation(
            connection, operation, final_slot_status=final_slot_status, detail=detail,
        )


def refill_tip_rack(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    operation_key: str,
    rack_key: str,
    *,
    operator: str,
    reason: str,
    pipette_bare_confirmed: bool,
) -> TipRefillSnapshot:
    """Record an operator-confirmed refill of one rack without touching fluids.

    A refill is a state reconciliation event, not a physical pipette action.
    It makes consumed slots available and, only with explicit bare-pipette
    confirmation, clears an attached tip in the durable state. Pending tip
    operations and uncertain/reserved slots are never silently cleared.
    """
    _validate_operation_key(operation_key)
    if not isinstance(rack_key, str) or not rack_key.strip():
        raise TipStateError("rack_key must be a non-empty string.")
    if not isinstance(operator, str) or not operator.strip():
        raise TipStateError("operator must be a non-empty string.")
    if not isinstance(reason, str) or not reason.strip():
        raise TipStateError("reason must be a non-empty string.")
    if not isinstance(pipette_bare_confirmed, bool):
        raise TipStateError("pipette_bare_confirmed must be true or false.")

    with _immediate_transaction(connection):
        _require_session(connection, fluid_state_id)
        owner = connection.execute(
            "SELECT fluid_state_id FROM tip_refill_operations "
            "WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        if owner is not None and int(owner[0]) != fluid_state_id:
            raise TipStateConflictError(
                f"Refill operation key {operation_key!r} belongs to fluid state "
                f"{owner[0]}, not {fluid_state_id}."
            )
        existing = connection.execute(
            "SELECT id, operation_key, rack_key, operator, reason, "
            "changed_slots_json, preserved_slots_json, created_at "
            "FROM tip_refill_operations WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        if existing is not None:
            if existing[2] != rack_key or existing[3] != operator or existing[4] != reason:
                raise TipStateError(
                    f"Refill operation key {operation_key!r} was reused with "
                    "different parameters."
                )
            return _tip_refill_snapshot(existing)

        pending = pending_tip_operations(connection, fluid_state_id)
        if pending:
            details = ", ".join(f"{key} ({status})" for key, status in pending)
            raise TipStateReconciliationRequiredError(
                f"Fluid state {fluid_state_id} cannot refill rack {rack_key!r} "
                f"while tip operations require reconciliation: {details}."
            )

        rows = connection.execute(
            "SELECT id, slot_id, status, version FROM tip_containers "
            "WHERE fluid_state_id = ? AND rack_key = ? ORDER BY id",
            (fluid_state_id, rack_key),
        ).fetchall()
        if not rows:
            raise TipStateError(
                f"Tip rack {rack_key!r} is not registered in state {fluid_state_id}."
            )

        pipette = _pipette_attachment_row(connection, fluid_state_id)
        attached_slots = [row[1] for row in rows if row[2] == "attached"]
        if len(attached_slots) > 1:
            raise TipStateConflictError(
                f"Tip rack {rack_key!r} has multiple attached slots: {attached_slots}."
            )
        pointer = (pipette["rack_key"], pipette["slot_id"])
        if pipette["attachment_uncertain"] and not pipette_bare_confirmed:
            raise TipStateReconciliationRequiredError(
                f"Pipette attachment for fluid state {fluid_state_id} is uncertain; "
                "confirm the pipette is bare before refilling."
            )
        if pointer[0] is not None and pointer[0] != rack_key:
            raise TipStateConflictError(
                f"Pipette tip is attached from rack {pointer[0]!r}; refill that "
                "rack first or reconcile the attachment."
            )
        if attached_slots and pointer != (rack_key, attached_slots[0]):
            raise TipStateConflictError(
                "Tip slot and pipette attachment disagree; reconcile before refill."
            )
        if pointer[1] is not None and pointer[1] not in attached_slots:
            raise TipStateConflictError(
                "Pipette attachment points to a slot that is not attached."
            )

        blocked = [row[1] for row in rows if row[2] in {"reserved", "reconciliation_required"}]
        if blocked:
            raise TipStateReconciliationRequiredError(
                f"Tip slots require reconciliation before refill: {blocked}."
            )

        changed_slots: list[str] = []
        preserved_slots: list[str] = []
        for row in rows:
            slot_id, status = row[1], row[2]
            if status == "attached" and not pipette_bare_confirmed:
                preserved_slots.append(slot_id)
                continue
            if status != "available":
                cursor = connection.execute(
                    "UPDATE tip_containers SET status = 'available', "
                    "version = version + 1, updated_at = datetime('now') "
                    "WHERE id = ? AND version = ? AND status = ?",
                    (row[0], row[3], status),
                )
                if cursor.rowcount != 1:
                    raise TipStateConflictError(
                        f"Tip {rack_key}.{slot_id} changed while refilling."
                    )
                changed_slots.append(slot_id)

        if pipette_bare_confirmed:
            _set_pipette_attachment(
                connection,
                fluid_state_id,
                rack_key=None,
                slot_id=None,
                tip_extension_mm=None,
                contents_known_empty=True,
                attachment_uncertain=False,
            )

        changed_json = json.dumps(changed_slots, separators=(",", ":"))
        preserved_json = json.dumps(preserved_slots, separators=(",", ":"))
        connection.execute(
            "INSERT INTO tip_refill_operations "
            "(fluid_state_id, operation_key, rack_key, operator, reason, "
            "changed_slots_json, preserved_slots_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                fluid_state_id,
                operation_key,
                rack_key,
                operator.strip(),
                reason.strip(),
                changed_json,
                preserved_json,
            ),
        )
        _touch_session(connection, fluid_state_id)
        row = connection.execute(
            "SELECT id, operation_key, rack_key, operator, reason, "
            "changed_slots_json, preserved_slots_json, created_at "
            "FROM tip_refill_operations WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        assert row is not None
        return _tip_refill_snapshot(row)


# ── Resume-time pipette restore ──────────────────────────────────────────────


def restore_pipette_attachment(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    pipette: "PipetteInstrument",
    pipette_key: str = _DEFAULT_PIPETTE_KEY,
) -> None:
    """Restore or refuse the pipette's attached-tip extension after resume.

    Sets the pipette's attached-tip extension from the persisted session when
    attachment is certain, or clears it when no tip is attached. Raises
    :class:`TipStateReconciliationRequiredError` when attachment is uncertain
    -- callers must block liquid handling until it is reconciled.
    """
    with _immediate_transaction(connection):
        _ensure_pipette_attachment_row(connection, fluid_state_id, pipette_key)
        row = _pipette_attachment_row(connection, fluid_state_id, pipette_key)
    if row["attachment_uncertain"]:
        raise TipStateReconciliationRequiredError(
            f"Pipette attachment for fluid state {fluid_state_id} is "
            "uncertain; reconcile before liquid handling."
        )
    if row["rack_key"] is not None and row["slot_id"] is not None:
        pipette.set_attached_tip_extension(float(row["tip_extension_mm"]))
    else:
        pipette.clear_attached_tip_extension()


# ── Internal helpers ─────────────────────────────────────────────────────────


def _iter_tip_racks(deck: "Deck") -> Iterator[tuple[str, TipRack]]:
    """Yield top-level deck TipRack labware by deck key, in key order."""
    for key, labware in sorted(deck.labware.items()):
        if isinstance(labware, TipRack):
            yield key, labware


def _apply_tip_operation(
    connection: sqlite3.Connection,
    operation: dict[str, Any],
    *,
    allowed_statuses: tuple[str, ...],
) -> None:
    if operation["operation_type"] == "pick_up_tip":
        _apply_pick_up(connection, operation, allowed_statuses=allowed_statuses)
    elif operation["operation_type"] == "drop_tip":
        _apply_drop(connection, operation, allowed_statuses=allowed_statuses)
    else:  # pragma: no cover - guarded by schema CHECK constraint
        raise TipStateError(
            f"Unsupported tip operation type {operation['operation_type']!r}."
        )


def _apply_pick_up(
    connection: sqlite3.Connection,
    operation: dict[str, Any],
    *,
    allowed_statuses: tuple[str, ...],
) -> None:
    fluid_state_id = int(operation["fluid_state_id"])
    slot = _tip_container_row(
        connection, fluid_state_id, operation["rack_key"], operation["slot_id"],
    )
    if int(slot["version"]) != int(operation["slot_version"]):
        raise TipStateConflictError(
            f"Tip {operation['rack_key']}.{operation['slot_id']} changed after "
            f"operation {operation['operation_key']!r} began; reconciliation "
            "required."
        )
    cursor = connection.execute(
        "UPDATE tip_containers SET status = 'attached', version = version + 1, "
        "updated_at = datetime('now') WHERE id = ? AND version = ?",
        (slot["id"], slot["version"]),
    )
    if cursor.rowcount != 1:
        raise TipStateConflictError(
            f"Tip {operation['rack_key']}.{operation['slot_id']} changed while "
            f"applying operation {operation['operation_key']!r}."
        )
    _set_pipette_attachment(
        connection,
        fluid_state_id,
        rack_key=operation["rack_key"],
        slot_id=operation["slot_id"],
        tip_extension_mm=float(operation["tip_extension_mm"]),
        contents_known_empty=True,
        attachment_uncertain=False,
    )
    _set_tip_operation_terminal(
        connection, operation, status="applied", detail=None,
        allowed_statuses=allowed_statuses,
    )


def _apply_drop(
    connection: sqlite3.Connection,
    operation: dict[str, Any],
    *,
    allowed_statuses: tuple[str, ...],
) -> None:
    fluid_state_id = int(operation["fluid_state_id"])
    slot = _tip_container_row(
        connection, fluid_state_id, operation["rack_key"], operation["slot_id"],
    )
    if int(slot["version"]) != int(operation["slot_version"]):
        raise TipStateConflictError(
            f"Tip {operation['rack_key']}.{operation['slot_id']} changed after "
            f"operation {operation['operation_key']!r} began; reconciliation "
            "required."
        )
    cursor = connection.execute(
        "UPDATE tip_containers SET status = 'consumed', version = version + 1, "
        "updated_at = datetime('now') WHERE id = ? AND version = ?",
        (slot["id"], slot["version"]),
    )
    if cursor.rowcount != 1:
        raise TipStateConflictError(
            f"Tip {operation['rack_key']}.{operation['slot_id']} changed while "
            f"applying operation {operation['operation_key']!r}."
        )
    _set_pipette_attachment(
        connection,
        fluid_state_id,
        rack_key=None,
        slot_id=None,
        tip_extension_mm=None,
        contents_known_empty=True,
        attachment_uncertain=False,
    )
    _set_tip_operation_terminal(
        connection, operation, status="applied", detail=None,
        allowed_statuses=allowed_statuses,
    )


def _revert_tip_operation(
    connection: sqlite3.Connection,
    operation: dict[str, Any],
    *,
    detail: str,
) -> None:
    fluid_state_id = int(operation["fluid_state_id"])
    slot = _tip_container_row(
        connection, fluid_state_id, operation["rack_key"], operation["slot_id"],
    )
    cursor = connection.execute(
        "UPDATE tip_containers SET status = ?, version = version + 1, "
        "updated_at = datetime('now') WHERE id = ? AND version = ?",
        (operation["previous_slot_status"], slot["id"], slot["version"]),
    )
    if cursor.rowcount != 1:
        raise TipStateConflictError(
            f"Tip {operation['rack_key']}.{operation['slot_id']} changed while "
            f"cancelling operation {operation['operation_key']!r}."
        )
    connection.execute(
        "UPDATE pipette_attachment SET attachment_uncertain = 0, "
        "version = version + 1, updated_at = datetime('now') "
        "WHERE fluid_state_id = ?",
        (fluid_state_id,),
    )
    _set_tip_operation_terminal(
        connection, operation, status="cancelled", detail=detail,
        allowed_statuses=_PENDING_STATUSES,
    )


def _reconcile_tip_operation(
    connection: sqlite3.Connection,
    operation: dict[str, Any],
    *,
    final_slot_status: str,
    detail: str,
) -> None:
    fluid_state_id = int(operation["fluid_state_id"])
    slot = _tip_container_row(
        connection, fluid_state_id, operation["rack_key"], operation["slot_id"],
    )
    cursor = connection.execute(
        "UPDATE tip_containers SET status = ?, version = version + 1, "
        "updated_at = datetime('now') WHERE id = ? AND version = ?",
        (final_slot_status, slot["id"], slot["version"]),
    )
    if cursor.rowcount != 1:
        raise TipStateConflictError(
            f"Tip {operation['rack_key']}.{operation['slot_id']} changed while "
            f"reconciling operation {operation['operation_key']!r}."
        )
    if final_slot_status == "attached":
        _set_pipette_attachment(
            connection,
            fluid_state_id,
            rack_key=operation["rack_key"],
            slot_id=operation["slot_id"],
            tip_extension_mm=float(operation["tip_extension_mm"]),
            contents_known_empty=True,
            attachment_uncertain=False,
        )
    else:
        _set_pipette_attachment(
            connection,
            fluid_state_id,
            rack_key=None,
            slot_id=None,
            tip_extension_mm=None,
            contents_known_empty=True,
            attachment_uncertain=False,
        )
    _set_tip_operation_terminal(
        connection, operation, status="reconciled", detail=detail,
        allowed_statuses=_PENDING_STATUSES,
    )


def _check_existing_tip_operation(
    connection: sqlite3.Connection,
    operation_key: str,
    *,
    fluid_state_id: int,
    operation_type: str,
    rack_key: str,
    slot_id: str | None,
    campaign_id: int | None,
) -> dict[str, Any] | None:
    cursor = connection.execute(
        "SELECT fluid_state_id, operation_type, status, rack_key, slot_id, "
        "tip_extension_mm, campaign_id FROM tip_operations WHERE operation_key = ?",
        (operation_key,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    if int(row[0]) != fluid_state_id:
        raise TipStateError(
            f"Operation key {operation_key!r} belongs to fluid state {row[0]}."
        )
    exact = (
        row[1] == operation_type
        and row[3] == rack_key
        and (slot_id is None or row[4] == slot_id)
        and row[6] == campaign_id
    )
    if row[2] == "applied" and exact:
        return {"slot_id": row[4], "tip_extension_mm": row[5]}
    if row[2] == "applied":
        raise TipStateError(
            f"Applied operation key {operation_key!r} was reused with "
            f"different {operation_type} parameters."
        )
    if row[2] in _PENDING_STATUSES:
        raise TipStateReconciliationRequiredError(
            f"Operation {operation_key!r} is {row[2]}; reconcile physical tip "
            "state before continuing."
        )
    raise TipStateError(
        f"Operation {operation_key!r} was operator-resolved as {row[2]}; start "
        "a new campaign operation rather than reusing its key."
    )


def _require_no_pending_tip_operation(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    operation_key: str,
) -> None:
    pending = pending_tip_operations(connection, fluid_state_id)
    if pending:
        details = ", ".join(f"{key} ({status})" for key, status in pending)
        raise TipStateReconciliationRequiredError(
            f"Fluid state {fluid_state_id} cannot start tip operation "
            f"{operation_key!r} while these tip operations require physical "
            f"reconciliation: {details}."
        )


def _require_no_pending_fluid_operation(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    operation_key: str,
) -> None:
    from . import cap_state as _cap_state
    from . import fluid_state as _fluid_state

    pending = [
        *_fluid_state.pending_operations(connection, fluid_state_id),
        *_cap_state.pending_cap_operations(connection, fluid_state_id),
    ]
    if pending:
        details = ", ".join(f"{key} ({status})" for key, status in pending)
        raise TipStateReconciliationRequiredError(
            f"Fluid state {fluid_state_id} cannot start tip operation "
            f"{operation_key!r} while these fluid/cap operations require "
            f"physical reconciliation: {details}."
        )


def _require_session(connection: sqlite3.Connection, fluid_state_id: int) -> None:
    row = connection.execute(
        "SELECT 1 FROM fluid_state_sessions WHERE id = ?", (fluid_state_id,),
    ).fetchone()
    if row is None:
        raise TipStateNotFoundError(f"Fluid state {fluid_state_id} not found.")


def _require_campaign_link(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    campaign_id: int | None,
) -> None:
    if campaign_id is None:
        raise TipStateError(
            "Durable tip operations require a campaign_id linked to the same "
            "fluid state."
        )
    row = connection.execute(
        "SELECT fluid_state_id FROM campaigns WHERE id = ?", (campaign_id,),
    ).fetchone()
    if row is None:
        raise TipStateError(f"Campaign {campaign_id} not found.")
    if row[0] is None:
        raise TipStateError(f"Campaign {campaign_id} is not linked to a fluid state.")
    if int(row[0]) != fluid_state_id:
        raise TipStateError(
            f"Campaign {campaign_id} is linked to fluid state {row[0]}, not "
            f"{fluid_state_id}."
        )


def _ensure_pipette_attachment_row(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    pipette_key: str = _DEFAULT_PIPETTE_KEY,
) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO pipette_attachment "
        "(fluid_state_id, pipette_key, contents_known_empty, attachment_uncertain) "
        "VALUES (?, ?, 1, 0)",
        (fluid_state_id, pipette_key),
    )


def _tip_container_row(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    rack_key: str,
    slot_id: str,
) -> dict[str, Any]:
    cursor = connection.execute(
        "SELECT id, rack_key, slot_id, tip_length_mm, status, version "
        "FROM tip_containers WHERE fluid_state_id = ? AND rack_key = ? "
        "AND slot_id = ?",
        (fluid_state_id, rack_key, slot_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise TipStateError(
            f"Tip slot {rack_key}.{slot_id} is not registered in state "
            f"{fluid_state_id}."
        )
    return dict(zip((column[0] for column in cursor.description), row))


def _tip_operation_row(
    connection: sqlite3.Connection,
    operation_key: str,
) -> dict[str, Any]:
    cursor = connection.execute(
        "SELECT id, fluid_state_id, operation_key, operation_type, rack_key, "
        "slot_id, tip_extension_mm, previous_slot_status, slot_version, "
        "status, campaign_id, detail FROM tip_operations WHERE operation_key = ?",
        (operation_key,),
    )
    row = cursor.fetchone()
    if row is None:
        raise TipStateNotFoundError(f"Tip operation {operation_key!r} not found.")
    return dict(zip((column[0] for column in cursor.description), row))


def _tip_refill_snapshot(row: Any) -> TipRefillSnapshot:
    return {
        "id": int(row[0]),
        "operation_key": row[1],
        "rack_key": row[2],
        "operator": row[3],
        "reason": row[4],
        "changed_slots": json.loads(row[5]),
        "preserved_slots": json.loads(row[6]),
        "created_at": row[7],
    }


def _pipette_attachment_row(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    pipette_key: str = _DEFAULT_PIPETTE_KEY,
) -> dict[str, Any]:
    _ensure_pipette_attachment_row(connection, fluid_state_id, pipette_key)
    cursor = connection.execute(
        "SELECT id, pipette_key, rack_key, slot_id, tip_extension_mm, "
        "contents_known_empty, attachment_uncertain, version, updated_at "
        "FROM pipette_attachment WHERE fluid_state_id = ? AND pipette_key = ?",
        (fluid_state_id, pipette_key),
    )
    row = cursor.fetchone()
    return dict(zip((column[0] for column in cursor.description), row))


def _set_pipette_attachment(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    *,
    rack_key: str | None,
    slot_id: str | None,
    tip_extension_mm: float | None,
    contents_known_empty: bool,
    attachment_uncertain: bool,
    pipette_key: str = _DEFAULT_PIPETTE_KEY,
) -> None:
    _ensure_pipette_attachment_row(connection, fluid_state_id, pipette_key)
    connection.execute(
        "UPDATE pipette_attachment SET rack_key = ?, slot_id = ?, "
        "tip_extension_mm = ?, contents_known_empty = ?, "
        "attachment_uncertain = ?, version = version + 1, "
        "updated_at = datetime('now') WHERE fluid_state_id = ? AND pipette_key = ?",
        (
            rack_key,
            slot_id,
            tip_extension_mm,
            int(contents_known_empty),
            int(attachment_uncertain),
            fluid_state_id,
            pipette_key,
        ),
    )


def _set_tip_operation_terminal(
    connection: sqlite3.Connection,
    operation: dict[str, Any],
    *,
    status: str,
    detail: str | None,
    allowed_statuses: tuple[str, ...],
) -> None:
    placeholders = ", ".join("?" for _ in allowed_statuses)
    cursor = connection.execute(
        "UPDATE tip_operations SET status = ?, detail = COALESCE(?, detail), "
        "applied_at = CASE WHEN ? = 'applied' THEN datetime('now') ELSE "
        "applied_at END, updated_at = datetime('now') WHERE id = ? AND status "
        f"IN ({placeholders})",
        (status, detail, status, operation["id"], *allowed_statuses),
    )
    if cursor.rowcount != 1:
        raise TipStateConflictError(
            f"Operation {operation['operation_key']!r} changed while "
            "resolving it."
        )
    _touch_session(connection, operation["fluid_state_id"])


def _touch_session(connection: sqlite3.Connection, fluid_state_id: int) -> None:
    connection.execute(
        "UPDATE fluid_state_sessions SET updated_at = datetime('now') WHERE id = ?",
        (fluid_state_id,),
    )


def _validate_operation_key(operation_key: str) -> None:
    if not isinstance(operation_key, str) or not operation_key.strip():
        raise TipStateError("operation_key must be a non-empty string.")


def _finite_nonnegative(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TipStateError(f"{label} must be a finite non-negative number.")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise TipStateError(f"{label} must be a finite non-negative number.")
    return result


@contextmanager
def _read_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Pin all queries in a logical snapshot to one SQLite read view."""
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        yield
    finally:
        if owns_transaction:
            connection.rollback()


@contextmanager
def _immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Acquire SQLite's writer reservation before reading mutable tip state."""
    if connection.in_transaction:
        raise TipStateError(
            "Tip-state mutation cannot start inside an existing SQLite "
            "transaction; commit or roll back that transaction first."
        )
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


__all__ = [
    "PipetteAttachmentSnapshot",
    "TipContainerSnapshot",
    "TipOperationSnapshot",
    "TipRefillSnapshot",
    "TipStateConflictError",
    "TipStateDeckMismatchError",
    "TipStateError",
    "TipStateNotFoundError",
    "TipStateReconciliationRequiredError",
    "TipStateSnapshot",
    "begin_drop_tip",
    "begin_pick_up_tip",
    "complete_drop_tip",
    "complete_pick_up_tip",
    "get_tip_snapshot",
    "mark_tip_reconciliation_required",
    "pending_tip_operations",
    "resolve_tip_operation",
    "refill_tip_rack",
    "restore_pipette_attachment",
    "seed_tip_state",
    "verify_tip_container_registry",
]
