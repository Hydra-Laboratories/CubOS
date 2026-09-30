"""Gantry config + position API endpoints."""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, HTTPException
from cubos.gantry.session import (
    CalibrationBlockedError,
    GantryAlarmError,
    GantryNotConnectedError,
    GantryPositionSnapshot,
    GantrySession,
    GantrySessionError,
    GantrySessionHealthCheckError,
    InterruptFeedHoldTimeoutError,
    MovementOutOfBoundsError,
)
from cubos.gantry.gantry_driver.exceptions import MillConnectionError
from cubos.gantry.limit_recovery import looks_like_limit_alarm
from cubos.gantry.yaml_schema import GantryYamlSchema
from cubos.instruments.pipette.models import PIPETTE_MODELS
from cubos.instruments.registry import (
    config_fields,
    list_measurement_methods,
    list_measurement_method_params,
    get_supported_types,
    get_supported_vendors,
)
from pydantic import BaseModel, ValidationError, model_validator

from cubos_api.config import get_settings
from cubos_api.models.gantry import GantryPosition, GantryResponse
from cubos_api.services.yaml_io import delete_config, list_configs, read_yaml, resolve_config_path, write_yaml

router = APIRouter(prefix="/api/v1/gantry", tags=["gantry"])

_session: GantrySession | None = None

# Session-lifecycle locking (not business logic): guards creation of the
# module-level session against concurrent /connect calls, and the run-state
# gate below guards against motion requests queuing behind a long-running
# protocol. See tests/test_architecture_boundaries.py for the rationale
# behind why this is allowed despite the general router-lock ban.
_session_create_lock = threading.Lock()

# Run-in-progress gate: `session.run_protocol` holds the CubOS session lock
# for the whole run (hours). Without this gate, motion endpoints would block
# on that lock as sync `def`s on the AnyIO threadpool and then fire *after*
# the run finishes — surprise motion. Endpoints check `run_active()` and
# reject with 409 instead of queuing.
_run_state: Dict[str, Any] = {"active": False, "campaign_id": None, "protocol_file": None}
_run_state_lock = threading.Lock()

class PipetteModelInfo(BaseModel):
    name: str
    family: str
    channels: int
    max_volume: float
    min_volume: float


class InstrumentTypeInfo(BaseModel):
    type: str
    vendors: List[str]
    is_mock: bool


class InstrumentFieldInfo(BaseModel):
    name: str
    type: str
    required: bool
    default: Any = None
    choices: Optional[List[Any]] = None


class JogRequest(BaseModel):
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class MoveToRequest(BaseModel):
    x: float
    y: float
    z: float


class SetWorkCoordinatesRequest(BaseModel):
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None

    @model_validator(mode="after")
    def _at_least_one_axis(self) -> "SetWorkCoordinatesRequest":
        if self.x is None and self.y is None and self.z is None:
            raise ValueError("At least one axis must be supplied.")
        return self


class ConfigureSoftLimitsRequest(BaseModel):
    max_travel_x: float
    max_travel_y: float
    max_travel_z: float
    status_report: Optional[float] = None
    homing_pull_off: Optional[float] = None
    hard_limits: Optional[bool] = None
    tolerance_mm: float = 0.25


class GrblSettingsResponse(BaseModel):
    settings: Dict[str, str]


class SetGrblSettingRequest(BaseModel):
    setting: str
    value: str


class JogBlockingRequest(BaseModel):
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    timeout_s: float = 10.0


class LimitRecoveryRequest(BaseModel):
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    pull_off_mm: float = 5.0
    feed_rate: float = 2500.0


class LimitRecoveryResponse(BaseModel):
    status: str
    attempts: int
    pull_off: Dict[str, float]
    messages: List[str]


class CalibrationCenterResponse(BaseModel):
    xy_bounds: Dict[str, float]
    position: Dict[str, float]


class FinalizeOriginRequest(BaseModel):
    home_z: float
    block_touch_z: float
    block_height: float
    factory_z_travel: float
    tolerance_mm: float = 0.25


class FinalizeOriginResponse(BaseModel):
    measured_volume: Dict[str, float]
    z_calibration: Dict[str, Any]
    max_travel: Dict[str, float]
    position: Dict[str, float]
    homing_pull_off_mm: Optional[float] = None


class ConnectRequest(BaseModel):
    filename: Optional[str] = None


def current_session() -> GantrySession | None:
    return _session


def _get_or_create_session() -> GantrySession:
    global _session
    with _session_create_lock:
        if _session is None:
            _session = GantrySession()
        return _session


def _require_session() -> GantrySession:
    session = _session
    if session is None or not session.connected:
        raise HTTPException(400, "Gantry not connected")
    return session


def reset_session() -> None:
    """Clear the module-level session. Used by app lifespan on shutdown."""
    global _session
    _session = None


def begin_run(*, campaign_id: Any = None, protocol_file: str | None = None) -> None:
    """Mark a protocol run as in-progress. Raises 409 if one is already active."""
    with _run_state_lock:
        if _run_state["active"]:
            raise HTTPException(409, "A protocol run is already in progress")
        _run_state["active"] = True
        _run_state["campaign_id"] = campaign_id
        _run_state["protocol_file"] = protocol_file


def end_run() -> None:
    """Clear the run-in-progress gate."""
    with _run_state_lock:
        _run_state["active"] = False
        _run_state["campaign_id"] = None
        _run_state["protocol_file"] = None


def run_active() -> bool:
    with _run_state_lock:
        return _run_state["active"]


def run_status() -> Dict[str, Any]:
    with _run_state_lock:
        return {
            "active": _run_state["active"],
            "protocol_file": _run_state["protocol_file"],
        }


def _reject_if_run_active() -> None:
    if run_active():
        raise HTTPException(409, "Gantry is busy running a protocol")


def _session_calibration_active(session: GantrySession | None) -> bool:
    if session is None:
        return False
    return bool(session.calibration_active)


def _position_response(
    snapshot: GantryPositionSnapshot,
    *,
    session: GantrySession | None = None,
) -> GantryPosition:
    return GantryPosition(
        **asdict(snapshot),
        calibration_active=_session_calibration_active(session or current_session()),
    )


def _session_http_exception(exc: Exception, *, default_action: str) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, GantryNotConnectedError):
        return HTTPException(400, "Gantry not connected")
    if isinstance(exc, CalibrationBlockedError):
        return HTTPException(400, str(exc))
    if isinstance(exc, GantrySessionHealthCheckError):
        return HTTPException(400, str(exc))
    if isinstance(exc, MovementOutOfBoundsError):
        status_code = 409 if _movement_requires_operator_reconnect(exc) else 400
        return HTTPException(status_code, str(exc))
    if isinstance(exc, GantryAlarmError):
        return HTTPException(409, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(400, str(exc))
    if isinstance(exc, GantrySessionError):
        return HTTPException(500, str(exc))
    return HTTPException(500, f"{default_action} failed: {exc}")


def _movement_requires_operator_reconnect(exc: MovementOutOfBoundsError) -> bool:
    return bool(exc.requires_reconnect)


def _build_instrument_fields(type_key: str, vendor: str) -> List[InstrumentFieldInfo]:
    return [
        InstrumentFieldInfo(
            name=field.name,
            type=field.type,
            required=field.required,
            default=field.default,
            choices=None if field.choices is None else list(field.choices),
        )
        for field in config_fields(type_key, vendor)
    ]


def _validated_gantry_config(data: Dict[str, Any]) -> GantryYamlSchema:
    return GantryYamlSchema.model_validate(data)


def _api_gantry_config(config: GantryYamlSchema) -> Dict[str, Any]:
    return config.model_dump(mode="json", exclude_none=True)


def _selected_gantry_path(filename: str | None) -> tuple[str | None, Any | None]:
    settings = get_settings()
    if filename:
        path = resolve_config_path(settings.configs_dir, "gantry", filename)
        if not path.is_file():
            raise HTTPException(404, f"Config not found: {filename}")
        return filename, path
    gantry_configs = list_configs(settings.configs_dir, "gantry")
    if not gantry_configs:
        return None, None
    selected = gantry_configs[0]
    return selected, resolve_config_path(settings.configs_dir, "gantry", selected)


@router.get("/configs")
def list_gantry_configs() -> list[str]:
    return list_configs(get_settings().configs_dir, "gantry")


@router.get("/instrument-types")
def list_instrument_types() -> List[InstrumentTypeInfo]:
    return [
        InstrumentTypeInfo(
            type=key,
            vendors=get_supported_vendors(key),
            is_mock=key.startswith("mock_"),
        )
        for key in get_supported_types()
    ]


@router.get("/pipette-models")
def list_pipette_models() -> List[PipetteModelInfo]:
    return [
        PipetteModelInfo(
            name=cfg.name,
            family=cfg.family.value,
            channels=cfg.channels,
            max_volume=cfg.max_volume,
            min_volume=cfg.min_volume,
        )
        for cfg in PIPETTE_MODELS.values()
    ]


@router.get("/instrument-schemas")
def get_instrument_schemas() -> Dict[str, Dict[str, List[InstrumentFieldInfo]]]:
    return {
        type_key: {
            vendor: _build_instrument_fields(type_key, vendor)
            for vendor in get_supported_vendors(type_key)
        }
        for type_key in get_supported_types()
    }


@router.get("/instrument-methods")
def get_instrument_methods() -> Dict[str, List[str]]:
    return {
        type_key: list_measurement_methods(type_key)
        for type_key in get_supported_types()
    }


@router.get("/instrument-method-params")
def get_instrument_method_params() -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Per instrument type, each measurement method's method_kwargs schema."""
    return {
        type_key: list_measurement_method_params(type_key)
        for type_key in get_supported_types()
    }


@router.get("/position")
def get_position() -> GantryPosition:
    session = current_session()
    if session is None:
        return GantryPosition(connected=False, status="Not connected")
    return _position_response(session.position())


@router.post("/home")
def home() -> GantryPosition:
    _reject_if_run_active()
    session = _require_session()
    try:
        return _position_response(session.home(), session=session)
    except Exception as exc:
        raise _session_http_exception(exc, default_action="Homing") from exc


@router.post("/jog")
def jog(req: JogRequest) -> dict:
    _reject_if_run_active()
    try:
        _require_session().jog(x=req.x, y=req.y, z=req.z)
    except Exception as exc:
        logging.warning("Jog error: %s", exc)
        raise _session_http_exception(exc, default_action="Jog") from exc
    return {"status": "ok"}


@router.post("/move-to")
def move_to(req: MoveToRequest) -> dict:
    _reject_if_run_active()
    try:
        _require_session().move_to(x=req.x, y=req.y, z=req.z)
    except Exception as exc:
        raise _session_http_exception(exc, default_action="Move") from exc
    return {"status": "ok"}


@router.post("/move-to-blocking")
def move_to_blocking(req: MoveToRequest) -> GantryPosition:
    _reject_if_run_active()
    session = _require_session()
    try:
        return _position_response(
            session.move_to_blocking(x=req.x, y=req.y, z=req.z),
            session=session,
        )
    except Exception as exc:
        raise _session_http_exception(exc, default_action="Move") from exc


@router.post("/jog-blocking")
def jog_blocking(req: JogBlockingRequest) -> GantryPosition:
    _reject_if_run_active()
    session = _require_session()
    try:
        return _position_response(
            session.jog_blocking(
                x=req.x,
                y=req.y,
                z=req.z,
                timeout_s=req.timeout_s,
            ),
            session=session,
        )
    except Exception as exc:
        raise _session_http_exception(exc, default_action="Jog") from exc


@router.post("/work-coordinates")
def set_work_coordinates(req: SetWorkCoordinatesRequest) -> GantryPosition:
    session = _require_session()
    try:
        return _position_response(
            session.set_work_coordinates(x=req.x, y=req.y, z=req.z),
            session=session,
        )
    except Exception as exc:
        raise _session_http_exception(
            exc, default_action="Set work coordinates"
        ) from exc


@router.post("/soft-limits")
def configure_soft_limits(req: ConfigureSoftLimitsRequest) -> dict:
    try:
        _require_session().configure_soft_limits(
            max_travel_x=req.max_travel_x,
            max_travel_y=req.max_travel_y,
            max_travel_z=req.max_travel_z,
            status_report=req.status_report,
            homing_pull_off=req.homing_pull_off,
            hard_limits=req.hard_limits,
            tolerance_mm=req.tolerance_mm,
        )
    except Exception as exc:
        raise _session_http_exception(
            exc, default_action="Soft-limit configuration"
        ) from exc
    return {"status": "ok"}


@router.post("/calibration/prepare-origin")
def prepare_calibration_origin() -> GantryPosition:
    _reject_if_run_active()
    session = _require_session()
    try:
        return _position_response(session.prepare_calibration_origin(), session=session)
    except Exception as exc:
        raise _session_http_exception(
            exc, default_action="Calibration preparation"
        ) from exc


@router.post("/calibration/home-and-center")
def calibration_home_and_center() -> CalibrationCenterResponse:
    _reject_if_run_active()
    try:
        result = _require_session().calibration_home_and_center()
    except Exception as exc:
        raise _session_http_exception(exc, default_action="Home and center") from exc
    return CalibrationCenterResponse(
        xy_bounds=result.xy_bounds,
        position=result.position,
    )


@router.post("/calibration/restore-soft-limits")
def restore_calibration_soft_limits() -> GantryPosition:
    _reject_if_run_active()
    session = _require_session()
    try:
        return _position_response(
            session.restore_calibration_soft_limits(),
            session=session,
        )
    except Exception as exc:
        raise _session_http_exception(
            exc, default_action="Soft-limit restore"
        ) from exc


@router.post("/calibration/finalize-origin")
def finalize_calibration_origin(req: FinalizeOriginRequest) -> FinalizeOriginResponse:
    _reject_if_run_active()
    try:
        result = _require_session().finalize_calibration_origin(
            home_z=req.home_z,
            block_touch_z=req.block_touch_z,
            block_height=req.block_height,
            factory_z_travel=req.factory_z_travel,
            tolerance_mm=req.tolerance_mm,
        )
    except Exception as exc:
        raise _session_http_exception(
            exc, default_action="Calibration finalization"
        ) from exc
    payload = asdict(result) if is_dataclass(result) else {
        "measured_volume": result.measured_volume,
        "z_calibration": result.z_calibration,
        "max_travel": result.max_travel,
        "position": result.position,
        "homing_pull_off_mm": result.homing_pull_off_mm,
    }
    return FinalizeOriginResponse(**payload)


@router.post("/calibration/recover-limit")
def recover_calibration_limit(req: LimitRecoveryRequest) -> LimitRecoveryResponse:
    try:
        result, messages = _require_session().recover_calibration_limit(
            x=req.x,
            y=req.y,
            z=req.z,
            pull_off_mm=req.pull_off_mm,
            feed_rate=req.feed_rate,
        )
    except ValueError as exc:
        raise _session_http_exception(exc, default_action="Limit recovery") from exc
    except GantryAlarmError as exc:
        raise HTTPException(
            409,
            "Limit recovery did not clear the gantry alarm. "
            f"Use E-stop/controller reset before continuing: {exc}",
        ) from exc
    except MillConnectionError as exc:
        raise HTTPException(
            503,
            "Controller connection lost during limit recovery. Power-cycle "
            "the controller, re-seat the USB cable, then Disconnect and "
            f"Connect again before continuing: {exc}",
        ) from exc
    except Exception as exc:
        if looks_like_limit_alarm(exc):
            raise HTTPException(
                409,
                "Limit recovery did not clear the gantry alarm. "
                f"Use E-stop/controller reset before continuing: {exc}",
            ) from exc
        raise _session_http_exception(exc, default_action="Limit recovery") from exc
    return LimitRecoveryResponse(
        status="recovered",
        attempts=result.attempts,
        pull_off=result.pull_off_delta,
        messages=messages,
    )


@router.post("/unlock")
def unlock() -> GantryPosition:
    session = _require_session()
    try:
        return _position_response(session.unlock(), session=session)
    except Exception as exc:
        raise _session_http_exception(exc, default_action="Unlock") from exc


@router.post("/reset-unlock")
def reset_and_unlock() -> GantryPosition:
    session = _require_session()
    try:
        return _position_response(session.reset_and_unlock(), session=session)
    except Exception as exc:
        raise _session_http_exception(exc, default_action="Reset and unlock") from exc


@router.post("/resume")
def resume() -> GantryPosition:
    session = _require_session()
    try:
        return _position_response(session.resume(), session=session)
    except Exception as exc:
        raise _session_http_exception(exc, default_action="Resume") from exc


@router.post("/feed-hold")
def feed_hold() -> GantryPosition:
    session = _require_session()
    try:
        return _position_response(session.feed_hold(), session=session)
    except Exception as exc:
        raise _session_http_exception(exc, default_action="Feed hold") from exc


@router.post("/jog-cancel")
def jog_cancel() -> GantryPosition:
    session = _require_session()
    try:
        # Realtime cancel (0x85) without waiting on the operation lock:
        # during a held jog the lock is contended by queued jog requests,
        # and a cancel that waits its turn arrives after the motion it was
        # meant to stop. position() is non-blocking (cache fallback).
        session.jog_cancel_interrupt()
        return _position_response(session.position(), session=session)
    except Exception as exc:
        raise _session_http_exception(exc, default_action="Jog cancel") from exc


@router.get("/grbl-settings")
def read_grbl_settings() -> GrblSettingsResponse:
    try:
        settings = _require_session().read_grbl_settings()
    except Exception as exc:
        raise _session_http_exception(exc, default_action="Read GRBL settings") from exc
    return GrblSettingsResponse(settings=settings)


@router.post("/grbl-settings")
def set_grbl_setting(req: SetGrblSettingRequest) -> GrblSettingsResponse:
    try:
        settings = _require_session().set_grbl_setting(req.setting, req.value)
    except Exception as exc:
        raise _session_http_exception(exc, default_action="Set GRBL setting") from exc
    return GrblSettingsResponse(settings=settings)


@router.post("/connect")
def connect(body: Optional[ConnectRequest] = None) -> GantryPosition:
    session = _get_or_create_session()
    if session.connected:
        raise HTTPException(409, "Gantry already connected; disconnect first")
    # Manually connected instruments belong to the previous config; the
    # import is deferred to avoid a circular import at module load.
    from cubos_api.routers.instruments import reset_manual_instruments

    reset_manual_instruments()
    try:
        filename, path = _selected_gantry_path(body.filename if body else None)
        snapshot = session.connect(path, filename=filename)
    except HTTPException:
        raise
    except (ValueError, ValidationError, yaml.YAMLError) as exc:
        raise HTTPException(400, f"Invalid gantry config: {exc}") from exc
    except Exception as exc:
        raise HTTPException(500, f"Failed to connect: {exc}") from exc
    return _position_response(snapshot, session=session)


@router.post("/disconnect")
def disconnect() -> GantryPosition:
    from cubos_api.routers.instruments import reset_manual_instruments

    reset_manual_instruments()
    session = current_session()
    if session is None:
        return GantryPosition(connected=False, status="Disconnected")
    try:
        return _position_response(session.disconnect(), session=session)
    except Exception as exc:
        raise _session_http_exception(exc, default_action="Disconnect") from exc


@router.get("/{filename}")
def get_gantry(filename: str) -> GantryResponse:
    try:
        path = resolve_config_path(get_settings().configs_dir, "gantry", filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(404, f"Config not found: {filename}")
    try:
        data = read_yaml(path)
        config = _validated_gantry_config(data)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return GantryResponse(filename=filename, config=_api_gantry_config(config))


@router.put("/{filename}")
def put_gantry(filename: str, body: dict) -> GantryResponse:
    try:
        path = resolve_config_path(get_settings().configs_dir, "gantry", filename)
        config = _validated_gantry_config(body)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(400, str(exc)) from exc
    config_dict = config.model_dump(mode="json", exclude_none=True)
    write_yaml(path, config_dict)
    session = current_session()
    if session is not None:
        session.refresh_connected_config(filename, config_dict)
    return get_gantry(filename)


@router.delete("/{filename}")
def delete_gantry(filename: str) -> dict:
    if run_active():
        raise HTTPException(409, "Cannot delete a gantry config while a protocol run is active")
    session = current_session()
    if session is not None and session.connected_gantry_filename == filename:
        raise HTTPException(409, f"{filename} is the connected gantry config; disconnect before deleting it")
    try:
        delete_config(get_settings().configs_dir, "gantry", filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError:
        raise HTTPException(404, f"Config not found: {filename}")
    return {"status": "deleted", "filename": filename}


def request_feed_hold_interrupt() -> None:
    session = _require_session()
    session.feed_hold_interrupt()


def request_jog_cancel_interrupt() -> None:
    session = _require_session()
    session.jog_cancel_interrupt()


def run_protocol_on_session(**kwargs: Any) -> Any:
    return _require_session().run_protocol(**kwargs)


def translate_interrupt_timeout(exc: InterruptFeedHoldTimeoutError) -> dict[str, str]:
    return {
        "status": "cancel_requested",
        "warning": str(exc),
    }
