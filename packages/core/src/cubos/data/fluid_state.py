"""Durable, deck-associated fluid state for CubOS protocol runs.

The physical deck definition remains in deck YAML. This module stores that
resolved YAML as provenance, a semantic compatibility descriptor, a
render-friendly layout, and the mutable contents of every addressable well or
vial in SQLite.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, TypedDict

import yaml

from cubos.deck.deck import Deck, DeckLabwareTarget
from cubos.deck.labware.vial import Vial
from cubos.deck.labware.vial_grid import VialGrid
from cubos.deck.labware.well_plate import WellPlate
from cubos.deck.loader import resolve_load_names
from cubos.yaml_utils import load_yaml_file

logger = logging.getLogger(__name__)

FLUID_STATE_API_VERSION = 1
_VOLUME_TOLERANCE_UL = 1e-6
_PENDING_OPERATION_STATUSES = ("started", "reconciliation_required")
_TERMINAL_OPERATION_STATUSES = ("applied", "cancelled", "reconciled")


class FluidStateError(ValueError):
    """Base exception for invalid or unavailable fluid state."""


class FluidStateNotFoundError(FluidStateError):
    """Raised when a requested fluid-state session does not exist."""


class FluidStateDeckMismatchError(FluidStateError):
    """Raised when a session is resumed with a different resolved deck."""


class FluidStateReconciliationRequiredError(FluidStateError):
    """Raised when physical and persisted state may have diverged."""


class FluidStateConflictError(FluidStateReconciliationRequiredError):
    """Raised when container state changed after an operation was planned."""


class FluidContainerSnapshot(TypedDict):
    labware_key: str
    location_id: str
    labware_type: str
    capacity_ul: float
    working_volume_ul: float
    current_volume_ul: float
    composition: dict[str, float]
    version: int
    updated_at: str


class FluidOperationSnapshot(TypedDict):
    id: int
    operation_key: str
    operation_type: str
    source: str
    destination: str
    volume_ul: float
    composition: dict[str, float]
    parameters: dict[str, Any]
    status: str
    campaign_id: Optional[int]
    detail: Optional[str]
    created_at: str
    updated_at: str
    applied_at: Optional[str]


class FluidStateSnapshot(TypedDict):
    id: int
    deck_path: str
    deck_fingerprint: str
    deck_descriptor: dict[str, Any]
    deck_snapshot: dict[str, Any]
    layout: dict[str, Any]
    label: Optional[str]
    created_at: str
    updated_at: str
    containers: list[FluidContainerSnapshot]
    operations: list[FluidOperationSnapshot]


class FluidStateSummary(TypedDict):
    id: int
    label: Optional[str]
    deck_path: str
    deck_fingerprint: str
    created_at: str
    updated_at: str
    container_count: int
    operation_count: int


class FluidReplacementEndpoint(TypedDict):
    volume_ul: float
    composition: dict[str, float]


class FluidReplacementState(TypedDict):
    source: FluidReplacementEndpoint
    destination: FluidReplacementEndpoint


def load_initial_fluids(path: str | Path) -> dict[str, dict[str, Any]]:
    """Load and validate a fluid-state seed YAML.

    The accepted shape is::

        fluids:
          source_vial:
            volume_ul: 1000
            composition:
              water: 1000

    ``composition`` may be omitted, in which case the volume is labelled
    ``unknown``.
    """
    source = Path(path).expanduser()
    try:
        raw = load_yaml_file(source)
    except (OSError, yaml.YAMLError) as exc:
        raise FluidStateError(
            f"Cannot load initial fluids YAML from {source}: {exc}"
        ) from exc
    if not isinstance(raw, Mapping) or set(raw) != {"fluids"}:
        raise FluidStateError(
            "Initial fluids YAML must contain exactly one top-level `fluids` mapping."
        )
    fluids = raw["fluids"]
    if not isinstance(fluids, Mapping):
        raise FluidStateError("Initial fluids `fluids` value must be a mapping.")
    return _normalize_initial_fluids(fluids)


def load_replacement_state(path: str | Path) -> FluidReplacementState:
    """Load exact measured endpoint state for partial reconciliation.

    The YAML must contain exactly ``source`` and ``destination``. Each endpoint
    must contain exactly ``volume_ul`` and ``composition``; component volumes
    must be finite, non-negative, and sum to the endpoint volume.
    """
    source = Path(path).expanduser()
    try:
        raw = load_yaml_file(source)
    except (OSError, yaml.YAMLError) as exc:
        raise FluidStateError(
            f"Cannot load replacement-state YAML from {source}: {exc}"
        ) from exc
    if not isinstance(raw, Mapping) or set(raw) != {"source", "destination"}:
        raise FluidStateError(
            "Replacement-state YAML must contain exactly `source` and "
            "`destination` mappings."
        )

    normalized: dict[str, FluidReplacementEndpoint] = {}
    for endpoint in ("source", "destination"):
        state = raw[endpoint]
        if not isinstance(state, Mapping) or set(state) != {
            "volume_ul",
            "composition",
        }:
            raise FluidStateError(
                f"Replacement-state `{endpoint}` must contain exactly "
                "`volume_ul` and `composition`."
            )
        volume = _finite_nonnegative(
            state["volume_ul"],
            f"replacement-state {endpoint}.volume_ul",
        )
        composition = _normalize_composition(state["composition"], volume)
        normalized[endpoint] = {
            "volume_ul": volume,
            "composition": composition,
        }
    return FluidReplacementState(
        source=normalized["source"],
        destination=normalized["destination"],
    )


def create_fluid_state(
    connection: sqlite3.Connection,
    deck_path: str | Path,
    deck: Deck,
    *,
    label: str | None = None,
    initial_fluids: Mapping[str, Any] | None = None,
) -> int:
    """Create one durable state session and register its volume labware."""
    path, snapshot_json = _resolved_deck_provenance(deck_path)
    descriptors = list(_iter_volume_labware(deck))
    descriptor_json = _canonical_json(
        _canonical_deck_descriptor(deck, descriptors)
    )
    fingerprint = hashlib.sha256(descriptor_json.encode("utf-8")).hexdigest()
    layout_json = _canonical_json({
        "containers": [_layout_entry(key, labware) for key, labware in descriptors]
    })
    normalized_initial = _normalize_initial_fluids(initial_fluids or {})
    canonical_initial = _canonicalize_initial_fluids(deck, normalized_initial)

    with connection:
        cursor = connection.execute(
            "INSERT INTO fluid_state_sessions "
            "(deck_path, deck_fingerprint, deck_descriptor_json, "
            "deck_snapshot_json, layout_json, label) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (path, fingerprint, descriptor_json, snapshot_json, layout_json, label),
        )
        fluid_state_id = int(cursor.lastrowid)
        for labware_key, labware in descriptors:
            _insert_container_rows(connection, fluid_state_id, labware_key, labware)

        # Tip/attached-pipette state hangs off this same session (one durable
        # session carries both fluids and consumables).
        from . import tip_state
        tip_state.seed_tip_state(connection, fluid_state_id, deck)

        # Durable per-vial cap state hangs off this same session too, seeded
        # from every vial/vial-grid position that declares `capped` in YAML
        # (see cubos.data.cap_state; vials without `capped` set are simply
        # not capper-managed and are skipped).
        from . import cap_state
        cap_state.seed_cap_state(connection, fluid_state_id, deck)

        for (labware_key, location_id), definition in canonical_initial.items():
            _seed_fluid_row(
                connection,
                fluid_state_id,
                labware_key,
                location_id,
                definition["volume_ul"],
                definition["composition"],
            )
        connection.execute(
            "UPDATE fluid_state_sessions SET updated_at = datetime('now') "
            "WHERE id = ?",
            (fluid_state_id,),
        )
    return fluid_state_id


def resume_fluid_state(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    deck_path: str | Path,
    deck: Deck,
) -> int:
    """Validate a persisted session against the current deck and resume it."""
    row = connection.execute(
        "SELECT deck_fingerprint, deck_descriptor_json "
        "FROM fluid_state_sessions WHERE id = ?",
        (fluid_state_id,),
    ).fetchone()
    if row is None:
        raise FluidStateNotFoundError(f"Fluid state {fluid_state_id} not found.")
    # The source YAML remains provenance and must still be readable/resolvable,
    # but its path, comments, display labels, and unused metadata are not the
    # compatibility boundary.
    _resolved_deck_provenance(deck_path)
    stored_descriptor_json = row[1]
    if not stored_descriptor_json or stored_descriptor_json == "{}":
        raise FluidStateDeckMismatchError(
            f"Fluid state {fluid_state_id} predates canonical deck descriptors "
            "and cannot be resumed safely; create a new fluid state."
        )
    stored_fingerprint = hashlib.sha256(
        stored_descriptor_json.encode("utf-8")
    ).hexdigest()
    if row[0] != stored_fingerprint:
        raise FluidStateError(
            f"Fluid state {fluid_state_id} has a corrupt deck descriptor fingerprint."
        )
    descriptors = list(_iter_volume_labware(deck))
    current_descriptor_json = _canonical_json(
        _canonical_deck_descriptor(deck, descriptors)
    )
    current_fingerprint = hashlib.sha256(
        current_descriptor_json.encode("utf-8")
    ).hexdigest()
    if row[0] != current_fingerprint or stored_descriptor_json != current_descriptor_json:
        raise FluidStateDeckMismatchError(
            f"Fluid state {fluid_state_id} belongs to deck fingerprint {row[0]}, "
            f"but the supplied deck resolves to {current_fingerprint}."
        )

    _verify_container_registry(connection, fluid_state_id, descriptors)

    # Tip/attached-pipette registry and reconciliation state share this
    # session; both must be checked before it can be safely resumed.
    from . import tip_state
    tip_state.verify_tip_container_registry(connection, fluid_state_id, deck)

    # Cap-state registry shares this session too.
    from . import cap_state
    cap_state.verify_cap_container_registry(connection, fluid_state_id, deck)

    pending = [
        *_pending_operations(connection, fluid_state_id),
        *tip_state.pending_tip_operations(connection, fluid_state_id),
        *cap_state.pending_cap_operations(connection, fluid_state_id),
    ]
    if pending:
        details = ", ".join(f"{key} ({status})" for key, status in pending)
        raise FluidStateReconciliationRequiredError(
            f"Fluid state {fluid_state_id} has operations requiring physical "
            f"reconciliation before resume: {details}."
        )
    return fluid_state_id


def seed_fluid(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    target: str | DeckLabwareTarget,
    volume_ul: float,
    composition: Mapping[str, float] | None = None,
) -> None:
    """Replace a container's current volume/composition with an explicit seed."""
    volume = _finite_nonnegative(volume_ul, "volume_ul")
    normalized = _normalize_composition(composition, volume)
    with _immediate_transaction(connection):
        _require_state(connection, fluid_state_id)
        pending = _pending_operations(connection, fluid_state_id)
        if pending:
            details = ", ".join(f"{key} ({status})" for key, status in pending)
            raise FluidStateReconciliationRequiredError(
                f"Fluid state {fluid_state_id} cannot be seeded while operations "
                f"require reconciliation: {details}."
            )
        labware_key, location_id = _target_parts_for_state(
            connection, fluid_state_id, target,
        )
        _seed_fluid_row(
            connection, fluid_state_id, labware_key, location_id, volume, normalized,
        )
        connection.execute(
            "UPDATE fluid_state_sessions SET updated_at = datetime('now') WHERE id = ?",
            (fluid_state_id,),
        )


def begin_fluid_transfer(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    operation_key: str,
    source: str | DeckLabwareTarget,
    destination: str | DeckLabwareTarget,
    volume_ul: float,
    campaign_id: int | None = None,
) -> bool:
    """Preflight and journal a transfer before physical actuation.

    Returns ``True`` when a new ``started`` operation was inserted and hardware
    should execute. Returns ``False`` when the same operation is already
    ``applied`` and must be skipped. Pending/reconciliation operations raise.
    """
    _validate_operation_key(operation_key)
    volume = _finite_positive(volume_ul, "volume_ul")
    with _immediate_transaction(connection):
        _require_state(connection, fluid_state_id)
        _require_campaign_link(connection, fluid_state_id, campaign_id)
        source_key, source_location = _target_parts_for_state(
            connection, fluid_state_id, source,
        )
        destination_key, destination_location = _target_parts_for_state(
            connection, fluid_state_id, destination,
        )
        if (source_key, source_location) == (destination_key, destination_location):
            raise FluidStateError("Source and destination must be different containers.")

        if _check_existing_operation(
            connection,
            operation_key,
            fluid_state_id=fluid_state_id,
            operation_type="transfer",
            source=(source_key, source_location),
            destination=(destination_key, destination_location),
            volume_ul=volume,
            campaign_id=campaign_id,
            parameters={},
        ):
            return False
        _require_no_pending_operation(connection, fluid_state_id, operation_key)

        source_row = _container_row(
            connection, fluid_state_id, source_key, source_location,
        )
        destination_row = _container_row(
            connection, fluid_state_id, destination_key, destination_location,
        )
        source_volume = float(source_row["current_volume_ul"])
        destination_volume = float(destination_row["current_volume_ul"])
        if source_volume + _VOLUME_TOLERANCE_UL < volume:
            raise FluidStateError(
                f"Cannot transfer {volume:g} uL from "
                f"{_format_target(source_key, source_location)}; only "
                f"{source_volume:g} uL is available."
            )
        projected_destination = destination_volume + volume
        _validate_replacement_volume(
            destination_row,
            projected_destination,
            _format_target(destination_key, destination_location),
            action="Transfer",
        )
        source_composition = _decode_composition(
            source_row["composition_json"], source_volume,
            target=_format_target(source_key, source_location),
        )
        transfer_composition = _proportional_composition(
            source_composition, source_volume, volume,
        )
        _insert_started_operation(
            connection,
            fluid_state_id=fluid_state_id,
            operation_key=operation_key,
            operation_type="transfer",
            source=(source_key, source_location),
            destination=(destination_key, destination_location),
            volume_ul=volume,
            composition=transfer_composition,
            parameters={},
            source_version=int(source_row["version"]),
            destination_version=int(destination_row["version"]),
            campaign_id=campaign_id,
        )
    return True


def begin_fluid_mix(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    operation_key: str,
    target: str | DeckLabwareTarget,
    volume_ul: float,
    repetitions: int,
    speed: float,
    height: float = 0.0,
    campaign_id: int | None = None,
) -> bool:
    """Preflight and journal a net-zero mix before physical actuation."""
    _validate_operation_key(operation_key)
    volume = _finite_positive(volume_ul, "volume_ul")
    if isinstance(repetitions, bool) or not isinstance(repetitions, int) or repetitions <= 0:
        raise FluidStateError("repetitions must be a positive integer.")
    mix_speed = _finite_positive(speed, "speed")
    mix_height = _finite_number(height, "height")
    parameters = {
        "height": mix_height,
        "repetitions": repetitions,
        "speed": mix_speed,
    }

    with _immediate_transaction(connection):
        _require_state(connection, fluid_state_id)
        _require_campaign_link(connection, fluid_state_id, campaign_id)
        labware_key, location_id = _target_parts_for_state(
            connection, fluid_state_id, target,
        )
        canonical_target = (labware_key, location_id)
        if _check_existing_operation(
            connection,
            operation_key,
            fluid_state_id=fluid_state_id,
            operation_type="mix",
            source=canonical_target,
            destination=canonical_target,
            volume_ul=volume,
            campaign_id=campaign_id,
            parameters=parameters,
        ):
            return False
        _require_no_pending_operation(connection, fluid_state_id, operation_key)

        container = _container_row(
            connection, fluid_state_id, labware_key, location_id,
        )
        current_volume = float(container["current_volume_ul"])
        if current_volume + _VOLUME_TOLERANCE_UL < volume:
            raise FluidStateError(
                f"Cannot mix {volume:g} uL in "
                f"{_format_target(labware_key, location_id)}; only "
                f"{current_volume:g} uL is available."
            )
        composition = _decode_composition(
            container["composition_json"],
            current_volume,
            target=_format_target(labware_key, location_id),
        )
        mixed_composition = _proportional_composition(
            composition, current_volume, volume,
        )
        _insert_started_operation(
            connection,
            fluid_state_id=fluid_state_id,
            operation_key=operation_key,
            operation_type="mix",
            source=canonical_target,
            destination=canonical_target,
            volume_ul=volume,
            composition=mixed_composition,
            parameters=parameters,
            source_version=int(container["version"]),
            destination_version=int(container["version"]),
            campaign_id=campaign_id,
        )
    return True


def complete_fluid_transfer(
    connection: sqlite3.Connection,
    operation_key: str,
) -> None:
    """Atomically apply a started transfer to source/destination state."""
    _complete_fluid_operation(connection, operation_key, expected_type="transfer")


def complete_fluid_mix(
    connection: sqlite3.Connection,
    operation_key: str,
) -> None:
    """Mark a successfully actuated net-zero mix as applied."""
    _complete_fluid_operation(connection, operation_key, expected_type="mix")


def _complete_fluid_operation(
    connection: sqlite3.Connection,
    operation_key: str,
    *,
    expected_type: str,
) -> None:
    with _immediate_transaction(connection):
        operation = _operation_row(connection, operation_key)
        if operation["operation_type"] != expected_type:
            raise FluidStateError(
                f"Operation {operation_key!r} is {operation['operation_type']}, "
                f"not {expected_type}."
            )
        if operation["status"] == "applied":
            return
        if operation["status"] != "started":
            raise FluidStateReconciliationRequiredError(
                f"Operation {operation_key!r} is {operation['status']} and "
                "cannot be applied automatically."
            )
        _apply_planned_operation(connection, operation, allowed_statuses=("started",))


def resolve_fluid_operation(
    connection: sqlite3.Connection,
    operation_key: str,
    resolution: str,
    *,
    detail: str,
    source_volume_ul: float | None = None,
    source_composition: Mapping[str, float] | None = None,
    destination_volume_ul: float | None = None,
    destination_composition: Mapping[str, float] | None = None,
) -> None:
    """Resolve an uncertain operation with an auditable operator decision."""
    if not isinstance(detail, str) or not detail.strip():
        raise FluidStateError("Operator resolution detail must be a non-empty string.")
    normalized_resolution = {
        "applied": "applied",
        "not_applied": "cancelled",
        "not-applied": "cancelled",
        "cancelled": "cancelled",
        "partial": "reconciled",
        "reconciled": "reconciled",
    }.get(resolution)
    if normalized_resolution is None:
        raise FluidStateError(
            "resolution must be applied, not_applied/cancelled, or "
            "partial/reconciled."
        )

    replacement_values = (
        source_volume_ul,
        source_composition,
        destination_volume_ul,
        destination_composition,
    )
    with _immediate_transaction(connection):
        operation = _operation_row(connection, operation_key)
        if operation["status"] in _TERMINAL_OPERATION_STATUSES:
            if operation["status"] == normalized_resolution:
                return
            raise FluidStateError(
                f"Operation {operation_key!r} is already terminal with status "
                f"{operation['status']}."
            )
        if operation["status"] not in _PENDING_OPERATION_STATUSES:
            raise FluidStateError(
                f"Operation {operation_key!r} cannot be resolved from status "
                f"{operation['status']}."
            )

        if normalized_resolution == "applied":
            if any(value is not None for value in replacement_values):
                raise FluidStateError(
                    "Applied resolution does not accept replacement container state."
                )
            _apply_planned_operation(
                connection,
                operation,
                allowed_statuses=_PENDING_OPERATION_STATUSES,
                detail=detail,
            )
            return

        if normalized_resolution == "cancelled":
            if any(value is not None for value in replacement_values):
                raise FluidStateError(
                    "Cancelled resolution does not accept replacement container state."
                )
            _set_operation_terminal(
                connection,
                operation,
                status="cancelled",
                detail=detail,
                allowed_statuses=_PENDING_OPERATION_STATUSES,
            )
            return

        if any(value is None for value in replacement_values):
            raise FluidStateError(
                "Partial reconciliation requires exact source and destination "
                "volumes and compositions."
            )
        source_volume = _finite_nonnegative(source_volume_ul, "source_volume_ul")
        destination_volume = _finite_nonnegative(
            destination_volume_ul, "destination_volume_ul",
        )
        source_normalized = _normalize_composition(source_composition, source_volume)
        destination_normalized = _normalize_composition(
            destination_composition, destination_volume,
        )
        _replace_operation_container_state(
            connection,
            operation,
            source_volume=source_volume,
            source_composition=source_normalized,
            destination_volume=destination_volume,
            destination_composition=destination_normalized,
        )
        _set_operation_terminal(
            connection,
            operation,
            status="reconciled",
            detail=detail,
            allowed_statuses=_PENDING_OPERATION_STATUSES,
        )


def mark_fluid_reconciliation_required(
    connection: sqlite3.Connection,
    operation_key: str,
    detail: str,
) -> None:
    """Mark a non-applied operation as requiring operator reconciliation."""
    if not isinstance(detail, str) or not detail.strip():
        raise FluidStateError("Reconciliation detail must be a non-empty string.")
    with _immediate_transaction(connection):
        row = connection.execute(
            "SELECT fluid_state_id, status FROM fluid_operations "
            "WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        if row is None:
            raise FluidStateNotFoundError(
                f"Fluid operation {operation_key!r} not found."
            )
        if row[1] in _TERMINAL_OPERATION_STATUSES:
            raise FluidStateError(
                f"Terminal operation {operation_key!r} ({row[1]}) cannot require "
                "reconciliation."
            )
        if row[1] == "reconciliation_required":
            return

        cursor = connection.execute(
            "UPDATE fluid_operations SET status = 'reconciliation_required', "
            "detail = ?, updated_at = datetime('now') "
            "WHERE operation_key = ? AND status = 'started'",
            (detail, operation_key),
        )
        if cursor.rowcount != 1:
            raise FluidStateConflictError(
                f"Operation {operation_key!r} changed while marking reconciliation."
            )
        connection.execute(
            "UPDATE fluid_state_sessions SET updated_at = datetime('now') WHERE id = ?",
            (row[0],),
        )


def list_fluid_states(
    connection: sqlite3.Connection,
) -> list[FluidStateSummary]:
    """Return deterministic summaries for every fluid-state session."""
    with _read_transaction(connection):
        rows = connection.execute(
            "SELECT state.id, state.label, state.deck_path, "
            "state.deck_fingerprint, state.created_at, state.updated_at, "
            "(SELECT COUNT(*) FROM fluid_containers AS container "
            " WHERE container.fluid_state_id = state.id), "
            "(SELECT COUNT(*) FROM fluid_operations AS operation "
            " WHERE operation.fluid_state_id = state.id) "
            "FROM fluid_state_sessions AS state ORDER BY state.id DESC"
        ).fetchall()

    return [
        {
            "id": int(row[0]),
            "label": row[1],
            "deck_path": row[2],
            "deck_fingerprint": row[3],
            "created_at": row[4],
            "updated_at": row[5],
            "container_count": int(row[6]),
            "operation_count": int(row[7]),
        }
        for row in rows
    ]


def get_fluid_snapshot(
    connection: sqlite3.Connection,
    fluid_state_id: int,
) -> FluidStateSnapshot:
    """Return a deterministic JSON-ready snapshot of one fluid-state session."""
    with _read_transaction(connection):
        state = connection.execute(
            "SELECT id, deck_path, deck_fingerprint, deck_descriptor_json, "
            "deck_snapshot_json, layout_json, label, created_at, updated_at "
            "FROM fluid_state_sessions WHERE id = ?",
            (fluid_state_id,),
        ).fetchone()
        if state is None:
            raise FluidStateNotFoundError(f"Fluid state {fluid_state_id} not found.")

        container_rows = connection.execute(
            "SELECT labware_key, location_id, labware_type, capacity_ul, "
            "working_volume_ul, current_volume_ul, composition_json, version, "
            "updated_at FROM fluid_containers WHERE fluid_state_id = ? "
            "ORDER BY labware_key, location_id",
            (fluid_state_id,),
        ).fetchall()
        operation_rows = connection.execute(
            "SELECT id, operation_key, operation_type, source_labware_key, "
            "source_location_id, destination_labware_key, destination_location_id, "
            "volume_ul, composition_json, parameters_json, status, campaign_id, "
            "detail, created_at, updated_at, applied_at FROM fluid_operations "
            "WHERE fluid_state_id = ? ORDER BY id",
            (fluid_state_id,),
        ).fetchall()

    containers: list[FluidContainerSnapshot] = []
    for row in container_rows:
        volume = float(row[5])
        containers.append({
            "labware_key": row[0],
            "location_id": row[1],
            "labware_type": row[2],
            "capacity_ul": float(row[3]),
            "working_volume_ul": float(row[4]),
            "current_volume_ul": volume,
            "composition": _decode_composition(
                row[6], volume, target=_format_target(row[0], row[1]),
            ),
            "version": int(row[7]),
            "updated_at": row[8],
        })

    operations: list[FluidOperationSnapshot] = []
    for row in operation_rows:
        volume = float(row[7])
        operations.append({
            "id": int(row[0]),
            "operation_key": row[1],
            "operation_type": row[2],
            "source": _format_target(row[3], row[4]),
            "destination": _format_target(row[5], row[6]),
            "volume_ul": volume,
            "composition": _decode_composition(row[8], volume, target=row[1]),
            "parameters": _decode_parameters(row[9], target=row[1]),
            "status": row[10],
            "campaign_id": row[11],
            "detail": row[12],
            "created_at": row[13],
            "updated_at": row[14],
            "applied_at": row[15],
        })

    return {
        "id": int(state[0]),
        "deck_path": state[1],
        "deck_fingerprint": state[2],
        "deck_descriptor": json.loads(state[3]),
        "deck_snapshot": json.loads(state[4]),
        "layout": json.loads(state[5]),
        "label": state[6],
        "created_at": state[7],
        "updated_at": state[8],
        "containers": containers,
        "operations": operations,
    }


def get_fluid_container(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    labware_key: str,
    location_id: str,
) -> FluidContainerSnapshot:
    """Return a single container's current durable state.

    Lightweight sibling of :func:`get_fluid_snapshot` for callers (transfer
    preflight) that need one container's volume/composition without paying
    for the whole session snapshot.
    """
    with _read_transaction(connection):
        _require_state(connection, fluid_state_id)
        row = connection.execute(
            "SELECT labware_key, location_id, labware_type, capacity_ul, "
            "working_volume_ul, current_volume_ul, composition_json, version, "
            "updated_at FROM fluid_containers WHERE fluid_state_id = ? AND "
            "labware_key = ? AND location_id = ?",
            (fluid_state_id, labware_key, location_id),
        ).fetchone()
    if row is None:
        raise FluidStateError(
            f"Fluid target {_format_target(labware_key, location_id)!r} is not "
            f"registered in state {fluid_state_id}."
        )
    volume = float(row[5])
    return {
        "labware_key": row[0],
        "location_id": row[1],
        "labware_type": row[2],
        "capacity_ul": float(row[3]),
        "working_volume_ul": float(row[4]),
        "current_volume_ul": volume,
        "composition": _decode_composition(
            row[6], volume, target=_format_target(row[0], row[1]),
        ),
        "version": int(row[7]),
        "updated_at": row[8],
    }


def _resolved_deck_provenance(deck_path: str | Path) -> tuple[str, str]:
    """Return the resolved source YAML as provenance, not compatibility state."""
    path = Path(deck_path).expanduser().resolve()
    try:
        raw = load_yaml_file(path)
    except (OSError, yaml.YAMLError) as exc:
        raise FluidStateError(f"Cannot load deck YAML from {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise FluidStateError(f"Deck YAML at {path} must contain a mapping.")
    try:
        resolved = resolve_load_names(raw)
    except Exception as exc:
        raise FluidStateError(
            f"Cannot resolve deck labware definitions from {path}: {exc}"
        ) from exc
    snapshot_json = _canonical_json(resolved)
    return str(path), snapshot_json


def _canonical_deck_descriptor(
    deck: Deck,
    descriptors: list[tuple[str, WellPlate | Vial | VialGrid]],
) -> dict[str, Any]:
    """Build the semantic resume boundary from loaded runtime labware.

    Canonical identity, exact coordinates, volume limits, and every alias that
    changes how a volume target resolves are included. Display labels, model
    names, source paths, and YAML presentation deliberately do not invalidate
    resumable liquid state.
    """
    containers: list[dict[str, Any]] = []
    for labware_key, labware in descriptors:
        for row in _container_descriptor_rows(labware_key, labware):
            containers.append(row)
    aliases = dict(deck.target_aliases)
    for labware_key, labware in descriptors:
        if isinstance(labware, VialGrid):
            for alias, canonical_id in labware.aliases.items():
                aliases.setdefault(
                    f"{labware_key}.{alias}",
                    f"{labware_key}.{canonical_id}",
                )
    volume_keys = {key for key, _labware in descriptors}
    aliases = {
        alias: canonical
        for alias, canonical in aliases.items()
        if canonical.split(".", 1)[0] in volume_keys
    }
    return {
        "containers": containers,
        "target_aliases": dict(sorted(aliases.items())),
    }


def _container_descriptor_rows(
    labware_key: str,
    labware: WellPlate | Vial | VialGrid,
) -> list[dict[str, Any]]:
    if isinstance(labware, WellPlate):
        return [
            {
                "labware_key": labware_key,
                "location_id": location_id,
                "labware_type": "well_plate",
                "coordinate": {
                    "x": float(coordinate.x),
                    "y": float(coordinate.y),
                    "z": float(coordinate.z),
                },
                "capacity_ul": float(labware.capacity_ul),
                "working_volume_ul": float(labware.working_volume_ul),
            }
            for location_id, coordinate in sorted(
                labware.wells.items(), key=lambda item: _location_sort_key(item[0])
            )
        ]
    if isinstance(labware, Vial):
        return [{
            "labware_key": labware_key,
            "location_id": "",
            "labware_type": "vial",
            "coordinate": {
                "x": float(labware.location.x),
                "y": float(labware.location.y),
                "z": float(labware.location.z),
            },
            "capacity_ul": float(labware.capacity_ul),
            "working_volume_ul": float(labware.working_volume_ul),
            # Role/solution identity participates in the resume boundary:
            # automatic stock/waste selection (Feature 05) depends on it, so
            # a deck edit that reassigns a container's role/solution must
            # invalidate an old fluid-state session exactly like a moved
            # coordinate or changed volume limit does.
            "role": labware.role,
            "solution": labware.solution,
            "allowed_solutions": (
                sorted(labware.allowed_solutions)
                if labware.allowed_solutions is not None else None
            ),
            # Capped identity participates in the resume boundary exactly
            # like role/solution: it seeds cubos.data.cap_state at session
            # creation (see create_fluid_state below), so a deck edit that
            # changes it must invalidate an old fluid-state session.
            "capped": labware.capped,
        }]
    if isinstance(labware, VialGrid):
        return [
            {
                "labware_key": labware_key,
                "location_id": location_id,
                "labware_type": "vial_grid",
                "coordinate": {
                    "x": float(vial.location.x),
                    "y": float(vial.location.y),
                    "z": float(vial.location.z),
                },
                "capacity_ul": float(vial.capacity_ul),
                "working_volume_ul": float(vial.working_volume_ul),
                "role": vial.role,
                "solution": vial.solution,
                "allowed_solutions": (
                    sorted(vial.allowed_solutions)
                    if vial.allowed_solutions is not None else None
                ),
                "capped": vial.capped,
            }
            for location_id, vial in sorted(
                labware.vials.items(), key=lambda item: _location_sort_key(item[0])
            )
        ]
    raise TypeError(f"Unsupported fluid labware: {type(labware).__name__}")


def _verify_container_registry(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    descriptors: list[tuple[str, WellPlate | Vial | VialGrid]],
) -> None:
    expected = {
        (row["labware_key"], row["location_id"]): (
            row["labware_type"],
            row["capacity_ul"],
            row["working_volume_ul"],
        )
        for key, labware in descriptors
        for row in _container_descriptor_rows(key, labware)
    }
    actual = {
        (row[0], row[1]): (row[2], float(row[3]), float(row[4]))
        for row in connection.execute(
            "SELECT labware_key, location_id, labware_type, capacity_ul, "
            "working_volume_ul FROM fluid_containers WHERE fluid_state_id = ?",
            (fluid_state_id,),
        )
    }
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        mismatched = sorted(
            key for key in set(actual) & set(expected) if actual[key] != expected[key]
        )
        raise FluidStateDeckMismatchError(
            f"Fluid state {fluid_state_id} container registry does not match its "
            f"canonical deck (missing={missing}, unexpected={unexpected}, "
            f"metadata_mismatch={mismatched})."
        )


def _iter_volume_labware(deck: Deck):
    """Yield trackable labware using the deck's canonical flat identities."""
    for key, labware in sorted(deck.volume_labware.items()):
        if isinstance(labware, WellPlate):
            if (
                labware.capacity_ul is not None
                and labware.working_volume_ul is not None
            ):
                yield key, labware
        elif isinstance(labware, (Vial, VialGrid)):
            yield key, labware


def _layout_entry(
    labware_key: str,
    labware: WellPlate | Vial | VialGrid,
) -> dict[str, Any]:
    if isinstance(labware, WellPlate):
        labware_type = "well_plate"
        locations = [
            {
                "location_id": location_id,
                "x": float(coordinate.x),
                "y": float(coordinate.y),
                "z": float(coordinate.z),
            }
            for location_id, coordinate in sorted(
                labware.wells.items(), key=lambda item: _location_sort_key(item[0])
            )
        ]
        geometry = {
            "length": _optional_float(labware.length),
            "width": _optional_float(labware.width),
            "height": _optional_float(labware.height),
            "well_depth": _optional_float(labware.well_depth),
            # Read out of the open attribute bag, staying None when the
            # plate does not record a diameter. This payload is a cross-repo
            # contract consumed by Zoo, so no new keys are introduced
            # alongside the populated one.
            "diameter": labware.well_attribute_float("diameter"),
        }
        capacity_ul = float(labware.capacity_ul)
        working_volume_ul = float(labware.working_volume_ul)
    elif isinstance(labware, Vial):
        labware_type = "vial"
        locations = [{
            "location_id": "",
            "x": float(labware.location.x),
            "y": float(labware.location.y),
            "z": float(labware.location.z),
        }]
        geometry = {
            "length": _optional_float(labware.diameter),
            "width": _optional_float(labware.diameter),
            "height": _optional_float(labware.height),
            "well_depth": None,
            "diameter": _optional_float(labware.diameter),
        }
        capacity_ul = float(labware.capacity_ul)
        working_volume_ul = float(labware.working_volume_ul)
    elif isinstance(labware, VialGrid):
        labware_type = "vial_grid"
        locations = [
            {
                "location_id": location_id,
                "x": float(vial.location.x),
                "y": float(vial.location.y),
                "z": float(vial.location.z),
            }
            for location_id, vial in sorted(
                labware.vials.items(),
                key=lambda item: _location_sort_key(item[0]),
            )
        ]
        diameters = {vial.diameter for vial in labware.vials.values()}
        heights = {vial.height for vial in labware.vials.values()}
        capacities = {vial.capacity_ul for vial in labware.vials.values()}
        working_volumes = {
            vial.working_volume_ul for vial in labware.vials.values()
        }
        diameter = (
            _optional_float(next(iter(diameters)))
            if len(diameters) == 1
            else None
        )
        geometry = {
            "length": diameter,
            "width": diameter,
            "height": (
                _optional_float(next(iter(heights)))
                if len(heights) == 1
                else None
            ),
            "well_depth": None,
            "diameter": diameter,
        }
        capacity_ul = (
            float(next(iter(capacities))) if len(capacities) == 1 else None
        )
        working_volume_ul = (
            float(next(iter(working_volumes)))
            if len(working_volumes) == 1
            else None
        )
    else:  # pragma: no cover - guarded by _iter_volume_labware
        raise TypeError(f"Unsupported fluid labware: {type(labware).__name__}")
    return {
        "labware_key": labware_key,
        "labware_type": labware_type,
        "name": labware.name,
        "model_name": labware.model_name,
        "geometry": geometry,
        "capacity_ul": capacity_ul,
        "working_volume_ul": working_volume_ul,
        "locations": locations,
    }


def _insert_container_rows(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    labware_key: str,
    labware: WellPlate | Vial | VialGrid,
) -> None:
    if isinstance(labware, WellPlate):
        rows = (
            (
                location_id,
                "well_plate",
                labware.capacity_ul,
                labware.working_volume_ul,
            )
            for location_id in sorted(labware.wells, key=_location_sort_key)
        )
    elif isinstance(labware, Vial):
        rows = (("", "vial", labware.capacity_ul, labware.working_volume_ul),)
    elif isinstance(labware, VialGrid):
        rows = (
            (
                location_id,
                "vial_grid",
                vial.capacity_ul,
                vial.working_volume_ul,
            )
            for location_id, vial in sorted(
                labware.vials.items(),
                key=lambda item: _location_sort_key(item[0]),
            )
        )
    else:  # pragma: no cover - guarded by _iter_volume_labware
        raise TypeError(f"Unsupported fluid labware: {type(labware).__name__}")

    for location_id, labware_type, capacity_ul, working_volume_ul in rows:
        connection.execute(
            "INSERT INTO fluid_containers ("
            "fluid_state_id, labware_key, location_id, labware_type, capacity_ul, "
            "working_volume_ul, current_volume_ul, composition_json) "
            "VALUES (?, ?, ?, ?, ?, ?, 0.0, '{}')",
            (
                fluid_state_id,
                labware_key,
                location_id,
                labware_type,
                float(capacity_ul),
                float(working_volume_ul),
            ),
        )


def _resolve_deck_target(deck: Deck, target: str) -> DeckLabwareTarget:
    if not isinstance(target, str) or not target.strip():
        raise FluidStateError("Fluid target must be a non-empty deck target string.")
    try:
        return deck.resolve_labware_target(target)
    except KeyError as exc:
        raise FluidStateError(f"Unknown fluid target {target!r}: {exc}") from exc


def _canonical_target(target: DeckLabwareTarget) -> tuple[str, str]:
    if isinstance(target.labware, WellPlate):
        if target.location_id is None:
            raise FluidStateError(
                f"Well plate target {target.labware_key!r} must include a well ID."
            )
        if target.location_id not in target.labware.wells:
            raise FluidStateError(
                f"Unknown well {target.location_id!r} on {target.labware_key!r}."
            )
        return target.labware_key, target.location_id
    if isinstance(target.labware, Vial):
        if target.location_id not in (None, "", target.labware.name):
            raise FluidStateError(
                f"Vial target {target.labware_key!r} does not have location "
                f"{target.location_id!r}."
            )
        return target.labware_key, ""
    if isinstance(target.labware, VialGrid):
        if target.location_id is None:
            raise FluidStateError(
                f"Vial grid target {target.labware_key!r} must include a position ID."
            )
        try:
            canonical_location_id = target.labware.canonicalize_location_id(
                target.location_id
            )
        except KeyError as exc:
            raise FluidStateError(
                f"Unknown vial-grid position {target.location_id!r} on "
                f"{target.labware_key!r}."
            ) from exc
        return target.labware_key, canonical_location_id
    raise FluidStateError(
        f"Target {target.labware_key!r} is {type(target.labware).__name__}, "
        "not volume-bearing WellPlate, Vial, or VialGrid labware."
    )


def _target_parts_for_state(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    target: str | DeckLabwareTarget,
) -> tuple[str, str]:
    if isinstance(target, DeckLabwareTarget):
        labware_key, location_id = _canonical_target(target)
        _container_row(connection, fluid_state_id, labware_key, location_id)
        return labware_key, location_id
    if not isinstance(target, str) or not target.strip():
        raise FluidStateError("Fluid target must be a non-empty string.")

    exact = connection.execute(
        "SELECT labware_key, location_id FROM fluid_containers "
        "WHERE fluid_state_id = ? AND labware_key = ? AND location_id = ''",
        (fluid_state_id, target),
    ).fetchone()
    if exact is not None:
        return exact[0], exact[1]
    if "." in target:
        labware_key, location_id = target.rsplit(".", 1)
        row = connection.execute(
            "SELECT labware_key, location_id FROM fluid_containers "
            "WHERE fluid_state_id = ? AND labware_key = ? AND location_id = ?",
            (fluid_state_id, labware_key, location_id),
        ).fetchone()
        if row is not None:
            return row[0], row[1]
    raise FluidStateError(
        f"Fluid target {target!r} is not registered in state {fluid_state_id}."
    )


def _seed_fluid_row(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    labware_key: str,
    location_id: str,
    volume_ul: float,
    composition: Mapping[str, float],
) -> None:
    row = _container_row(connection, fluid_state_id, labware_key, location_id)
    capacity = float(row["capacity_ul"])
    if volume_ul > capacity + _VOLUME_TOLERANCE_UL:
        raise FluidStateError(
            f"Seed for {_format_target(labware_key, location_id)} is {volume_ul:g} uL, "
            f"above its {capacity:g} uL capacity."
        )
    working = float(row["working_volume_ul"])
    if volume_ul > working + _VOLUME_TOLERANCE_UL:
        logger.warning(
            "Seed for %s exceeds working volume: %.6g uL > %.6g uL.",
            _format_target(labware_key, location_id), volume_ul, working,
        )
    connection.execute(
        "UPDATE fluid_containers SET current_volume_ul = ?, composition_json = ?, "
        "version = version + 1, updated_at = datetime('now') WHERE id = ?",
        (volume_ul, _canonical_json(dict(composition)), row["id"]),
    )


def _container_row(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    labware_key: str,
    location_id: str,
) -> dict[str, Any]:
    cursor = connection.execute(
        "SELECT id, capacity_ul, working_volume_ul, current_volume_ul, "
        "composition_json, version FROM fluid_containers "
        "WHERE fluid_state_id = ? AND labware_key = ? AND location_id = ?",
        (fluid_state_id, labware_key, location_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise FluidStateError(
            f"Fluid target {_format_target(labware_key, location_id)!r} is not "
            f"registered in state {fluid_state_id}."
        )
    return dict(zip((column[0] for column in cursor.description), row))


def _operation_row(
    connection: sqlite3.Connection,
    operation_key: str,
) -> dict[str, Any]:
    cursor = connection.execute(
        "SELECT id, fluid_state_id, operation_key, operation_type, source_labware_key, "
        "source_location_id, "
        "destination_labware_key, destination_location_id, volume_ul, "
        "composition_json, parameters_json, source_version, destination_version, "
        "status, campaign_id, detail "
        "FROM fluid_operations WHERE operation_key = ?",
        (operation_key,),
    )
    row = cursor.fetchone()
    if row is None:
        raise FluidStateNotFoundError(f"Fluid operation {operation_key!r} not found.")
    return dict(zip((column[0] for column in cursor.description), row))


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
            # This context is deliberately query-only. Rollback closes the read
            # transaction without ever committing incidental caller state.
            connection.rollback()


@contextmanager
def _immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Acquire SQLite's writer reservation before reading mutable fluid state."""
    if connection.in_transaction:
        raise FluidStateError(
            "Fluid-state mutation cannot start inside an existing SQLite "
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


def _validate_operation_key(operation_key: str) -> None:
    if not isinstance(operation_key, str) or not operation_key.strip():
        raise FluidStateError("operation_key must be a non-empty string.")


def _require_campaign_link(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    campaign_id: int | None,
) -> None:
    if campaign_id is None:
        raise FluidStateError(
            "Durable fluid operations require a campaign_id linked to the "
            "same fluid state."
        )
    row = connection.execute(
        "SELECT fluid_state_id FROM campaigns WHERE id = ?",
        (campaign_id,),
    ).fetchone()
    if row is None:
        raise FluidStateError(f"Campaign {campaign_id} not found.")
    if row[0] is None:
        raise FluidStateError(
            f"Campaign {campaign_id} is not linked to a fluid state."
        )
    if int(row[0]) != fluid_state_id:
        raise FluidStateError(
            f"Campaign {campaign_id} is linked to fluid state {row[0]}, "
            f"not {fluid_state_id}."
        )


def _check_existing_operation(
    connection: sqlite3.Connection,
    operation_key: str,
    *,
    fluid_state_id: int,
    operation_type: str,
    source: tuple[str, str],
    destination: tuple[str, str],
    volume_ul: float,
    campaign_id: int | None,
    parameters: Mapping[str, Any],
) -> bool:
    cursor = connection.execute(
        "SELECT fluid_state_id, operation_type, status, source_labware_key, "
        "source_location_id, destination_labware_key, destination_location_id, "
        "volume_ul, campaign_id, parameters_json FROM fluid_operations "
        "WHERE operation_key = ?",
        (operation_key,),
    )
    row = cursor.fetchone()
    if row is None:
        return False
    if int(row[0]) != fluid_state_id:
        raise FluidStateError(
            f"Operation key {operation_key!r} belongs to fluid state {row[0]}."
        )
    expected_targets = (*source, *destination)
    exact = (
        row[1] == operation_type
        and tuple(row[3:7]) == expected_targets
        and math.isclose(
            float(row[7]),
            volume_ul,
            rel_tol=1e-12,
            abs_tol=_VOLUME_TOLERANCE_UL,
        )
        and row[8] == campaign_id
        and _decode_parameters(row[9], target=operation_key) == dict(parameters)
    )
    if row[2] == "applied" and exact:
        return True
    if row[2] == "applied":
        raise FluidStateError(
            f"Applied operation key {operation_key!r} was reused with different "
            f"{operation_type} parameters."
        )
    if row[2] in _PENDING_OPERATION_STATUSES:
        raise FluidStateReconciliationRequiredError(
            f"Operation {operation_key!r} is {row[2]}; reconcile physical liquid "
            "state before continuing."
        )
    raise FluidStateError(
        f"Operation {operation_key!r} was operator-resolved as {row[2]}; start a "
        "new campaign operation rather than reusing its key."
    )


def _require_no_pending_operation(
    connection: sqlite3.Connection,
    fluid_state_id: int,
    operation_key: str,
) -> None:
    # Tip and cap operations share this session's single-physical-action-at-
    # a-time journal: a pending pick_up_tip/drop_tip/decap/cap blocks new
    # fluid operations exactly like a pending fluid operation blocks new tip
    # operations (see tip_state._require_no_pending_fluid_operation) and cap
    # operations (see cap_state._require_no_pending_fluid_operation).
    from . import cap_state, tip_state

    pending = [
        *_pending_operations(connection, fluid_state_id),
        *tip_state.pending_tip_operations(connection, fluid_state_id),
        *cap_state.pending_cap_operations(connection, fluid_state_id),
    ]
    if pending:
        details = ", ".join(f"{key} ({status})" for key, status in pending)
        raise FluidStateReconciliationRequiredError(
            f"Fluid state {fluid_state_id} cannot start operation "
            f"{operation_key!r} while these operations require physical "
            f"reconciliation: {details}."
        )


def _insert_started_operation(
    connection: sqlite3.Connection,
    *,
    fluid_state_id: int,
    operation_key: str,
    operation_type: str,
    source: tuple[str, str],
    destination: tuple[str, str],
    volume_ul: float,
    composition: Mapping[str, float],
    parameters: Mapping[str, Any],
    source_version: int,
    destination_version: int,
    campaign_id: int | None,
) -> None:
    try:
        connection.execute(
            "INSERT INTO fluid_operations ("
            "fluid_state_id, operation_key, operation_type, source_labware_key, "
            "source_location_id, destination_labware_key, destination_location_id, "
            "volume_ul, composition_json, parameters_json, source_version, "
            "destination_version, status, campaign_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'started', ?)",
            (
                fluid_state_id,
                operation_key,
                operation_type,
                source[0],
                source[1],
                destination[0],
                destination[1],
                volume_ul,
                _canonical_json(dict(composition)),
                _canonical_json(dict(parameters)),
                source_version,
                destination_version,
                campaign_id,
            ),
        )
    except sqlite3.IntegrityError as exc:
        if _pending_operations(connection, fluid_state_id):
            raise FluidStateReconciliationRequiredError(
                f"Fluid state {fluid_state_id} already has a pending operation."
            ) from exc
        raise
    connection.execute(
        "UPDATE fluid_state_sessions SET updated_at = datetime('now') WHERE id = ?",
        (fluid_state_id,),
    )


def _apply_planned_operation(
    connection: sqlite3.Connection,
    operation: Mapping[str, Any],
    *,
    allowed_statuses: tuple[str, ...],
    detail: str | None = None,
) -> None:
    fluid_state_id = int(operation["fluid_state_id"])
    source_row = _container_row(
        connection,
        fluid_state_id,
        operation["source_labware_key"],
        operation["source_location_id"],
    )
    destination_row = _container_row(
        connection,
        fluid_state_id,
        operation["destination_labware_key"],
        operation["destination_location_id"],
    )
    if (
        int(source_row["version"]) != int(operation["source_version"])
        or int(destination_row["version"]) != int(operation["destination_version"])
    ):
        raise FluidStateConflictError(
            f"Container state changed after operation "
            f"{operation['operation_key']!r} began; exact reconciliation is required."
        )

    if operation["operation_type"] == "transfer":
        volume = float(operation["volume_ul"])
        transfer_composition = _decode_composition(
            operation["composition_json"], volume, target=operation["operation_key"],
        )
        source_volume = float(source_row["current_volume_ul"])
        destination_volume = float(destination_row["current_volume_ul"])
        source_composition = _decode_composition(
            source_row["composition_json"],
            source_volume,
            target=_format_target(
                operation["source_labware_key"], operation["source_location_id"],
            ),
        )
        destination_composition = _decode_composition(
            destination_row["composition_json"],
            destination_volume,
            target=_format_target(
                operation["destination_labware_key"],
                operation["destination_location_id"],
            ),
        )
        new_source_volume = _clean_volume(source_volume - volume)
        new_destination_volume = _clean_volume(destination_volume + volume)
        new_source_composition = _subtract_composition(
            source_composition, transfer_composition, new_source_volume,
        )
        new_destination_composition = _add_composition(
            destination_composition, transfer_composition, new_destination_volume,
        )
        _update_container(
            connection,
            source_row,
            new_source_volume,
            new_source_composition,
            operation_key=operation["operation_key"],
        )
        _update_container(
            connection,
            destination_row,
            new_destination_volume,
            new_destination_composition,
            operation_key=operation["operation_key"],
        )
    elif operation["operation_type"] != "mix":
        raise FluidStateError(
            f"Unsupported fluid operation type {operation['operation_type']!r}."
        )

    _set_operation_terminal(
        connection,
        operation,
        status="applied",
        detail=detail,
        allowed_statuses=allowed_statuses,
    )


def _update_container(
    connection: sqlite3.Connection,
    row: Mapping[str, Any],
    volume_ul: float,
    composition: Mapping[str, float],
    *,
    operation_key: str,
) -> None:
    cursor = connection.execute(
        "UPDATE fluid_containers SET current_volume_ul = ?, composition_json = ?, "
        "version = version + 1, updated_at = datetime('now') "
        "WHERE id = ? AND version = ?",
        (
            volume_ul,
            _canonical_json(dict(composition)),
            row["id"],
            row["version"],
        ),
    )
    if cursor.rowcount != 1:
        raise FluidStateConflictError(
            f"Container state changed while applying operation {operation_key!r}."
        )


def _set_operation_terminal(
    connection: sqlite3.Connection,
    operation: Mapping[str, Any],
    *,
    status: str,
    detail: str | None,
    allowed_statuses: tuple[str, ...],
) -> None:
    placeholders = ", ".join("?" for _ in allowed_statuses)
    cursor = connection.execute(
        "UPDATE fluid_operations SET status = ?, detail = COALESCE(?, detail), "
        "applied_at = CASE WHEN ? = 'applied' THEN datetime('now') ELSE applied_at END, "
        "updated_at = datetime('now') WHERE id = ? AND status IN "
        f"({placeholders})",
        (status, detail, status, operation["id"], *allowed_statuses),
    )
    if cursor.rowcount != 1:
        raise FluidStateConflictError(
            f"Operation {operation['operation_key']!r} changed while resolving it."
        )
    connection.execute(
        "UPDATE fluid_state_sessions SET updated_at = datetime('now') WHERE id = ?",
        (operation["fluid_state_id"],),
    )


def _replace_operation_container_state(
    connection: sqlite3.Connection,
    operation: Mapping[str, Any],
    *,
    source_volume: float,
    source_composition: Mapping[str, float],
    destination_volume: float,
    destination_composition: Mapping[str, float],
) -> None:
    fluid_state_id = int(operation["fluid_state_id"])
    source_identity = (
        operation["source_labware_key"], operation["source_location_id"],
    )
    destination_identity = (
        operation["destination_labware_key"],
        operation["destination_location_id"],
    )
    source_row = _container_row(connection, fluid_state_id, *source_identity)
    destination_row = _container_row(connection, fluid_state_id, *destination_identity)
    _validate_replacement_volume(
        source_row, source_volume, _format_target(*source_identity), action="Reconciliation",
    )
    _validate_replacement_volume(
        destination_row,
        destination_volume,
        _format_target(*destination_identity),
        action="Reconciliation",
    )
    if source_identity == destination_identity:
        if (
            not math.isclose(
                source_volume,
                destination_volume,
                rel_tol=1e-12,
                abs_tol=_VOLUME_TOLERANCE_UL,
            )
            or dict(source_composition) != dict(destination_composition)
        ):
            raise FluidStateError(
                "A net-zero mix uses one container; source and destination "
                "replacement states must be identical."
            )
        _update_container(
            connection,
            source_row,
            source_volume,
            source_composition,
            operation_key=operation["operation_key"],
        )
        return
    _update_container(
        connection,
        source_row,
        source_volume,
        source_composition,
        operation_key=operation["operation_key"],
    )
    _update_container(
        connection,
        destination_row,
        destination_volume,
        destination_composition,
        operation_key=operation["operation_key"],
    )


def _validate_replacement_volume(
    row: Mapping[str, Any],
    volume_ul: float,
    target: str,
    *,
    action: str,
) -> None:
    capacity = float(row["capacity_ul"])
    if volume_ul > capacity + _VOLUME_TOLERANCE_UL:
        raise FluidStateError(
            f"{action} would overfill {target}: {volume_ul:g} uL exceeds "
            f"{capacity:g} uL capacity."
        )
    working_volume = float(row["working_volume_ul"])
    if volume_ul > working_volume + _VOLUME_TOLERANCE_UL:
        logger.warning(
            "%s for %s exceeds working volume: %.6g uL > %.6g uL.",
            action,
            target,
            volume_ul,
            working_volume,
        )


def _require_state(connection: sqlite3.Connection, fluid_state_id: int) -> None:
    row = connection.execute(
        "SELECT 1 FROM fluid_state_sessions WHERE id = ?", (fluid_state_id,)
    ).fetchone()
    if row is None:
        raise FluidStateNotFoundError(f"Fluid state {fluid_state_id} not found.")


def _pending_operations(
    connection: sqlite3.Connection,
    fluid_state_id: int,
) -> list[tuple[str, str]]:
    return connection.execute(
        "SELECT operation_key, status FROM fluid_operations "
        "WHERE fluid_state_id = ? AND status IN (?, ?) ORDER BY id",
        (fluid_state_id, *_PENDING_OPERATION_STATUSES),
    ).fetchall()


def pending_operations(
    connection: sqlite3.Connection,
    fluid_state_id: int,
) -> list[tuple[str, str]]:
    """Public sibling of :func:`_pending_operations` for cross-module checks.

    Used by ``tip_state`` to block new tip operations while a fluid
    operation is pending, mirroring ``_require_no_pending_operation``'s
    check of tip operations here.
    """
    return _pending_operations(connection, fluid_state_id)


def _normalize_initial_fluids(
    fluids: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for target, definition in fluids.items():
        if not isinstance(target, str) or not target.strip():
            raise FluidStateError("Initial fluid targets must be non-empty strings.")
        if not isinstance(definition, Mapping):
            raise FluidStateError(
                f"Initial fluid {target!r} must map to `volume_ul` and `composition`."
            )
        unknown = set(definition) - {"volume_ul", "composition"}
        if unknown:
            raise FluidStateError(
                f"Initial fluid {target!r} has unknown fields: {sorted(unknown)}."
            )
        if "volume_ul" not in definition:
            raise FluidStateError(f"Initial fluid {target!r} requires `volume_ul`.")
        volume = _finite_nonnegative(definition["volume_ul"], "volume_ul")
        composition = _normalize_composition(definition.get("composition"), volume)
        normalized[target] = {"volume_ul": volume, "composition": composition}
    return normalized


def _canonicalize_initial_fluids(
    deck: Deck,
    fluids: Mapping[str, dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Resolve all seed names before creating or mutating persisted rows."""
    canonical: dict[tuple[str, str], dict[str, Any]] = {}
    original_targets: dict[tuple[str, str], str] = {}
    for target, definition in fluids.items():
        container = _canonical_target(_resolve_deck_target(deck, target))
        previous = original_targets.get(container)
        if previous is not None:
            raise FluidStateError(
                f"Initial fluid targets {previous!r} and {target!r} both resolve "
                f"to canonical container {_format_target(*container)!r}."
            )
        original_targets[container] = target
        canonical[container] = definition
    return canonical


def _normalize_composition(
    composition: Mapping[str, float] | None,
    volume_ul: float,
) -> dict[str, float]:
    if composition is None:
        return {} if volume_ul == 0 else {"unknown": volume_ul}
    if not isinstance(composition, Mapping):
        raise FluidStateError("composition must be a mapping of component to uL.")
    normalized: dict[str, float] = {}
    for component, amount in composition.items():
        if not isinstance(component, str) or not component.strip():
            raise FluidStateError("Composition component names must be non-empty strings.")
        value = _finite_nonnegative(amount, f"composition[{component!r}]")
        if value > _VOLUME_TOLERANCE_UL:
            normalized[component] = value
    _require_composition_sum(normalized, volume_ul, "composition")
    return dict(sorted(normalized.items()))


def _decode_composition(raw: str | None, volume_ul: float, *, target: str) -> dict[str, float]:
    try:
        decoded = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise FluidStateError(f"Corrupt composition JSON for {target!r}.") from exc
    if not isinstance(decoded, dict):
        raise FluidStateError(f"Composition JSON for {target!r} must be an object.")
    return _normalize_composition(decoded, volume_ul)


def _decode_parameters(raw: str | None, *, target: str) -> dict[str, Any]:
    try:
        decoded = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise FluidStateError(f"Corrupt operation parameters JSON for {target!r}.") from exc
    if not isinstance(decoded, dict):
        raise FluidStateError(
            f"Operation parameters JSON for {target!r} must be an object."
        )
    return decoded


def _proportional_composition(
    composition: Mapping[str, float],
    source_volume_ul: float,
    transfer_volume_ul: float,
) -> dict[str, float]:
    if source_volume_ul <= _VOLUME_TOLERANCE_UL:
        raise FluidStateError("Cannot transfer from an empty source container.")
    names = sorted(composition)
    result = {
        name: float(composition[name]) * transfer_volume_ul / source_volume_ul
        for name in names
    }
    if names:
        result[names[-1]] += transfer_volume_ul - sum(result.values())
    return _clean_composition(result)


def _subtract_composition(
    composition: Mapping[str, float],
    removed: Mapping[str, float],
    expected_volume_ul: float,
) -> dict[str, float]:
    result = dict(composition)
    for component, amount in removed.items():
        remaining = result.get(component, 0.0) - amount
        if remaining < -_VOLUME_TOLERANCE_UL:
            raise FluidStateError(
                f"Transfer composition removes more {component!r} than the source contains."
            )
        result[component] = max(0.0, remaining)
    result = _clean_composition(result)
    _require_composition_sum(result, expected_volume_ul, "source composition")
    return result


def _add_composition(
    composition: Mapping[str, float],
    added: Mapping[str, float],
    expected_volume_ul: float,
) -> dict[str, float]:
    result = dict(composition)
    for component, amount in added.items():
        result[component] = result.get(component, 0.0) + amount
    result = _clean_composition(result)
    _require_composition_sum(result, expected_volume_ul, "destination composition")
    return result


def _require_composition_sum(
    composition: Mapping[str, float],
    volume_ul: float,
    label: str,
) -> None:
    total = sum(composition.values())
    tolerance = max(_VOLUME_TOLERANCE_UL, abs(volume_ul) * 1e-9)
    if not math.isclose(total, volume_ul, rel_tol=1e-9, abs_tol=tolerance):
        raise FluidStateError(
            f"{label} totals {total:g} uL, but current volume is {volume_ul:g} uL."
        )


def _clean_composition(composition: Mapping[str, float]) -> dict[str, float]:
    return {
        component: _clean_volume(float(amount))
        for component, amount in sorted(composition.items())
        if abs(float(amount)) > _VOLUME_TOLERANCE_UL
    }


def _clean_volume(value: float) -> float:
    return 0.0 if abs(value) <= _VOLUME_TOLERANCE_UL else float(value)


def _finite_nonnegative(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FluidStateError(f"{label} must be a finite non-negative number.")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise FluidStateError(f"{label} must be a finite non-negative number.")
    return result


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FluidStateError(f"{label} must be a finite number.")
    result = float(value)
    if not math.isfinite(result):
        raise FluidStateError(f"{label} must be a finite number.")
    return result


def _finite_positive(value: Any, label: str) -> float:
    result = _finite_nonnegative(value, label)
    if result <= 0:
        raise FluidStateError(f"{label} must be greater than zero.")
    return result


def _format_target(labware_key: str, location_id: str) -> str:
    return f"{labware_key}.{location_id}" if location_id else labware_key


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _location_sort_key(location_id: str) -> tuple[str, int, str]:
    prefix = "".join(character for character in location_id if character.isalpha())
    suffix = "".join(character for character in location_id if character.isdigit())
    return prefix, int(suffix) if suffix else -1, location_id


__all__ = [
    "FLUID_STATE_API_VERSION",
    "FluidContainerSnapshot",
    "FluidOperationSnapshot",
    "FluidReplacementEndpoint",
    "FluidReplacementState",
    "FluidStateConflictError",
    "FluidStateDeckMismatchError",
    "FluidStateError",
    "FluidStateNotFoundError",
    "FluidStateReconciliationRequiredError",
    "FluidStateSnapshot",
    "FluidStateSummary",
    "begin_fluid_mix",
    "begin_fluid_transfer",
    "complete_fluid_mix",
    "complete_fluid_transfer",
    "create_fluid_state",
    "get_fluid_container",
    "get_fluid_snapshot",
    "load_initial_fluids",
    "load_replacement_state",
    "list_fluid_states",
    "mark_fluid_reconciliation_required",
    "pending_operations",
    "resolve_fluid_operation",
    "resume_fluid_state",
    "seed_fluid",
]
