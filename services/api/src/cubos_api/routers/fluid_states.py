"""Fluid/tip/cap state resource: authoritative CubOS liquid-handling state
over HTTP, so an operator can create, inspect, resume, and reconcile a
run's complete state without shell or SQL access.

This router is a thin projection over ``cubos.data.DataStore`` — the same
public API the protocol engine itself uses — never raw SQL.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from cubos.data import (
    CapStateError,
    DataStore,
    FluidStateError,
    TipStateError,
)
from cubos.deck import load_deck_from_yaml
from cubos.deck.errors import DeckLoaderError
from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from cubos_api.config import get_settings
from cubos_api.models.state import (
    CapContainerView,
    CapStateResponse,
    ContainerView,
    CreateFluidStateRequest,
    FluidStateDetailResponse,
    FluidStateSummaryResponse,
    OperationsResponse,
    OperationView,
    PipetteAttachmentView,
    ReconciliationResponse,
    ResolveReconciliationRequest,
    ResolveReconciliationResponse,
    TipContainerView,
    TipStateResponse,
)
from cubos_api.services.state_errors import map_state_exception
from cubos_api.services.yaml_io import resolve_config_path

router = APIRouter(prefix="/api/v1/fluid-states", tags=["cubos-state-v1"])

_PENDING_STATUSES = {"started", "reconciliation_required"}
_STATE_EXCEPTIONS = (FluidStateError, TipStateError, CapStateError)


def _data_db_path() -> Path:
    return get_settings().data_db_path.expanduser().resolve()


def _open_store() -> DataStore:
    return DataStore(_data_db_path())


def _load_deck(deck_file: str):
    try:
        path = resolve_config_path(get_settings().configs_dir, "deck", deck_file)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(404, f"deck config not found: {deck_file}")
    try:
        return path, load_deck_from_yaml(path)
    except (DeckLoaderError, ValueError, ValidationError) as exc:
        raise HTTPException(400, f"cannot load deck {deck_file!r}: {exc}") from exc


def _containers_with_roles(snapshot: Dict[str, Any]) -> List[ContainerView]:
    descriptor_rows = snapshot.get("deck_descriptor", {}).get("containers", [])
    lookup = {
        (row["labware_key"], row["location_id"]): row for row in descriptor_rows
    }
    views: List[ContainerView] = []
    for container in snapshot["containers"]:
        descriptor = lookup.get(
            (container["labware_key"], container["location_id"]), {}
        )
        views.append(
            ContainerView(
                labware_key=container["labware_key"],
                location_id=container["location_id"],
                labware_type=container["labware_type"],
                capacity_ul=container["capacity_ul"],
                working_volume_ul=container["working_volume_ul"],
                current_volume_ul=container["current_volume_ul"],
                composition=container["composition"],
                version=container["version"],
                updated_at=container["updated_at"],
                role=descriptor.get("role"),
                solution=descriptor.get("solution"),
                allowed_solutions=descriptor.get("allowed_solutions"),
            )
        )
    return views


def _operation_views(
    store: DataStore, fluid_state_id: int, *, only_status: Optional[set] = None
) -> List[OperationView]:
    fluid_snapshot = store.get_fluid_snapshot(fluid_state_id)
    tip_snapshot = store.get_tip_snapshot(fluid_state_id)
    cap_snapshot = store.get_cap_snapshot(fluid_state_id)

    views: List[OperationView] = []
    for op in fluid_snapshot["operations"]:
        if only_status and op["status"] not in only_status:
            continue
        views.append(
            OperationView(
                domain="fluid",
                id=op["id"],
                operation_key=op["operation_key"],
                operation_type=op["operation_type"],
                status=op["status"],
                campaign_id=op["campaign_id"],
                detail=op["detail"],
                created_at=op["created_at"],
                updated_at=op["updated_at"],
                applied_at=op["applied_at"],
                context={
                    "source": op["source"],
                    "destination": op["destination"],
                    "volume_ul": op["volume_ul"],
                    "composition": op["composition"],
                    "parameters": op["parameters"],
                },
            )
        )
    for op in tip_snapshot["operations"]:
        if only_status and op["status"] not in only_status:
            continue
        views.append(
            OperationView(
                domain="tip",
                id=op["id"],
                operation_key=op["operation_key"],
                operation_type=op["operation_type"],
                status=op["status"],
                campaign_id=op["campaign_id"],
                detail=op["detail"],
                created_at=op["created_at"],
                updated_at=op["updated_at"],
                applied_at=op["applied_at"],
                context={
                    "rack_key": op["rack_key"],
                    "slot_id": op["slot_id"],
                    "tip_extension_mm": op["tip_extension_mm"],
                },
            )
        )
    for op in cap_snapshot["operations"]:
        if only_status and op["status"] not in only_status:
            continue
        views.append(
            OperationView(
                domain="cap",
                id=op["id"],
                operation_key=op["operation_key"],
                operation_type=op["operation_type"],
                status=op["status"],
                campaign_id=op["campaign_id"],
                detail=op["detail"],
                created_at=op["created_at"],
                updated_at=op["updated_at"],
                applied_at=op["applied_at"],
                context={
                    "labware_key": op["labware_key"],
                    "location_id": op["location_id"],
                },
            )
        )
    return views


def _summary(row: Dict[str, Any]) -> FluidStateSummaryResponse:
    return FluidStateSummaryResponse(**row)


@router.post("", response_model=FluidStateSummaryResponse, status_code=201)
def create_fluid_state(body: CreateFluidStateRequest) -> FluidStateSummaryResponse:
    deck_path, deck = _load_deck(body.deck_file)
    store = _open_store()
    try:
        fluids = {
            key: {"volume_ul": item.volume_ul, "composition": item.composition}
            for key, item in body.fluids.items()
        }
        try:
            state_id = store.create_fluid_state(
                str(deck_path), deck, label=body.label, initial_fluids=fluids
            )
        except _STATE_EXCEPTIONS as exc:
            raise map_state_exception(exc) from exc
        summaries = {row["id"]: row for row in store.list_fluid_states()}
        return _summary(summaries[state_id])
    finally:
        store.close()


@router.get("", response_model=List[FluidStateSummaryResponse])
def list_fluid_states() -> List[FluidStateSummaryResponse]:
    store = _open_store()
    try:
        return [_summary(row) for row in store.list_fluid_states()]
    finally:
        store.close()


@router.get("/{fluid_state_id}", response_model=FluidStateDetailResponse)
def get_fluid_state(fluid_state_id: int) -> FluidStateDetailResponse:
    store = _open_store()
    try:
        try:
            snapshot = store.get_fluid_snapshot(fluid_state_id)
        except _STATE_EXCEPTIONS as exc:
            raise map_state_exception(exc) from exc
        containers = _containers_with_roles(snapshot)
        pending = sum(
            1 for op in snapshot["operations"] if op["status"] in _PENDING_STATUSES
        )
        reconciliation = sum(
            1
            for op in snapshot["operations"]
            if op["status"] == "reconciliation_required"
        )
        return FluidStateDetailResponse(
            id=snapshot["id"],
            deck_path=snapshot["deck_path"],
            deck_fingerprint=snapshot["deck_fingerprint"],
            label=snapshot["label"],
            created_at=snapshot["created_at"],
            updated_at=snapshot["updated_at"],
            containers=containers,
            pending_operation_count=pending,
            reconciliation_required_count=reconciliation,
        )
    finally:
        store.close()


@router.get("/{fluid_state_id}/containers", response_model=List[ContainerView])
def get_containers(fluid_state_id: int) -> List[ContainerView]:
    store = _open_store()
    try:
        try:
            snapshot = store.get_fluid_snapshot(fluid_state_id)
        except _STATE_EXCEPTIONS as exc:
            raise map_state_exception(exc) from exc
        return _containers_with_roles(snapshot)
    finally:
        store.close()


@router.get("/{fluid_state_id}/tips", response_model=TipStateResponse)
def get_tips(fluid_state_id: int) -> TipStateResponse:
    store = _open_store()
    try:
        try:
            snapshot = store.get_tip_snapshot(fluid_state_id)
        except _STATE_EXCEPTIONS as exc:
            raise map_state_exception(exc) from exc
        return TipStateResponse(
            fluid_state_id=snapshot["fluid_state_id"],
            containers=[TipContainerView(**c) for c in snapshot["containers"]],
            pipette=PipetteAttachmentView(**snapshot["pipette"]),
        )
    finally:
        store.close()


@router.get("/{fluid_state_id}/caps", response_model=CapStateResponse)
def get_caps(fluid_state_id: int) -> CapStateResponse:
    store = _open_store()
    try:
        try:
            snapshot = store.get_cap_snapshot(fluid_state_id)
        except _STATE_EXCEPTIONS as exc:
            raise map_state_exception(exc) from exc
        return CapStateResponse(
            fluid_state_id=snapshot["fluid_state_id"],
            containers=[CapContainerView(**c) for c in snapshot["containers"]],
        )
    finally:
        store.close()


@router.get("/{fluid_state_id}/operations", response_model=OperationsResponse)
def get_operations(
    fluid_state_id: int, pending_only: bool = True
) -> OperationsResponse:
    store = _open_store()
    try:
        statuses = _PENDING_STATUSES if pending_only else None
        try:
            operations = _operation_views(store, fluid_state_id, only_status=statuses)
        except _STATE_EXCEPTIONS as exc:
            raise map_state_exception(exc) from exc
        return OperationsResponse(fluid_state_id=fluid_state_id, operations=operations)
    finally:
        store.close()


@router.get("/{fluid_state_id}/reconciliation", response_model=ReconciliationResponse)
def get_reconciliation(fluid_state_id: int) -> ReconciliationResponse:
    store = _open_store()
    try:
        try:
            items = _operation_views(
                store, fluid_state_id, only_status={"reconciliation_required"}
            )
        except _STATE_EXCEPTIONS as exc:
            raise map_state_exception(exc) from exc
        return ReconciliationResponse(fluid_state_id=fluid_state_id, items=items)
    finally:
        store.close()


@router.post(
    "/{fluid_state_id}/reconciliation/resolve",
    response_model=ResolveReconciliationResponse,
)
def resolve_reconciliation(
    fluid_state_id: int, body: ResolveReconciliationRequest
) -> ResolveReconciliationResponse:
    detail = f"[{body.operator.strip()}] {body.reason.strip()}"
    store = _open_store()
    try:
        try:
            if body.domain == "fluid":
                store.resolve_fluid_operation(
                    body.operation_key,
                    body.resolution,
                    detail=detail,
                    source_volume_ul=body.source_volume_ul,
                    source_composition=body.source_composition,
                    destination_volume_ul=body.destination_volume_ul,
                    destination_composition=body.destination_composition,
                )
                snapshot = store.get_fluid_snapshot(fluid_state_id)
            elif body.domain == "tip":
                store.resolve_tip_operation(
                    body.operation_key,
                    body.resolution,
                    detail=detail,
                    final_slot_status=body.final_slot_status,
                )
                snapshot = store.get_tip_snapshot(fluid_state_id)
            else:
                store.resolve_cap_operation(
                    body.operation_key,
                    body.resolution,
                    detail=detail,
                    final_status=body.final_status,
                )
                snapshot = store.get_cap_snapshot(fluid_state_id)
        except _STATE_EXCEPTIONS as exc:
            raise map_state_exception(exc) from exc

        resolved = next(
            (
                op
                for op in snapshot["operations"]
                if op["operation_key"] == body.operation_key
            ),
            None,
        )
        if resolved is None:
            raise HTTPException(
                404, f"operation {body.operation_key!r} was not found"
            )
        return ResolveReconciliationResponse(
            domain=body.domain,
            operation_key=body.operation_key,
            status=resolved["status"],
            detail=resolved["detail"],
        )
    finally:
        store.close()
