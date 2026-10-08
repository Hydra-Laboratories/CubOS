"""USB webcam camera driver via OpenCV (optional opencv-python dependency)."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from pathlib import Path
from typing import Any, Optional

from cubos.instruments.camera.exceptions import (
    CameraCaptureError,
    CameraConnectionError,
)
from cubos.instruments.camera.interface import CameraInstrument
from cubos.instruments.camera.frame_broker import (
    CameraFrame,
    FrameLease,
    active_frame_broker_keys,
    acquire_frame_broker,
)
from cubos.instruments.camera.placeholder import write_placeholder_png

_AUTO_DETECT_MAX_INDEX = 10
_CONNECT_LOCK = threading.Lock()
_CAPTURE_TIMEOUT_S = 2.0
_CONTROL_PROPERTIES = {
    "exposure": "CAP_PROP_EXPOSURE",
    "white_balance": "CAP_PROP_WB_TEMPERATURE",
    "auto_white_balance": "CAP_PROP_AUTO_WB",
    "focus": "CAP_PROP_FOCUS",
    "brightness": "CAP_PROP_BRIGHTNESS",
}
# V4L2 UVC backends reject OpenCV's 0.25 and want the raw auto_exposure menu value 1.
_MANUAL_MODE_PROPERTIES = {
    "exposure": ("CAP_PROP_AUTO_EXPOSURE", (0.25, 1.0)),
    "white_balance": ("CAP_PROP_AUTO_WB", (0.0,)),
    "focus": ("CAP_PROP_AUTOFOCUS", (0.0,)),
}
_READBACK_TOLERANCE = 1e-3
_PROFILE_PROPERTIES = {
    "auto_exposure": "CAP_PROP_AUTO_EXPOSURE",
    "auto_white_balance": "CAP_PROP_AUTO_WB",
    "autofocus": "CAP_PROP_AUTOFOCUS",
}


class OpenCVCamera(CameraInstrument):
    """USB webcam driven through ``cv2.VideoCapture``.

    ``camera_id`` < 0 (the default) auto-detects the first responsive
    device index at connect time.
    """

    def __init__(
        self,
        camera_id: int = -1,
        resolution_width: int = 1280,
        resolution_height: int = 720,
        pixel_format: str | None = None,
        unsupported_controls: str = "",
        name: Optional[str] = None,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
        depth: float = 0.0,
        offline: bool = False,
        **kwargs,
    ):
        super().__init__(
            name=name,
            offset_x=offset_x,
            offset_y=offset_y,
            depth=depth,
            offline=offline,
        )
        self.camera_id = camera_id
        self.resolution = (resolution_width, resolution_height)
        if pixel_format is not None and (
            len(pixel_format) != 4 or not pixel_format.isascii()
        ):
            raise ValueError("pixel_format must be a four-character ASCII code")
        self.pixel_format = pixel_format
        self._lease: FrameLease | None = None
        self._cv2 = None
        configured_unsupported = {
            name.strip()
            for name in unsupported_controls.split(",")
            if name.strip()
        }
        unknown_controls = sorted(configured_unsupported - set(_CONTROL_PROPERTIES))
        if unknown_controls:
            raise ValueError(
                "unsupported_controls contains unknown names: "
                + ", ".join(unknown_controls)
            )
        self.unsupported_controls = ",".join(sorted(configured_unsupported))
        self._unsupported_controls = {
            name: "Configured as unsupported for this camera"
            for name in configured_unsupported
        }
        self._confirmed_controls: set[str] = set()
        self._last_captured_frame: CameraFrame | None = None
        self._last_capture_profile: dict[str, object] | None = None
        self._last_image_sha256: str | None = None

    # ── BaseInstrument interface ──────────────────────────────────────────

    def connect(self) -> None:
        if self._offline:
            self.logger.info("OpenCV camera connected (offline)")
            return
        import cv2

        if self._lease is not None and self._lease.is_open():
            return
        with _CONNECT_LOCK:
            camera_id = self.camera_id
            if camera_id < 0:
                active_ids = [
                    key[1]
                    for key in active_frame_broker_keys()
                    if isinstance(key, tuple)
                    and len(key) == 2
                    and key[0] == "opencv"
                    and isinstance(key[1], int)
                ]
                if len(active_ids) == 1:
                    camera_id = active_ids[0]
                elif len(active_ids) > 1:
                    raise CameraConnectionError(
                        "Multiple OpenCV cameras are active; configure camera_id "
                        "explicitly instead of auto-detecting."
                    )
                else:
                    camera_id = self._detect_camera(cv2)

            def open_capture():
                capture = cv2.VideoCapture(camera_id)
                if not capture.isOpened():
                    capture.release()
                    raise CameraConnectionError(
                        f"Cannot open webcam at index {camera_id}."
                    )
                if self.pixel_format is not None:
                    capture.set(
                        cv2.CAP_PROP_FOURCC,
                        cv2.VideoWriter_fourcc(*self.pixel_format),
                    )
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
                return capture

            self._lease = acquire_frame_broker(
                ("opencv", camera_id),
                open_capture,
                configuration=(
                    self.resolution,
                    self.pixel_format,
                    self.unsupported_controls,
                ),
                metadata_reader=lambda capture: self._read_capture_metadata(
                    capture, cv2
                ),
            )
        self._cv2 = cv2
        self.camera_id = camera_id
        self.logger.info("Connected to webcam ID %d", camera_id)

    def disconnect(self) -> None:
        if self._offline:
            self.logger.info("OpenCV camera disconnected (offline)")
            return
        if self._lease is not None:
            self._lease.release()
            self._lease = None
        self.logger.info("Disconnected from webcam")

    def health_check(self) -> bool:
        if self._offline:
            return True
        return self._lease is not None and self._lease.is_open()

    # ── CameraInstrument interface ────────────────────────────────────────

    def capture(self, *args: Any, save_path: str = "", **kwargs: Any) -> str:
        """Capture one frame and save it to *save_path*; return the path."""
        if not save_path:
            raise CameraCaptureError("capture requires a save_path.")
        if self._offline:
            return str(write_placeholder_png(save_path))
        if self._lease is None or not self._lease.is_open():
            raise CameraCaptureError("Cannot capture image: camera not connected.")
        previous = self._latest_or_none()
        after_frame_id = previous.frame_id if previous is not None else None
        try:
            frame = self._lease.latest(
                after_frame_id=after_frame_id,
                timeout_s=_CAPTURE_TIMEOUT_S,
            )
        except CameraCaptureError as exc:
            raise CameraCaptureError(
                f"Failed to capture image from webcam: {exc}"
            ) from exc
        target = Path(save_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not self._cv2.imwrite(str(target), frame.data):
            raise CameraCaptureError(f"Failed to write image to {target}")
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        self._last_captured_frame = frame
        self._last_capture_profile = self._profile_for_frame(frame)
        self._last_image_sha256 = digest.hexdigest()
        self.logger.info("Image saved to %s", target)
        return str(target)

    def latest_frame(
        self,
        *,
        after_frame_id: int | None = None,
        timeout_s: float = 0.0,
    ) -> CameraFrame:
        """Return a shared live frame without consuming a protocol capture."""
        if self._lease is None or not self._lease.is_open():
            raise CameraCaptureError("Cannot read preview: camera not connected.")
        return self._lease.latest(
            after_frame_id=after_frame_id,
            timeout_s=timeout_s,
        )

    def encode_jpeg(self, frame: CameraFrame) -> bytes:
        if self._cv2 is None:
            raise CameraCaptureError("Cannot encode preview: camera not connected.")
        ok, encoded = self._cv2.imencode(".jpg", frame.data)
        if not ok:
            raise CameraCaptureError("Failed to encode camera preview as JPEG.")
        return bytes(encoded)

    def control_status(self) -> dict[str, dict[str, object]]:
        """Read supported optical controls without changing device settings."""
        if self._lease is None or not self._lease.is_open() or self._cv2 is None:
            raise CameraCaptureError("Cannot read controls: camera not connected.")
        statuses: dict[str, dict[str, object]] = {}
        for name, constant_name in _CONTROL_PROPERTIES.items():
            property_id = getattr(self._cv2, constant_name, None)
            known_error = self._unsupported_controls.get(name)
            if property_id is None or known_error is not None:
                statuses[name] = {
                    "supported": False,
                    "value": None,
                    "error": known_error or f"OpenCV does not expose {constant_name}",
                }
                continue
            try:
                value = self._lease.get_property(property_id)
            except Exception as exc:
                statuses[name] = {
                    "supported": False,
                    "value": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                continue
            supported = math.isfinite(value)
            statuses[name] = {
                "supported": True if name in self._confirmed_controls else None,
                "value": value if supported else None,
                "error": (
                    None
                    if name in self._confirmed_controls
                    else "Writable support has not been confirmed"
                )
                if supported
                else "Camera returned no finite value",
            }
        return statuses

    def set_controls(
        self, controls: dict[str, float | bool]
    ) -> dict[str, dict[str, object]]:
        """Apply only the explicitly requested controls and return readback."""
        if self._lease is None or not self._lease.is_open() or self._cv2 is None:
            raise CameraCaptureError("Cannot set controls: camera not connected.")
        unknown = sorted(set(controls) - set(_CONTROL_PROPERTIES))
        if unknown:
            raise CameraCaptureError(f"Unknown camera controls: {', '.join(unknown)}")
        if controls.get("auto_white_balance") is True and "white_balance" in controls:
            raise CameraCaptureError(
                "Cannot set white_balance while enabling auto_white_balance."
            )
        accepted_any = False
        for name, value in controls.items():
            if not math.isfinite(float(value)):
                raise CameraCaptureError(f"Camera control {name!r} must be finite.")
            constant_name = _CONTROL_PROPERTIES[name]
            property_id = getattr(self._cv2, constant_name, None)
            if property_id is None:
                self._unsupported_controls[name] = (
                    f"OpenCV does not expose {constant_name}"
                )
                continue
            requested = (
                1.0 if value is True else 0.0 if value is False else float(value)
            )
            try:
                mode = _MANUAL_MODE_PROPERTIES.get(name)
                mode_accepted = True
                if mode is not None:
                    mode_property = getattr(self._cv2, mode[0], None)
                    if mode_property is not None:
                        mode_accepted = any(
                            self._lease.set_property(mode_property, candidate)
                            for candidate in mode[1]
                        )
                value_accepted = self._lease.set_property(property_id, requested)
                readback = (
                    self._lease.get_property(property_id) if value_accepted else None
                )
            except Exception as exc:
                accepted = False
                detail = f"{type(exc).__name__}: {exc}"
            else:
                matches = value_accepted and math.isclose(
                    readback,
                    requested,
                    rel_tol=_READBACK_TOLERANCE,
                    abs_tol=_READBACK_TOLERANCE,
                )
                accepted = mode_accepted and matches
                if not value_accepted:
                    detail = "Camera rejected this control"
                elif not matches:
                    detail = f"Camera reports {readback:g} after requesting {requested:g}"
                else:
                    detail = ""
            if accepted:
                accepted_any = True
                self._unsupported_controls.pop(name, None)
                self._confirmed_controls.add(name)
            else:
                self._confirmed_controls.discard(name)
                self._unsupported_controls[name] = (
                    "Camera rejected manual mode for this control"
                    if not mode_accepted
                    else detail
                )
        if accepted_any:
            current = self._latest_or_none()
            after_frame_id = current.frame_id if current is not None else None
            self._lease.latest(
                after_frame_id=after_frame_id,
                timeout_s=_CAPTURE_TIMEOUT_S,
            )
        return self.control_status()

    def control_fingerprint(self) -> dict[str, object]:
        """Return the stable optical configuration used for color profiles."""
        latest = self._latest_or_none()
        return self._profile_for_frame(latest)

    def _profile_for_frame(self, latest: CameraFrame | None) -> dict[str, object]:
        actual_resolution = None
        capture_metadata: dict[str, Any] = {}
        configuration_revision = self._lease.configuration_revision if self._lease else 0
        if latest is not None:
            actual_resolution = {"width": latest.width, "height": latest.height}
            capture_metadata = latest.capture_metadata
            configuration_revision = latest.configuration_revision
        payload: dict[str, object] = {
            "schema": "cubos.opencv-capture.v1",
            "vendor": "opencv",
            "camera_id": self.camera_id,
            "requested_resolution": {
                "width": self.resolution[0],
                "height": self.resolution[1],
            },
            "actual_resolution": actual_resolution,
            "requested_pixel_format": self.pixel_format,
            "actual_pixel_format": capture_metadata.get("pixel_format"),
            "controls": capture_metadata.get("controls", {}),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        payload["fingerprint"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        payload["configuration_revision"] = configuration_revision
        return payload

    def last_frame_metadata(self) -> dict[str, object] | None:
        """Describe the exact frame most recently saved by ``capture``."""
        frame = self._last_captured_frame
        if frame is None:
            return None
        return {
            "frame_id": frame.frame_id,
            "received_at": frame.received_at,
            "width": frame.width,
            "height": frame.height,
            "configuration_revision": frame.configuration_revision,
            "capture_profile": self._last_capture_profile,
            "image_sha256": self._last_image_sha256,
        }

    def _read_capture_metadata(self, capture: Any, cv2: Any) -> dict[str, Any]:
        controls: dict[str, dict[str, object]] = {}
        properties = {**_CONTROL_PROPERTIES, **_PROFILE_PROPERTIES}
        for name, constant_name in properties.items():
            property_id = getattr(cv2, constant_name, None)
            if property_id is None:
                controls[name] = {"supported": False, "value": None}
                continue
            try:
                value = float(capture.get(property_id))
            except Exception:
                controls[name] = {"supported": False, "value": None}
            else:
                controls[name] = {
                    "supported": (
                        True
                        if name in self._confirmed_controls
                        else False
                        if name in self._unsupported_controls
                        else None
                    ),
                    "value": round(value, 6) if math.isfinite(value) else None,
                }
        property_id = getattr(cv2, "CAP_PROP_FOURCC", None)
        pixel_format = None
        try:
            value = int(capture.get(property_id)) if property_id is not None else 0
        except Exception:
            value = 0
        if value:
            characters = "".join(
                chr((value >> (8 * index)) & 0xFF) for index in range(4)
            )
            if all(32 <= ord(char) <= 126 for char in characters):
                pixel_format = characters
        return {"pixel_format": pixel_format, "controls": controls}

    def _latest_or_none(self) -> CameraFrame | None:
        if self._lease is None:
            return None
        try:
            return self._lease.latest()
        except CameraCaptureError:
            return None

    # ── Private helpers ───────────────────────────────────────────────────

    def _detect_camera(self, cv2) -> int:
        for index in range(_AUTO_DETECT_MAX_INDEX):
            candidate = cv2.VideoCapture(index)
            if candidate.isOpened():
                candidate.release()
                self.logger.info("Auto-detected webcam at index %d", index)
                return index
            candidate.release()
        raise CameraConnectionError(
            f"No responsive webcam found in indexes 0..{_AUTO_DETECT_MAX_INDEX - 1}."
        )
