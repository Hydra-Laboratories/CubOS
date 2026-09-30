"""Explicitly leased live previews over the shared OpenCV frame reader."""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Hashable
from urllib.parse import quote

import yaml

from cubos.instruments.camera.exceptions import CameraCaptureError
from cubos.instruments.camera.vendors.opencv import OpenCVCamera
from cubos_api.models.camera_monitor import (
    CameraControlValue,
    CameraControlsResponse,
    CameraMonitorStatus,
    CameraResolution,
)


class CameraMonitorError(RuntimeError):
    pass


class CameraMonitorNotRunning(CameraMonitorError):
    pass


class CameraMonitorUnsupported(CameraMonitorError):
    pass


class CameraMonitorFrameExpired(CameraMonitorError):
    pass


ContextProvider = Callable[[], dict[str, Any]]


@dataclass
class _Monitor:
    camera: OpenCVCamera
    configuration: Hashable
    leases: dict[str, float] = field(default_factory=dict)
    frame_cache: OrderedDict[int, tuple[bytes, float]] = field(
        default_factory=OrderedDict
    )
    expiry_timer: threading.Timer | None = None


class CameraMonitorService:
    """Own monitor leases independently from manual and protocol instruments."""

    def __init__(
        self,
        *,
        context_provider: ContextProvider | None = None,
        clock: Callable[[], float] = time.time,
        initial_frame_timeout_s: float = 2.0,
        maximum_frame_age_s: float = 3.0,
        lease_ttl_s: float = 15.0,
        encoded_frame_cache_size: int = 3,
    ) -> None:
        self._lock = threading.RLock()
        self._monitors: dict[str, _Monitor] = {}
        self._errors: dict[str, str] = {}
        self._context_provider = context_provider or _active_execution_context
        self._clock = clock
        self._initial_frame_timeout_s = initial_frame_timeout_s
        self._maximum_frame_age_s = maximum_frame_age_s
        self._lease_ttl_s = lease_ttl_s
        self._encoded_frame_cache_size = encoded_frame_cache_size

    def is_running(self, instrument: str) -> bool:
        with self._lock:
            monitor = self._active_monitor_locked(instrument)
            return monitor is not None and monitor.camera.health_check()

    def start(
        self,
        instrument: str,
        camera: Any,
        *,
        configuration: Hashable,
    ) -> CameraMonitorStatus:
        if not isinstance(camera, OpenCVCamera):
            raise CameraMonitorUnsupported(
                "Live monitoring currently supports OpenCV cameras only."
            )
        with self._lock:
            existing = self._active_monitor_locked(instrument)
            if existing is not None and existing.camera.health_check():
                if existing.configuration != configuration:
                    raise CameraMonitorError(
                        "Camera monitor is already running with a different "
                        "capture configuration."
                    )
                lease_id, expires_at = self._add_lease_locked(
                    instrument, existing
                )
                return self._status(
                    instrument,
                    existing,
                    lease_id=lease_id,
                    lease_expires_at=expires_at,
                )
            if existing is not None:
                existing.camera.disconnect()
                self._monitors.pop(instrument, None)
            try:
                camera.connect()
                camera.latest_frame(timeout_s=self._initial_frame_timeout_s)
            except Exception as exc:
                try:
                    camera.disconnect()
                except Exception:
                    pass
                self._errors[instrument] = f"{type(exc).__name__}: {exc}"
                raise CameraMonitorError(self._errors[instrument]) from exc
            monitor = _Monitor(camera=camera, configuration=configuration)
            self._monitors[instrument] = monitor
            self._errors.pop(instrument, None)
            lease_id, expires_at = self._add_lease_locked(instrument, monitor)
            return self._status(
                instrument,
                monitor,
                lease_id=lease_id,
                lease_expires_at=expires_at,
            )

    def heartbeat(
        self,
        instrument: str,
        lease_id: str,
    ) -> CameraMonitorStatus:
        with self._lock:
            monitor = self._active_monitor_locked(instrument)
            if monitor is None or lease_id not in monitor.leases:
                raise CameraMonitorNotRunning(
                    "Camera monitor lease is missing or expired. Start a new monitor."
                )
            expires_at = self._clock() + self._lease_ttl_s
            monitor.leases[lease_id] = expires_at
            self._schedule_expiry_locked(instrument, monitor)
            return self._status(
                instrument,
                monitor,
                lease_id=lease_id,
                lease_expires_at=expires_at,
            )

    def stop(self, instrument: str, lease_id: str) -> CameraMonitorStatus:
        with self._lock:
            monitor = self._active_monitor_locked(instrument)
            if monitor is None:
                return CameraMonitorStatus(
                    instrument=instrument,
                    state="stopped",
                    connected=False,
                )
            monitor.leases.pop(lease_id, None)
            if monitor.leases:
                self._schedule_expiry_locked(instrument, monitor)
                return self._status(instrument, monitor)
            self._remove_monitor_locked(instrument, monitor)
        monitor.camera.disconnect()
        return CameraMonitorStatus(
            instrument=instrument,
            state="stopped",
            connected=False,
        )

    def stop_all(self) -> None:
        with self._lock:
            monitors = list(self._monitors.values())
            self._monitors.clear()
            self._errors.clear()
            for monitor in monitors:
                if monitor.expiry_timer is not None:
                    monitor.expiry_timer.cancel()
        for monitor in monitors:
            try:
                monitor.camera.disconnect()
            except Exception:
                pass

    def status(self, instrument: str) -> CameraMonitorStatus:
        with self._lock:
            monitor = self._active_monitor_locked(instrument)
            error = self._errors.get(instrument)
            if monitor is None:
                return CameraMonitorStatus(
                    instrument=instrument,
                    state="failed" if error else "stopped",
                    connected=False,
                    error=error,
                )
            return self._status(instrument, monitor)

    def require_fresh_status(self, instrument: str) -> CameraMonitorStatus:
        """Return an existing monitor only when its latest frame is current."""
        status = self.status(instrument)
        if status.state != "running" or not status.connected:
            raise CameraMonitorNotRunning(
                "Start the camera monitor before previewing or saving alignment."
            )
        if status.frame_id is None or status.frame_age_seconds is None:
            raise CameraMonitorFrameExpired(
                "Camera monitor has no current frame for alignment."
            )
        if status.frame_age_seconds > self._maximum_frame_age_s:
            raise CameraMonitorFrameExpired(
                f"Camera alignment frame is stale "
                f"({status.frame_age_seconds:.2f}s old)."
            )
        return status

    def frame(
        self,
        instrument: str,
        requested_frame_id: int | None = None,
    ) -> tuple[bytes, int, float]:
        with self._lock:
            monitor = self._require_monitor_locked(instrument)
            camera = monitor.camera
            if requested_frame_id is not None:
                cached = monitor.frame_cache.get(requested_frame_id)
                if cached is not None:
                    age = max(0.0, self._clock() - cached[1])
                    if age > self._maximum_frame_age_s:
                        raise CameraMonitorFrameExpired(
                            f"Requested frame {requested_frame_id} is stale "
                            f"({age:.2f}s old)."
                        )
                    return cached[0], requested_frame_id, cached[1]
            try:
                frame = camera.latest_frame()
            except CameraCaptureError as exc:
                raise CameraMonitorError(str(exc)) from exc
            age = max(0.0, self._clock() - frame.received_at)
            if age > self._maximum_frame_age_s:
                raise CameraMonitorError(
                    f"Latest frame is stale ({age:.2f}s old)."
                )
            if requested_frame_id is not None and frame.frame_id != requested_frame_id:
                raise CameraMonitorFrameExpired(
                    f"Requested frame {requested_frame_id} is no longer available; "
                    f"latest frame is {frame.frame_id}."
                )
            encoded = self._cache_frame_locked(monitor, frame)
            return encoded, frame.frame_id, frame.received_at

    def controls(self, instrument: str) -> CameraControlsResponse:
        with self._lock:
            monitor = self._require_monitor_locked(instrument)
            return _controls_response(instrument, monitor.camera)

    def analysis_image_path(self, instrument: str | None = None) -> Path:
        """Resolve the latest scored preview from trusted run metadata only."""
        try:
            context = self._context_provider()
        except Exception as exc:
            raise CameraMonitorError(
                f"Camera analysis metadata is unavailable: {type(exc).__name__}: {exc}"
            ) from exc
        analysis = context.get("analysis") or {}
        source_instrument = analysis.get("_source_instrument")
        if (
            instrument is not None
            and isinstance(source_instrument, str)
            and source_instrument != instrument
        ):
            raise CameraMonitorError(
                "Latest scored image belongs to a different camera instrument."
            )
        selected = analysis.get("annotated_preview_path") or analysis.get("image_path")
        if not isinstance(selected, str):
            raise CameraMonitorError("No scored camera image is available.")
        from cubos.protocol_engine.commands.camera import default_images_dir

        root = default_images_dir().expanduser().resolve()
        try:
            path = Path(selected).expanduser().resolve(strict=True)
        except OSError as exc:
            raise CameraMonitorError(f"Scored camera image is unavailable: {exc}") from exc
        if not path.is_relative_to(root):
            raise CameraMonitorError(
                "Scored camera image is outside the configured image directory."
            )
        return path

    def set_controls(
        self,
        instrument: str,
        controls: dict[str, float | bool],
    ) -> CameraControlsResponse:
        if not controls:
            raise CameraMonitorError("Provide at least one camera control.")
        with self._lock:
            monitor = self._require_monitor_locked(instrument)
            camera = monitor.camera
            try:
                camera.set_controls(controls)
            except CameraCaptureError as exc:
                raise CameraMonitorError(str(exc)) from exc
            return _controls_response(instrument, camera)

    def _require_monitor_locked(self, instrument: str) -> _Monitor:
        monitor = self._active_monitor_locked(instrument)
        if monitor is None or not monitor.camera.health_check():
            raise CameraMonitorNotRunning(
                f"Camera monitor {instrument!r} is not running."
            )
        return monitor

    def _status(
        self,
        instrument: str,
        monitor: _Monitor,
        *,
        lease_id: str | None = None,
        lease_expires_at: float | None = None,
    ) -> CameraMonitorStatus:
        camera = monitor.camera
        warnings: list[str] = []
        try:
            frame = camera.latest_frame()
        except CameraCaptureError as exc:
            frame = None
            warnings.append(str(exc))
        try:
            capture_profile = camera.control_fingerprint()
        except CameraCaptureError as exc:
            capture_profile = None
            warnings.append(f"Camera control readback unavailable: {exc}")

        requested = CameraResolution(
            width=camera.resolution[0],
            height=camera.resolution[1],
        )
        actual = (
            CameraResolution(width=frame.width, height=frame.height)
            if frame is not None
            else None
        )
        if actual is not None and actual != requested:
            warnings.append(
                "Camera returned "
                f"{actual.width}x{actual.height}; configured resolution is "
                f"{requested.width}x{requested.height}."
            )
        requested_pixel_format = camera.pixel_format
        actual_pixel_format = (
            str(capture_profile.get("actual_pixel_format"))
            if capture_profile is not None
            and capture_profile.get("actual_pixel_format") is not None
            else None
        )
        if (
            actual_pixel_format is not None
            and actual_pixel_format != requested_pixel_format
        ):
            warnings.append(
                f"Camera returned {actual_pixel_format}; configured pixel format is "
                f"{requested_pixel_format}."
            )

        frame_age = (
            max(0.0, self._clock() - frame.received_at)
            if frame is not None
            else None
        )
        if frame_age is not None and frame_age > self._maximum_frame_age_s:
            warnings.append(f"Latest frame is stale ({frame_age:.2f}s old).")

        try:
            context = self._context_provider()
        except Exception as exc:
            context = {}
            warnings.append(
                "Run context is temporarily unavailable: "
                f"{type(exc).__name__}: {exc}"
            )
        analysis = context.get("analysis") or {}
        public_analysis = {
            key: value
            for key, value in analysis.items()
            if not str(key).startswith("_")
        }
        analysis_frame_metadata = analysis.get("frame_metadata") or {}
        analysis_source_frame_id = analysis_frame_metadata.get("frame_id")
        analysis_source_received_at = analysis_frame_metadata.get("received_at")
        analysis_source_run_id = analysis.get("_source_run_id")
        well_identity = analysis.get("well_identity") or {}
        analysis_source_well = (
            well_identity.get("expected_well") or analysis.get("_source_well")
        )
        analysis_is_current = bool(
            frame is not None
            and analysis_source_run_id == context.get("run_id")
            and analysis_source_frame_id == frame.frame_id
        )
        current_fingerprint = (
            str(capture_profile.get("fingerprint"))
            if capture_profile is not None
            else None
        )
        prior_capture = (
            (analysis.get("processing_profile") or {})
            .get("configuration", {})
            .get("acquisition", {})
        )
        if isinstance(prior_capture, dict):
            prior_capture = (
                prior_capture.get("actual_capture_profile")
                or prior_capture.get("requested_capture_profile")
                or {}
            )
        if (
            isinstance(prior_capture, dict)
            and prior_capture.get("fingerprint")
            and current_fingerprint
            and prior_capture["fingerprint"] != current_fingerprint
        ):
            warnings.append(
                "Camera controls or capture format changed since the latest color "
                "measurement; capture a new reference before comparing colors."
            )

        image_url = None
        if frame is not None:
            image_url = (
                "/api/v1/instruments/camera/monitor/frame?instrument="
                f"{quote(instrument, safe='')}&frame_id={frame.frame_id}"
            )
            try:
                self._cache_frame_locked(monitor, frame)
            except CameraMonitorError as exc:
                warnings.append(str(exc))
        analysis_image_url = None
        if analysis:
            try:
                self.analysis_image_path(instrument)
            except CameraMonitorError:
                pass
            else:
                analysis_image_url = (
                    "/api/v1/instruments/camera/monitor/analysis-frame?instrument="
                    f"{quote(instrument, safe='')}"
                )
        return CameraMonitorStatus(
            instrument=instrument,
            state="running" if camera.health_check() else "failed",
            connected=camera.health_check(),
            lease_id=lease_id,
            lease_expires_at=lease_expires_at,
            subscriber_count=len(monitor.leases),
            camera_id=camera.camera_id,
            requested_resolution=requested,
            actual_resolution=actual,
            requested_pixel_format=requested_pixel_format,
            actual_pixel_format=actual_pixel_format,
            frame_id=frame.frame_id if frame is not None else None,
            received_at=frame.received_at if frame is not None else None,
            frame_age_seconds=frame_age,
            image_url=image_url,
            control_fingerprint=current_fingerprint,
            capture_profile=capture_profile,
            run_id=context.get("run_id"),
            campaign_id=context.get("campaign_id"),
            trial_number=context.get("trial_number"),
            step_index=context.get("step_index"),
            step_command=context.get("step_command"),
            step_substep=context.get("step_substep"),
            expected_well=context.get("expected_well"),
            expected_center=context.get("expected_center"),
            expected_center_source=context.get("expected_center_source"),
            roi=analysis.get("roi") if analysis_is_current else None,
            quality=analysis.get("quality") if analysis_is_current else None,
            processing_profile=(
                analysis.get("processing_profile") if analysis_is_current else None
            ),
            analysis_source_image_path=analysis.get("image_path"),
            analysis_image_url=analysis_image_url,
            analysis_source_run_id=analysis_source_run_id,
            analysis_source_well=analysis_source_well,
            analysis_source_frame_id=(
                analysis_source_frame_id
                if isinstance(analysis_source_frame_id, int)
                else None
            ),
            analysis_source_received_at=(
                float(analysis_source_received_at)
                if isinstance(analysis_source_received_at, (int, float))
                else None
            ),
            analysis_is_current_frame=analysis_is_current,
            latest_analysis=public_analysis or None,
            warnings=warnings,
            error=self._errors.get(instrument),
        )

    def _cache_frame_locked(self, monitor: _Monitor, frame: Any) -> bytes:
        cached = monitor.frame_cache.get(frame.frame_id)
        if cached is not None:
            return cached[0]
        try:
            encoded = monitor.camera.encode_jpeg(frame)
        except CameraCaptureError as exc:
            raise CameraMonitorError(str(exc)) from exc
        monitor.frame_cache[frame.frame_id] = (encoded, frame.received_at)
        while len(monitor.frame_cache) > self._encoded_frame_cache_size:
            monitor.frame_cache.popitem(last=False)
        return encoded

    def _add_lease_locked(
        self,
        instrument: str,
        monitor: _Monitor,
    ) -> tuple[str, float]:
        lease_id = uuid.uuid4().hex
        expires_at = self._clock() + self._lease_ttl_s
        monitor.leases[lease_id] = expires_at
        self._schedule_expiry_locked(instrument, monitor)
        return lease_id, expires_at

    def _active_monitor_locked(self, instrument: str) -> _Monitor | None:
        monitor = self._monitors.get(instrument)
        if monitor is None:
            return None
        now = self._clock()
        monitor.leases = {
            lease_id: expires_at
            for lease_id, expires_at in monitor.leases.items()
            if expires_at > now
        }
        if monitor.leases:
            return monitor
        self._remove_monitor_locked(instrument, monitor)
        monitor.camera.disconnect()
        return None

    def _remove_monitor_locked(
        self,
        instrument: str,
        monitor: _Monitor,
    ) -> None:
        if self._monitors.get(instrument) is monitor:
            self._monitors.pop(instrument, None)
        if monitor.expiry_timer is not None:
            monitor.expiry_timer.cancel()
            monitor.expiry_timer = None

    def _schedule_expiry_locked(
        self,
        instrument: str,
        monitor: _Monitor,
    ) -> None:
        if monitor.expiry_timer is not None:
            monitor.expiry_timer.cancel()
        next_expiry = min(monitor.leases.values())
        timer = threading.Timer(
            max(0.01, next_expiry - self._clock()),
            self._expire_monitor,
            args=(instrument,),
        )
        timer.daemon = True
        monitor.expiry_timer = timer
        timer.start()

    def _expire_monitor(self, instrument: str) -> None:
        with self._lock:
            monitor = self._active_monitor_locked(instrument)
            if monitor is not None:
                self._schedule_expiry_locked(instrument, monitor)


def _controls_response(
    instrument: str,
    camera: OpenCVCamera,
) -> CameraControlsResponse:
    try:
        statuses = camera.control_status()
        profile = camera.control_fingerprint()
    except CameraCaptureError as exc:
        raise CameraMonitorError(str(exc)) from exc
    return CameraControlsResponse(
        instrument=instrument,
        controls={
            name: CameraControlValue.model_validate(value)
            for name, value in statuses.items()
        },
        control_fingerprint=str(profile["fingerprint"]),
    )


def _active_execution_context() -> dict[str, Any]:
    from cubos_api.services.run_manager import get_run_manager

    manager = get_run_manager()
    run_id = manager.active_run_id
    campaign_id = manager.campaign_owner
    result: dict[str, Any] = {
        "run_id": run_id,
        "campaign_id": campaign_id,
    }
    if run_id is not None:
        record = manager.get(run_id)
        if record is not None:
            result["campaign_id"] = record.metadata.get(
                "active_learning_campaign_id", campaign_id
            )
            result["trial_number"] = record.metadata.get("trial")
            step = _active_step(manager.store.events(run_id))
            if step is not None:
                result.update(step)
                result.update(_expected_target(
                    manager.store.run_dir(run_id) / "protocol.yaml",
                    step["step_index"],
                    step.get("step_substep"),
                ))

    if campaign_id is not None:
        try:
            from cubos_api.services.campaign_manager import get_campaign_manager

            campaign = get_campaign_manager().get(campaign_id)
        except (KeyError, OSError, ValueError):
            campaign = None
        if campaign is not None:
            for trial in reversed(campaign.trials):
                if trial.measurement:
                    analysis = dict(trial.measurement)
                    analysis["_source_run_id"] = trial.run_id
                    source = _analysis_protocol_source(
                        manager.store.run_dir(trial.run_id) / "protocol.yaml",
                        campaign.spec.objective.path,
                    )
                    analysis.update(source)
                    result["analysis"] = analysis
                    break
    return result


def _active_step(events: list[Any]) -> dict[str, Any] | None:
    open_steps: dict[tuple[int, str | None], dict[str, Any]] = {}
    for event in events:
        if event.kind != "step" or not isinstance(event.data, dict):
            continue
        data = event.data
        index = data.get("index")
        command = data.get("command")
        substep = data.get("substep")
        outcome = data.get("outcome")
        if not isinstance(index, int) or not isinstance(command, str):
            continue
        key = (index, substep if isinstance(substep, str) else None)
        if outcome == "started":
            open_steps[key] = {
                "step_index": index,
                "step_command": command,
                "step_substep": key[1],
            }
        elif outcome in {"completed", "failed", "skipped"}:
            open_steps.pop(key, None)
    return next(reversed(open_steps.values()), None) if open_steps else None


def _expected_target(
    protocol_path: Path,
    step_index: int,
    substep: str | None,
) -> dict[str, Any]:
    try:
        document = yaml.safe_load(protocol_path.read_text(encoding="utf-8"))
        step = document["protocol"][step_index]
        body = step[next(iter(step))]
    except (OSError, ValueError, TypeError, KeyError, IndexError, yaml.YAMLError):
        return {}
    if not isinstance(body, dict):
        return {}
    if substep and substep.endswith("aspirate"):
        keys = ("source", "position", "destination")
    elif substep and substep.endswith("dispense"):
        keys = ("destination", "position", "source")
    else:
        keys = ("position", "destination", "source")
    for key in keys:
        value = body.get(key)
        if isinstance(value, str):
            result: dict[str, Any] = {"expected_well": value}
            center = body.get("expected_center")
            if (
                isinstance(center, (list, tuple))
                and len(center) == 2
                and all(isinstance(item, (int, float)) for item in center)
            ):
                result["expected_center"] = {
                    "x": float(center[0]),
                    "y": float(center[1]),
                }
            source = body.get("expected_center_source")
            if isinstance(source, str):
                result["expected_center_source"] = source
            return result
    return {}


def _expected_well(
    protocol_path: Path,
    step_index: int,
    substep: str | None,
) -> str | None:
    return _expected_target(protocol_path, step_index, substep).get("expected_well")


def _analysis_protocol_source(
    protocol_path: Path,
    objective_path: str,
) -> dict[str, str]:
    first, _, _ = objective_path.partition(".")
    if not first.isdigit():
        return {}
    step_index = int(first)
    try:
        document = yaml.safe_load(protocol_path.read_text(encoding="utf-8"))
        step = document["protocol"][step_index]
        body = step[next(iter(step))]
    except (OSError, ValueError, TypeError, KeyError, IndexError, yaml.YAMLError):
        return {}
    if not isinstance(body, dict):
        return {}
    result: dict[str, str] = {}
    instrument = body.get("instrument")
    position = body.get("position")
    if isinstance(instrument, str):
        result["_source_instrument"] = instrument
    if isinstance(position, str):
        result["_source_well"] = position
    return result


_service = CameraMonitorService()


def get_camera_monitor_service() -> CameraMonitorService:
    return _service


__all__ = [
    "CameraMonitorError",
    "CameraMonitorFrameExpired",
    "CameraMonitorNotRunning",
    "CameraMonitorService",
    "CameraMonitorUnsupported",
    "get_camera_monitor_service",
]
