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

import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from cubos.instruments.base_instrument import BaseInstrument, InstrumentError
from cubos.instruments.camera.exceptions import CameraError
from cubos.instruments.camera.interface import CameraInstrument
from cubos.instruments.lighting.exceptions import LightingError
from cubos.instruments.lighting.interface import LightingInstrument
from cubos.instruments.registry import get_instrument_class, validate_instrument
from cubos.protocol_engine.commands.camera import default_images_dir

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


def reset_manual_instruments() -> None:
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


@router.get("/camera/last-image")
def last_image(instrument: str) -> FileResponse:
    """Serve the most recent capture (preview or manual) for *instrument*."""
    with _manual_lock:
        saved = _last_capture.get(instrument)
    if saved is None or not Path(saved).is_file():
        raise HTTPException(404, f"No capture yet for instrument {instrument!r}.")
    return FileResponse(saved, media_type="image/png")
