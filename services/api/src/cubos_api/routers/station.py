"""Generic station ownership, authoritative state and configuration snapshots."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal

from cubos.data import CapStateError, DataStore, FluidStateError, TipStateError
from cubos.deck import load_deck_from_yaml
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from cubos_api.config import get_settings
from cubos_api.models.state import RunStateSelection
from cubos_api.services.run_manager import RunConflictError, get_run_manager
from cubos_api.services.state_errors import map_state_exception
from cubos_api.services.yaml_io import resolve_config_path

router = APIRouter(prefix="/api/v1", tags=["station"])


class ReservationRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=160)


class OperatorReleaseRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=160)
    confirmation: str = Field(min_length=1, max_length=180)


class StateValidationRequest(BaseModel):
    deck_yaml: str = Field(min_length=1)
    fluid_state_id: int = Field(gt=0)


@router.get("/station/status")
def station_status() -> dict:
    from cubos_api.routers import gantry
    session = gantry.current_session()
    manager = get_run_manager()
    return {
        "connected": bool(session is not None and session.connected),
        "calibration_active": bool(session is not None and session.calibration_active),
        "active_run_id": manager.active_run_id,
        "reserved": manager.reservation_owner is not None,
        "owner": manager.reservation_owner,
    }


@router.get("/station/reservation")
def reservation_status() -> dict:
    owner = get_run_manager().reservation_owner
    return {"reserved": owner is not None, "owner": owner}


@router.post("/station/reservation", status_code=201)
def reserve_station(body: ReservationRequest) -> dict:
    owner = body.owner.strip()
    if not owner:
        raise HTTPException(422, "Reservation owner must not be blank")
    try:
        token = get_run_manager().reserve_station(owner)
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"reserved": True, "owner": owner, "reservation_token": token}


@router.delete("/station/reservation")
def release_station(x_cubos_reservation: str = Header()) -> dict:
    try:
        get_run_manager().release_station(x_cubos_reservation)
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"reserved": False, "owner": None}


@router.post("/station/reservation/operator-release")
def operator_release_station(body: OperatorReleaseRequest) -> dict:
    try:
        get_run_manager().operator_release_station(body.owner, body.confirmation)
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"reserved": False, "owner": None}


@router.post("/station/state/validate")
def validate_state(body: StateValidationRequest) -> dict:
    manager = get_run_manager()
    try:
        manager._resolve_run_state(body.deck_yaml, RunStateSelection(fluid_state_id=body.fluid_state_id))
        store = DataStore(get_settings().data_db_path)
        try:
            fluids = store.get_fluid_snapshot(body.fluid_state_id)
            tips = store.get_tip_snapshot(body.fluid_state_id)
        finally:
            store.close()
        with tempfile.TemporaryDirectory(prefix="cubos-state-check-") as directory:
            path = Path(directory) / "deck.yaml"
            path.write_text(body.deck_yaml, encoding="utf-8")
            deck = load_deck_from_yaml(path)
        dead_volumes = {}
        for item in fluids["containers"]:
            target = f"{item['labware_key']}.{item['location_id']}" if item["location_id"] else item["labware_key"]
            resolved = deck.resolve_labware_target(target)
            labware = resolved.labware
            if resolved.location_id is not None and hasattr(labware, "vials"):
                labware = labware.vials[resolved.location_id]
            dead_volumes[target] = float(getattr(labware, "dead_volume_ul", 0.0))
            item["dead_volume_ul"] = dead_volumes[target]
    except (FluidStateError, TipStateError, CapStateError) as exc:
        raise map_state_exception(exc) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
    return {"fluids": fluids, "tips": tips, "dead_volumes": dead_volumes}


@router.get("/configs/{category}/{filename}/raw")
def config_snapshot(category: Literal["gantry", "deck", "protocol"], filename: str) -> dict:
    try:
        path = resolve_config_path(get_settings().configs_dir, category, filename)
        if not path.is_file():
            raise HTTPException(404, f"Missing {category} configuration: {filename}")
        return {"content": path.read_text(encoding="utf-8")}
    except (OSError, ValueError) as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
