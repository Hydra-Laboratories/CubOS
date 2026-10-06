"""Manual instrument control endpoints (outside protocol runs).

Lighting toggles and camera captures for bring-up work. Endpoints act on
the connected gantry config, reject with 409 while a run is active, and
cache instrument connections between calls (closing the Pawduino port
after every request would reset the Arduino and turn the lights off); the
cache clears when the gantry session connects or disconnects. Manual
captures land under ``<images root>/manual/`` and are not recorded in the
data store.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from cubos.instruments.base_instrument import BaseInstrument, InstrumentError
from cubos.instruments.camera.exceptions import CameraError
from cubos.instruments.camera.interface import CameraInstrument
from cubos.instruments.camera.vendors.opencv import OpenCVCamera
from cubos.instruments.lighting.exceptions import LightingError
from cubos.instruments.lighting.interface import LightingInstrument
from cubos.instruments.registry import get_instrument_class, validate_instrument
from cubos.deck import load_deck_from_yaml
from cubos.gantry.yaml_schema import GantryYamlSchema
from cubos.protocol_engine.commands.camera import default_images_dir
from cubos_api.config import get_settings

from cubos_api.models.camera_monitor import (
    CameraControlsResponse,
    CameraAlignmentPreviewRequest,
    CameraAlignmentProposal,
    CameraAlignmentSaveRequest,
    CameraAlignmentSaveResponse,
    CameraMonitorLeaseRequest,
    CameraMonitorRequest,
    CameraMonitorStatus,
    SetCameraControlsRequest,
)
from cubos_api.services.camera_monitor import (
    CameraMonitorError,
    CameraMonitorFrameExpired,
    CameraMonitorNotRunning,
    CameraMonitorUnsupported,
    get_camera_monitor_service,
)
from cubos_api.services.yaml_io import read_yaml, resolve_config_path, write_yaml

from .gantry import _reject_if_run_active, _require_session

router = APIRouter(prefix="/api/v1/instruments", tags=["instruments"])

_manual_instruments: Dict[str, BaseInstrument] = {}
_manual_lock = threading.Lock()
# Most recent capture of any kind (preview or manual) — feeds the live-preview
# image fetch, which always wants whatever was just captured.
_last_capture: Dict[str, str] = {}
# Most recent *manual* (non-preview) capture — feeds CameraInfo.last_image,
# which callers expect to be a deliberate archival capture, not a throwaway
# preview frame overwritten every ~800ms while the wizard is open.
_last_manual_capture: Dict[str, str] = {}
_capture_locks: Dict[str, threading.Lock] = {}
_alignment_lock = threading.Lock()
_ALIGNMENT_PROPOSAL_TTL_S = 300.0


def _capture_lock(instrument: str) -> threading.Lock:
    """Serialize capture calls per instrument (vendor handles aren't thread-safe)."""
    with _manual_lock:
        lock = _capture_locks.get(instrument)
        if lock is None:
            lock = threading.Lock()
            _capture_locks[instrument] = lock
        return lock


class LightingChannelInfo(BaseModel):
    instrument: str
    connected: bool
    channels: Dict[str, List[int]]
    active: Dict[str, int]


class SetLightsRequest(BaseModel):
    instrument: str
    channel: Optional[str] = None
    brightness: Optional[int] = None
    all_off: bool = False


class CameraInfo(BaseModel):
    instrument: str
    vendor: str
    connected: bool
    last_image: Optional[str] = None


class CaptureRequest(BaseModel):
    instrument: str
    label: Optional[str] = None
    # Live-preview polling: overwrite one fixed file per instrument instead of
    # writing a fresh timestamped file on every tick.
    preview: bool = False


class CaptureResponse(BaseModel):
    instrument: str
    image_path: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _alignment_idle_snapshot(gantry_file: str):
    _reject_if_run_active()
    from cubos_api.services.run_manager import active_reservation_owner

    if active_reservation_owner() is not None:
        raise HTTPException(409, "An external client reserves the station")
    with _manual_lock:
        if _manual_instruments:
            raise HTTPException(
                409, "Disconnect manual instruments before saving camera alignment",
            )
    session = _require_session()
    if session.calibration_active:
        raise HTTPException(409, "Finish gantry calibration before saving camera alignment")
    if session.connected_gantry_filename != gantry_file:
        raise HTTPException(
            409,
            f"Selected gantry {gantry_file!r} is not the connected config "
            f"{session.connected_gantry_filename!r}",
        )
    if session.operation_lock.locked():
        raise HTTPException(409, "The gantry is busy with a manual operation")
    snapshot = session.position()
    if session.operation_lock.locked():
        raise HTTPException(409, "The gantry became busy while reading its position")
    if not snapshot.connected or any(
        value is None or not math.isfinite(float(value))
        for value in (snapshot.work_x, snapshot.work_y, snapshot.work_z)
    ):
        raise HTTPException(409, "Camera alignment requires a known finite work position")
    if snapshot.status != "Idle" and not snapshot.status.startswith("<Idle|"):
        raise HTTPException(
            409, f"Camera alignment requires an Idle controller; observed {snapshot.status!r}",
        )
    return session, snapshot


def _alignment_proposal(
    request: CameraAlignmentPreviewRequest,
) -> CameraAlignmentProposal:
    session, snapshot = _alignment_idle_snapshot(request.gantry_file)
    settings = get_settings()
    gantry_path = resolve_config_path(
        settings.configs_dir, "gantry", request.gantry_file,
    )
    deck_path = resolve_config_path(settings.configs_dir, "deck", request.deck_file)
    if not gantry_path.is_file():
        raise HTTPException(404, f"Gantry config not found: {request.gantry_file}")
    if not deck_path.is_file():
        raise HTTPException(404, f"Deck config not found: {request.deck_file}")
    gantry_document = read_yaml(gantry_path)
    try:
        saved_config = GantryYamlSchema.model_validate(gantry_document)
        connected_config = GantryYamlSchema.model_validate(
            session.connected_gantry_config,
        )
    except Exception as exc:
        raise HTTPException(400, f"Invalid gantry configuration: {exc}") from exc
    saved_normalized = saved_config.model_dump(mode="json", exclude_none=True)
    connected_normalized = connected_config.model_dump(mode="json", exclude_none=True)
    if saved_normalized != connected_normalized:
        raise HTTPException(
            409,
            "The saved gantry file differs from the connected runtime; reload or "
            "reconnect it before aligning the camera",
        )
    instruments = gantry_document.get("instruments") or {}
    camera = instruments.get(request.camera_instrument)
    if not isinstance(camera, Mapping) or camera.get("type") != "camera":
        raise HTTPException(
            400, f"Instrument {request.camera_instrument!r} is not a camera",
        )
    try:
        monitor = get_camera_monitor_service().require_fresh_status(
            request.camera_instrument,
        )
    except (CameraMonitorNotRunning, CameraMonitorFrameExpired) as exc:
        raise HTTPException(409, str(exc)) from exc
    assert monitor.frame_id is not None
    assert monitor.received_at is not None
    assert monitor.frame_age_seconds is not None
    try:
        deck = load_deck_from_yaml(deck_path)
        target = deck.resolve_coordinate(request.target_position)
    except Exception as exc:
        raise HTTPException(
            400, f"Cannot resolve alignment target {request.target_position!r}: {exc}",
        ) from exc
    head = {
        "work_x": float(snapshot.work_x),
        "work_y": float(snapshot.work_y),
        "work_z": float(snapshot.work_z),
        "status": snapshot.status,
    }
    target_data = {"x": float(target.x), "y": float(target.y), "z": float(target.z)}
    before = {
        "offset_x": float(camera.get("offset_x", 0.0)),
        "offset_y": float(camera.get("offset_y", 0.0)),
    }
    after = {
        "offset_x": target_data["x"] - head["work_x"],
        "offset_y": target_data["y"] - head["work_y"],
    }
    gantry_sha256 = _sha256(gantry_path)
    deck_sha256 = _sha256(deck_path)
    facts = {
        "gantry_file": request.gantry_file,
        "gantry_sha256": gantry_sha256,
        "deck_file": request.deck_file,
        "deck_sha256": deck_sha256,
        "camera_instrument": request.camera_instrument,
        "target_position": request.target_position,
        "head": head,
        "target": target_data,
        "before": before,
        "after": after,
    }
    proposal_id = hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return CameraAlignmentProposal(
        proposal_id=proposal_id,
        **facts,
        camera_frame_id=monitor.frame_id,
        camera_frame_received_at=monitor.received_at,
        camera_frame_age_seconds=monitor.frame_age_seconds,
        calibration_warning=snapshot.calibration_warning,
        expires_at=time.time() + _ALIGNMENT_PROPOSAL_TTL_S,
    )


def reset_manual_instruments(*, preserve_camera_monitors: bool = False) -> None:
    """Disconnect and drop every manually connected instrument."""
    with _manual_lock:
        for name, instrument in _manual_instruments.items():
            try:
                instrument.disconnect()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
        _manual_instruments.clear()
        _last_capture.clear()
        _last_manual_capture.clear()
        _capture_locks.clear()
    if not preserve_camera_monitors:
        get_camera_monitor_service().stop_all()


def _configured_instruments() -> Dict[str, Dict[str, Any]]:
    session = _require_session()
    config = session.connected_gantry_config or {}
    instruments = config.get("instruments") or {}
    if not isinstance(instruments, dict):
        raise HTTPException(500, "Connected gantry config has invalid instruments.")
    return instruments


def _build_instrument(name: str, entry: Dict[str, Any]) -> BaseInstrument:
    kwargs = dict(entry)
    type_key = kwargs.pop("type", None)
    vendor = kwargs.pop("vendor", None)
    if not type_key or not vendor:
        raise HTTPException(
            500, f"Instrument {name!r} entry is missing type/vendor."
        )
    try:
        validate_instrument(type_key, vendor)
        cls = get_instrument_class(type_key, vendor)
        return cls(**kwargs)
    except (ValueError, TypeError, InstrumentError) as exc:
        raise HTTPException(400, f"Cannot build instrument {name!r}: {exc}") from exc


def _manual_instrument(name: str) -> BaseInstrument:
    """Return a connected instrument for *name*, building it on first use."""
    instruments = _configured_instruments()
    if name not in instruments:
        available = ", ".join(sorted(instruments)) or "none"
        raise HTTPException(
            404, f"No instrument {name!r} in the connected gantry config. "
            f"Available: {available}",
        )
    with _manual_lock:
        cached = _manual_instruments.get(name)
        if cached is not None:
            return cached
        instrument = _build_instrument(name, instruments[name])
        try:
            instrument.connect()
        except Exception as exc:
            raise HTTPException(
                502, f"Failed to connect instrument {name!r}: {exc}"
            ) from exc
        _manual_instruments[name] = instrument
        return instrument


def _lighting_instrument(name: str) -> LightingInstrument:
    instrument = _manual_instrument(name)
    if not isinstance(instrument, LightingInstrument):
        raise HTTPException(
            400, f"Instrument {name!r} is a {type(instrument).__name__}, "
            "not a lighting instrument.",
        )
    return instrument


def _camera_instrument(name: str) -> CameraInstrument:
    instrument = _manual_instrument(name)
    if not isinstance(instrument, CameraInstrument):
        raise HTTPException(
            400, f"Instrument {name!r} is a {type(instrument).__name__}, "
            "not a camera instrument.",
        )
    return instrument


def _camera_entry(name: str) -> Dict[str, Any]:
    instruments = _configured_instruments()
    entry = instruments.get(name)
    if entry is None:
        available = ", ".join(sorted(instruments)) or "none"
        raise HTTPException(
            404,
            f"No instrument {name!r} in the connected gantry config. "
            f"Available: {available}",
        )
    if not isinstance(entry, dict) or entry.get("type") != "camera":
        raise HTTPException(400, f"Instrument {name!r} is not a camera instrument.")
    return entry


def _monitor_configuration(entry: Dict[str, Any]) -> tuple[Any, ...]:
    return (
        entry.get("vendor"),
        entry.get("camera_id", -1),
        entry.get("resolution_width", 1280),
        entry.get("resolution_height", 720),
        entry.get("pixel_format"),
        entry.get("unsupported_controls", ""),
    )


def _entries_of_type(type_key: str) -> Dict[str, Dict[str, Any]]:
    return {
        name: entry
        for name, entry in _configured_instruments().items()
        if isinstance(entry, dict) and entry.get("type") == type_key
    }


@router.get("/lighting")
def list_lighting() -> List[LightingChannelInfo]:
    """Describe every lighting instrument on the connected gantry config."""
    infos: List[LightingChannelInfo] = []
    for name, entry in _entries_of_type("lighting").items():
        with _manual_lock:
            cached = _manual_instruments.get(name)
        if isinstance(cached, LightingInstrument):
            channels = cached.channels
            active = cached.status().channels
            connected = True
        else:
            probe = _build_instrument(name, entry)
            if not isinstance(probe, LightingInstrument):
                continue
            channels = probe.channels
            active = {channel: 0 for channel in channels}
            connected = False
        infos.append(
            LightingChannelInfo(
                instrument=name,
                connected=connected,
                channels={ch: list(levels) for ch, levels in channels.items()},
                active=dict(active),
            )
        )
    return infos


@router.post("/lighting/set")
def set_lights(req: SetLightsRequest) -> LightingChannelInfo:
    """Manually set one lighting channel (or all off). Rejected mid-run."""
    _reject_if_run_active()
    lighting = _lighting_instrument(req.instrument)
    try:
        if req.all_off:
            if req.channel is not None or req.brightness is not None:
                raise HTTPException(
                    400, "all_off cannot be combined with channel/brightness.",
                )
            lighting.all_off()
        else:
            if req.channel is None or req.brightness is None:
                raise HTTPException(
                    400, "Provide either all_off or both channel and brightness.",
                )
            lighting.set_channel(req.channel, req.brightness)
    except LightingError as exc:
        raise HTTPException(400, str(exc)) from exc
    return LightingChannelInfo(
        instrument=req.instrument,
        connected=True,
        channels={ch: list(levels) for ch, levels in lighting.channels.items()},
        active=dict(lighting.status().channels),
    )


@router.get("/camera")
def list_cameras() -> List[CameraInfo]:
    """Describe every camera instrument on the connected gantry config."""
    infos: List[CameraInfo] = []
    for name, entry in _entries_of_type("camera").items():
        with _manual_lock:
            connected = name in _manual_instruments
            last = _last_manual_capture.get(name)
        connected = connected or get_camera_monitor_service().is_running(name)
        infos.append(
            CameraInfo(
                instrument=name,
                vendor=str(entry.get("vendor", "")),
                connected=connected,
                last_image=last,
            )
        )
    return infos


@router.post("/camera/capture")
def manual_capture(req: CaptureRequest) -> CaptureResponse:
    """Capture one image wherever the gantry currently is. Rejected mid-run."""
    _reject_if_run_active()
    camera = _camera_instrument(req.instrument)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", (req.label or req.instrument)).strip("-")
    directory = default_images_dir() / "manual"
    directory.mkdir(parents=True, exist_ok=True)
    if req.preview:
        # Overwrite one fixed file per instrument so polling doesn't pile up
        # a new PNG on disk every tick.
        path = directory / f"{stem or 'image'}_preview.png"
    else:
        path = directory / f"{stem or 'image'}_{time.strftime('%Y%m%d-%H%M%S')}.png"
        counter = 1
        while path.exists():
            path = directory / f"{stem}_{time.strftime('%Y%m%d-%H%M%S')}_{counter:03d}.png"
            counter += 1
    try:
        with _capture_lock(req.instrument):
            saved = camera.capture(save_path=str(path))
    except CameraError as exc:
        raise HTTPException(502, f"Capture failed: {exc}") from exc
    except NotImplementedError as exc:
        raise HTTPException(
            501, f"Camera {req.instrument!r} does not support capture: {exc}"
        ) from exc
    with _manual_lock:
        _last_capture[req.instrument] = saved
        if not req.preview:
            _last_manual_capture[req.instrument] = saved
    return CaptureResponse(instrument=req.instrument, image_path=saved)


@router.post(
    "/camera/alignment/preview",
    response_model=CameraAlignmentProposal,
)
def preview_camera_alignment(
    request: CameraAlignmentPreviewRequest,
) -> CameraAlignmentProposal:
    """Propose camera XY offsets from the current head and a saved deck target."""
    return _alignment_proposal(request)


@router.post(
    "/camera/alignment/save",
    response_model=CameraAlignmentSaveResponse,
)
def save_camera_alignment(
    request: CameraAlignmentSaveRequest,
) -> CameraAlignmentSaveResponse:
    """Persist an unchanged alignment proposal without moving the gantry."""
    proposal = request.proposal
    if proposal.expires_at < time.time():
        raise HTTPException(409, "Camera alignment proposal expired; preview it again")
    preview_request = CameraAlignmentPreviewRequest(
        gantry_file=proposal.gantry_file,
        deck_file=proposal.deck_file,
        camera_instrument=proposal.camera_instrument,
        target_position=proposal.target_position,
    )
    with _alignment_lock:
        current = _alignment_proposal(preview_request)
        if current.proposal_id != proposal.proposal_id:
            raise HTTPException(
                409,
                "Camera alignment inputs changed after preview; inspect the current "
                "head, deck, and offsets, then preview again",
            )
        session, snapshot = _alignment_idle_snapshot(proposal.gantry_file)
        current_head = current.head
        if (
            float(snapshot.work_x) != current_head.work_x
            or float(snapshot.work_y) != current_head.work_y
            or float(snapshot.work_z) != current_head.work_z
        ):
            raise HTTPException(
                409, "The gantry position changed after alignment preview; preview again",
            )
        settings = get_settings()
        gantry_path = resolve_config_path(
            settings.configs_dir, "gantry", proposal.gantry_file,
        )
        if _sha256(gantry_path) != proposal.gantry_sha256:
            raise HTTPException(
                409, "The gantry file changed after alignment preview; preview again",
            )
        document = read_yaml(gantry_path)
        instruments = document.get("instruments") or {}
        camera = instruments.get(proposal.camera_instrument)
        if not isinstance(camera, MutableMapping) or camera.get("type") != "camera":
            raise HTTPException(409, "The selected camera definition changed")
        camera["offset_x"] = current.after.offset_x
        camera["offset_y"] = current.after.offset_y
        try:
            validated = GantryYamlSchema.model_validate(document)
        except Exception as exc:
            raise HTTPException(
                400, f"Updated camera offsets do not form a valid gantry config: {exc}",
            ) from exc
        runtime_config = validated.model_dump(mode="json", exclude_none=True)
        def persist_alignment() -> None:
            from cubos_api.services.run_manager import active_reservation_owner

            _reject_if_run_active()
            if active_reservation_owner() is not None:
                raise HTTPException(
                    409, "A reservation started before alignment save; preview again",
                )
            with _manual_lock:
                if _manual_instruments:
                    raise HTTPException(
                        409,
                        "A manual instrument connected before alignment save; "
                        "disconnect it and preview again",
                    )
            try:
                get_camera_monitor_service().require_fresh_status(
                    proposal.camera_instrument,
                )
            except (CameraMonitorNotRunning, CameraMonitorFrameExpired) as exc:
                raise HTTPException(409, str(exc)) from exc
            if _sha256(gantry_path) != proposal.gantry_sha256:
                raise HTTPException(
                    409, "The gantry file changed during alignment save; preview again",
                )
            write_yaml(gantry_path, document)

        try:
            session.apply_camera_alignment_config(
                proposal.gantry_file,
                expected_work_position=(
                    current.head.work_x,
                    current.head.work_y,
                    current.head.work_z,
                ),
                config=runtime_config,
                persist=persist_alignment,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        runtime_camera = (
            (session.connected_gantry_config or {}).get("instruments") or {}
        ).get(proposal.camera_instrument)
        if not isinstance(runtime_camera, Mapping) or (
            float(runtime_camera.get("offset_x", math.nan)) != current.after.offset_x
            or float(runtime_camera.get("offset_y", math.nan)) != current.after.offset_y
        ):
            raise HTTPException(
                500,
                "Camera offsets were saved, but the connected runtime did not refresh; "
                "disconnect and reconnect before any motion",
            )
        # TODO(iter): test stale file/position rejection, the remaining
        # RunManager reservation concurrency boundary, and comment-preserving
        # round trips after the operator completes the first alignment iteration.
        return CameraAlignmentSaveResponse(
            proposal=current,
            saved_gantry_sha256=_sha256(gantry_path),
        )


@router.post("/camera/monitor/start", response_model=CameraMonitorStatus)
def start_camera_monitor(req: CameraMonitorRequest) -> CameraMonitorStatus:
    """Acquire an explicit preview lease for one configured OpenCV camera."""
    entry = _camera_entry(req.instrument)
    if entry.get("vendor") != "opencv":
        raise HTTPException(501, "Live monitoring currently supports OpenCV cameras only.")
    service = get_camera_monitor_service()
    camera = _build_instrument(req.instrument, entry)
    if not isinstance(camera, OpenCVCamera):
        raise HTTPException(501, "Live monitoring currently supports OpenCV cameras only.")
    try:
        return service.start(
            req.instrument,
            camera,
            configuration=_monitor_configuration(entry),
        )
    except CameraMonitorUnsupported as exc:
        raise HTTPException(501, str(exc)) from exc
    except CameraMonitorError as exc:
        raise HTTPException(502, f"Camera monitor failed to start: {exc}") from exc


@router.post("/camera/monitor/heartbeat", response_model=CameraMonitorStatus)
def heartbeat_camera_monitor(req: CameraMonitorLeaseRequest) -> CameraMonitorStatus:
    """Renew one preview subscriber without changing camera ownership."""
    _camera_entry(req.instrument)
    try:
        return get_camera_monitor_service().heartbeat(
            req.instrument, req.lease_id
        )
    except CameraMonitorNotRunning as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/camera/monitor/stop", response_model=CameraMonitorStatus)
def stop_camera_monitor(req: CameraMonitorLeaseRequest) -> CameraMonitorStatus:
    """Release this UI monitor's lease without affecting a protocol lease."""
    _camera_entry(req.instrument)
    return get_camera_monitor_service().stop(req.instrument, req.lease_id)


@router.get("/camera/monitor", response_model=CameraMonitorStatus)
def camera_monitor_status(instrument: str) -> CameraMonitorStatus:
    """Read monitor state without opening any instrument."""
    _camera_entry(instrument)
    return get_camera_monitor_service().status(instrument)


@router.get("/camera/monitor/frame")
def camera_monitor_frame(instrument: str, frame_id: int | None = None) -> Response:
    """Serve the latest leased JPEG frame without consuming camera input."""
    _camera_entry(instrument)
    try:
        encoded, actual_frame_id, received_at = (
            get_camera_monitor_service().frame(instrument, frame_id)
        )
    except CameraMonitorFrameExpired as exc:
        raise HTTPException(409, str(exc)) from exc
    except CameraMonitorNotRunning as exc:
        raise HTTPException(409, str(exc)) from exc
    except CameraMonitorError as exc:
        raise HTTPException(503, str(exc)) from exc
    return Response(
        encoded,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-CubOS-Frame-Id": str(actual_frame_id),
            "X-CubOS-Received-At": str(received_at),
        },
    )


@router.get("/camera/controls", response_model=CameraControlsResponse)
def camera_controls(instrument: str) -> CameraControlsResponse:
    """Read controls from an existing monitor lease; never auto-connect."""
    _camera_entry(instrument)
    try:
        return get_camera_monitor_service().controls(instrument)
    except CameraMonitorNotRunning as exc:
        raise HTTPException(409, str(exc)) from exc
    except CameraMonitorError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/camera/monitor/analysis-frame", response_class=FileResponse)
def camera_monitor_analysis_frame(instrument: str) -> FileResponse:
    """Serve the latest scored image selected from trusted run metadata."""
    _camera_entry(instrument)
    try:
        path = get_camera_monitor_service().analysis_image_path(instrument)
    except CameraMonitorError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, headers={"Cache-Control": "no-store"})


@router.patch("/camera/controls", response_model=CameraControlsResponse)
def set_camera_controls(req: SetCameraControlsRequest) -> CameraControlsResponse:
    """Apply only explicitly supplied controls to an active camera lease."""
    _reject_if_run_active()
    _camera_entry(req.instrument)
    updates = req.controls.model_dump(exclude_none=True, exclude_unset=True)
    try:
        return get_camera_monitor_service().set_controls(req.instrument, updates)
    except CameraMonitorNotRunning as exc:
        raise HTTPException(409, str(exc)) from exc
    except CameraMonitorError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/camera/last-image")
def last_image(instrument: str) -> FileResponse:
    """Serve the most recent capture (preview or manual) for *instrument*."""
    with _manual_lock:
        saved = _last_capture.get(instrument)
    if saved is None or not Path(saved).is_file():
        raise HTTPException(404, f"No capture yet for instrument {instrument!r}.")
    return FileResponse(saved, media_type="image/png")
