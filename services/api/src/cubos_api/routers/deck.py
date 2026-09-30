"""Deck config API endpoints — thin layer over CubOS deck loader."""

from copy import deepcopy
import logging
from pathlib import Path
import tempfile
from typing import Any, Dict, Optional

from cubos.deck import LABWARE_YAML_ENTRY_MODELS, derive_wells_preview, load_deck_from_yaml, resolve_load_names
from cubos.deck.labware.well_plate import WellPlate
from cubos.deck.errors import DeckLoaderError
from cubos.deck.yaml_schema import WellPlateYamlEntry
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from cubos_api.config import get_settings
from cubos_api.services.yaml_io import delete_config, list_configs, read_yaml, resolve_config_path, write_yaml

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/deck", tags=["deck"])


# ── Response models (API shape only, no validation duplication) ────────


class WellPosition(BaseModel):
    x: float
    y: float
    z: float


class LabwareResponse(BaseModel):
    key: str
    config: Dict[str, Any]
    wells: Optional[Dict[str, WellPosition]] = None
    location: Optional[WellPosition] = None
    geometry: Optional[Dict[str, Optional[float]]] = None
    positions: Optional[Dict[str, WellPosition]] = None


class DeckResponse(BaseModel):
    filename: str
    labware: list[LabwareResponse]


# ── Routes ─────────────────────────────────────────────────────────────


@router.get("/configs")
def list_deck_configs() -> list[str]:
    return list_configs(get_settings().configs_dir, "deck")


@router.get("/{filename}")
def get_deck(filename: str) -> DeckResponse:
    try:
        path = resolve_config_path(get_settings().configs_dir, "deck", filename)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not path.is_file():
        raise HTTPException(404, f"Config not found: {filename}")

    # Use CubOS's loader for validation + well derivation.
    try:
        raw = read_yaml(path)
        resolved_raw = resolve_load_names(raw)
        deck = load_deck_from_yaml(path)
    except (ValueError, ValidationError, DeckLoaderError) as e:
        raise HTTPException(400, str(e))

    items: list[LabwareResponse] = []
    for key, labware in deck.labware.items():
        config = _normalize_labware_config(
            resolved_raw.get("labware", {}).get(key, {}),
            labware,
            key,
        )
        if hasattr(labware, "iter_positions"):
            positions = _serialize_positions(labware.iter_positions())
        else:
            log.warning("Labware %s (%s) has no iter_positions — positions will be empty", key, type(labware).__name__)
            positions = {}
        wells = positions if isinstance(labware, WellPlate) else None
        if hasattr(labware, "get_initial_position"):
            location = _serialize_point(labware.get_initial_position())
        else:
            location = None
        items.append(
            LabwareResponse(
                key=key,
                config=config,
                wells=wells,
                location=location,
                geometry=_serialize_geometry(getattr(labware, "geometry", None)),
                positions={name: point for name, point in positions.items() if name != "location"} or None,
            )
        )

    return DeckResponse(filename=filename, labware=items)


@router.post("/preview-wells")
def preview_wells(body: dict) -> Dict[str, WellPosition]:
    """Compute well positions from a well plate config using CubOS's
    calibration logic, without requiring the config to be saved first."""
    try:
        entry = WellPlateYamlEntry.model_validate(body)
        resolved_z = entry.a1_point.z
        if resolved_z is None:
            raise ValueError("Calibration A1 must include z for preview.")
        wells = derive_wells_preview(entry, resolved_z=resolved_z)
        return {
            wid: WellPosition(x=round(c.x, 3), y=round(c.y, 3), z=round(c.z, 3))
            for wid, c in wells.items()
        }
    except (ValueError, ValidationError) as e:
        raise HTTPException(400, str(e))


@router.put("/{filename}")
def put_deck(filename: str, body: dict) -> DeckResponse:
    try:
        path = resolve_config_path(get_settings().configs_dir, "deck", filename)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    payload = deepcopy(body)
    try:
        _validate_deck_payload(payload)
    except (ValueError, ValidationError) as e:
        raise HTTPException(400, str(e))
    write_yaml(path, payload)
    return get_deck(filename)


@router.delete("/{filename}")
def delete_deck(filename: str) -> dict:
    from cubos_api.routers import gantry as gantry_router

    if gantry_router.run_active():
        raise HTTPException(409, "Cannot delete a deck config while a protocol run is active")
    try:
        delete_config(get_settings().configs_dir, "deck", filename)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError:
        raise HTTPException(404, f"Config not found: {filename}")
    return {"status": "deleted", "filename": filename}


def _validate_deck_payload(payload: dict) -> None:
    """Validate a prospective deck payload through CubOS before writing it."""
    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        write_yaml(tmp_path, payload)
        load_deck_from_yaml(tmp_path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _serialize_point(point: Any) -> Optional[WellPosition]:
    if point is None:
        return None
    return WellPosition(x=point.x, y=point.y, z=point.z)


def _serialize_positions(positions: Dict[str, Any]) -> Dict[str, WellPosition]:
    return {
        name: WellPosition(x=coord.x, y=coord.y, z=coord.z)
        for name, coord in positions.items()
        if coord is not None
    }


def _serialize_geometry(geometry: Any) -> Optional[Dict[str, Optional[float]]]:
    if geometry is None:
        return None
    return {
        "length": _geometry_dimension(geometry, "length"),
        "width": _geometry_dimension(geometry, "width"),
        "height": _geometry_dimension(geometry, "height"),
    }


def _geometry_dimension(geometry: Any, name: str) -> Optional[float]:
    return getattr(geometry, name, None)


def _normalize_labware_config(raw_config: Any, labware: Any, deck_key: str) -> Dict[str, Any]:
    if not isinstance(raw_config, dict):
        log.warning("Labware %s has non-dict raw config (%r) — using empty config", deck_key, type(raw_config).__name__)
        config: Dict[str, Any] = {}
    else:
        config = deepcopy(raw_config)

    labware_type = config.get("type")

    if "name" not in config and hasattr(labware, "name"):
        config["name"] = getattr(labware, "name")
    if "model_name" not in config and hasattr(labware, "model_name"):
        config["model_name"] = getattr(labware, "model_name")
    if "name" not in config:
        config["name"] = deck_key

    schema = LABWARE_YAML_ENTRY_MODELS.get(labware_type)
    if schema is not None:
        for field_name in schema.model_fields:
            _set_default(config, field_name, getattr(labware, field_name, None))

    return config


def _set_default(config: Dict[str, Any], key: str, value: Any) -> None:
    if config.get(key) is None and value is not None:
        config[key] = value
