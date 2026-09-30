"""Persistent CubOS gantry session for UI/API wrappers."""

from __future__ import annotations

import copy
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from cubos.data import DataStore, create_campaign_for_protocol_run
from cubos.gantry.gantry import Gantry
from cubos.gantry.grbl_settings import normalize_expected_grbl_settings
from cubos.gantry.limit_recovery import (
    LimitRecoveryResult,
    looks_like_limit_alarm,
    recover_from_limit_alarm,
)
from cubos.gantry.yaml_schema import GantryYamlSchema
from cubos.protocol_engine.setup import setup_protocol


@dataclass(frozen=True)
class GantryPositionSnapshot:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    work_x: Optional[float] = None
    work_y: Optional[float] = None
    work_z: Optional[float] = None
    status: str = "Unknown"
    connected: bool = False
    calibration_warning: Optional[str] = None
    move_error: Optional[str] = None


@dataclass(frozen=True)
class CalibrationCenterResult:
    xy_bounds: dict[str, float]
    position: dict[str, float]


@dataclass(frozen=True)
class FinalizeOriginResult:
    measured_volume: dict[str, float]
    z_calibration: dict[str, Any]
    max_travel: dict[str, float]
    position: dict[str, float]
    homing_pull_off_mm: Optional[float] = None


@dataclass(frozen=True)
class ProtocolRunResult:
    status: str
    steps_executed: int
    campaign_id: int
    results: list[Any]


class GantrySessionError(Exception):
    """Base class for persistent gantry-session failures."""


class GantryNotConnectedError(GantrySessionError):
    """Raised when a session operation requires a connected gantry."""


class CalibrationBlockedError(GantrySessionError):
    """Raised when calibration drift blocks protocol execution."""


class MovementOutOfBoundsError(GantrySessionError):
    """Raised when manual movement would leave the configured working volume."""

    def __init__(self, message: str, *, requires_reconnect: bool = False) -> None:
        super().__init__(message)
        self.requires_reconnect = requires_reconnect
        """Whether the operator should reconnect before retrying movement."""


class GantryAlarmError(GantrySessionError):
    """Raised when a movement surfaces a GRBL alarm or active limit."""


class GantrySessionHealthCheckError(GantrySessionError):
    """Raised when a connected session fails a pre-run health check."""


class InterruptFeedHoldTimeoutError(GantrySessionError):
    """Raised when a feed-hold interrupt was sent but not acknowledged in time."""


class GantrySession:
    """Own a persistent connected :class:`Gantry` and serialized operations."""

    def __init__(
        self,
        *,
        gantry_factory: Callable[..., Gantry] = Gantry,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._gantry_factory = gantry_factory
        self._sleep = sleep
        self._lock = threading.Lock()
        self._gantry: Gantry | None = None
        self._last_position: GantryPositionSnapshot | None = None
        self._calibration_warning: str | None = None
        self._connected_gantry_config: dict[str, Any] | None = None
        self._connected_gantry_filename: str | None = None
        self._calibration_restore_soft_limits = False
        self._calibration_restore_hard_limits = False
        self._calibration_jog_bypass_working_volume = False
        self._move_error: str | None = None
        self._jog_cancel_generation = 0
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @property
    def connected(self) -> bool:
        return self._gantry is not None

    @property
    def calibration_warning(self) -> str | None:
        return self._calibration_warning

    @property
    def calibration_active(self) -> bool:
        """True while calibration bypass/restore state is active.

        This is the public read-only contract for UI/runtime consumers that
        need to warn operators before manual motion during calibration.
        """
        return bool(
            self._calibration_jog_bypass_working_volume
            or self._calibration_restore_soft_limits
            or self._calibration_restore_hard_limits
        )

    @property
    def connected_gantry_filename(self) -> str | None:
        return self._connected_gantry_filename

    @property
    def connected_gantry_config(self) -> dict[str, Any] | None:
        """Deep copy of the YAML config the connected gantry was built from.

        ``None`` while disconnected. Consumers (e.g. the API's manual
        instrument endpoints) read the ``instruments:`` section from here so
        they act on exactly the configuration the operator connected.
        """
        return copy.deepcopy(self._connected_gantry_config)

    @property
    def operation_lock(self) -> threading.Lock:
        """Return the serial-operation lock for targeted tests/observability."""
        return self._lock

    def connect(
        self,
        config_path: str | Path | None = None,
        *,
        filename: str | None = None,
    ) -> GantryPositionSnapshot:
        """Connect a gantry and publish it only after the full handshake works."""
        with self._lock:
            if self._gantry is not None:
                self._restore_calibration_soft_limits_if_needed_locked()
                self._gantry.disconnect()
                self._clear_connected_state_locked()
            config = (
                self._load_config_from_yaml(config_path)
                if config_path is not None
                else {}
            )
            staged = self._gantry_factory(config=self._runtime_connect_config(config))
            port = str(config.get("serial_port") or "") or None
            try:
                staged.connect(port=port)
                calibration_warning = self._calibration_mismatch_warning(staged, config)
                for _ in range(10):
                    info = staged.get_position_info()
                    if info.get("work_pos") is not None:
                        break
                    self._sleep(0.1)
            except Exception:
                try:
                    staged.disconnect()
                except Exception:
                    pass
                raise

            self._gantry = staged
            self._calibration_warning = calibration_warning
            self._calibration_restore_soft_limits = False
            self._calibration_restore_hard_limits = False
            self._calibration_jog_bypass_working_volume = False
            self._connected_gantry_config = copy.deepcopy(config)
            self._connected_gantry_filename = filename

        return self.position()

    def disconnect(self) -> GantryPositionSnapshot:
        if self._gantry is None:
            return GantryPositionSnapshot(connected=False, status="Disconnected")

        restore_error: Exception | None = None
        disconnect_error: Exception | None = None
        with self._lock:
            gantry = self._gantry
            try:
                self._restore_calibration_soft_limits_if_needed_locked()
            except Exception as exc:
                restore_error = exc
            try:
                if gantry is not None:
                    gantry.disconnect()
            except Exception as exc:
                disconnect_error = exc
            finally:
                self._clear_connected_state_locked()

        if restore_error is not None:
            if disconnect_error is not None:
                raise GantrySessionError(
                    "Soft-limit restore and disconnect both failed: "
                    f"restore error: {restore_error}; "
                    f"disconnect error: {disconnect_error}. Verify controller "
                    "state before moving again."
                )
            raise GantrySessionError(
                "Soft-limit restore failed before disconnect: "
                f"{restore_error}. Gantry was disconnected; verify GRBL soft "
                "limits and travel settings before moving again."
            )
        if disconnect_error is not None:
            raise GantrySessionError(f"Disconnect failed: {disconnect_error}")
        return GantryPositionSnapshot(connected=False, status="Disconnected")

    def refresh_connected_config(
        self,
        filename: str,
        config: dict[str, Any],
    ) -> None:
        with self._lock:
            if self._gantry is None or self._connected_gantry_filename != filename:
                return
            self._connected_gantry_config = copy.deepcopy(config)
            self._gantry.config = self._runtime_connect_config(config)

    def position(self) -> GantryPositionSnapshot:
        if self._gantry is None:
            return GantryPositionSnapshot(connected=False, status="Not connected")
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            return self._position_from_cache_with_status(self._extract_status())
        try:
            return self._read_position_locked()
        except Exception as exc:
            if self._looks_like_alarm_error(exc):
                return self._position_from_cache_with_status(
                    self._alarm_status_from_error(exc)
                )
            if (
                self._last_position is not None
                and not self._calibration_jog_bypass_working_volume
            ):
                return self._last_position
            return self._position_from_cache_with_status("Query failed")
        finally:
            self._lock.release()

    def home(self) -> GantryPositionSnapshot:
        with self._locked_gantry() as gantry:
            gantry.home()
        return self.position()

    def unlock(self) -> GantryPositionSnapshot:
        with self._locked_gantry() as gantry:
            gantry.unlock()
        return self.position()

    def reset_and_unlock(self) -> GantryPositionSnapshot:
        with self._locked_gantry() as gantry:
            gantry.reset_and_unlock()
        return self.position()

    def resume(self) -> GantryPositionSnapshot:
        """Resume a feed-held controller after an explicit operator action."""
        with self._locked_gantry() as gantry:
            gantry.resume()
        return self.position()

    def feed_hold(self) -> GantryPositionSnapshot:
        with self._locked_gantry() as gantry:
            gantry.stop()
        return self.position()

    def feed_hold_interrupt(self) -> None:
        """Send feed hold without waiting for the operation lock."""
        gantry = self._require_connected()
        try:
            gantry.feed_hold_realtime()
        except Exception as exc:
            if self._looks_like_feed_hold_timeout(exc):
                raise InterruptFeedHoldTimeoutError(
                    "Feed hold was sent, but the controller did not acknowledge "
                    "before the read timeout."
                ) from exc
            raise

    def jog_cancel(self) -> GantryPositionSnapshot:
        self._jog_cancel_generation += 1
        with self._locked_gantry() as gantry:
            gantry.jog_cancel()
        return self.position()

    def jog_cancel_interrupt(self) -> None:
        """Cancel an active jog without waiting for the operation lock.

        Also bumps the cancel generation so jog requests already queued on
        the operation lock are dropped instead of restarting motion after
        the cancel (GRBL 0x85 only flushes what is already in its planner).
        """
        self._jog_cancel_generation += 1
        self._require_connected().jog_cancel()

    def read_grbl_settings(self) -> dict[str, str]:
        with self._locked_gantry() as gantry:
            return {str(key): str(value) for key, value in gantry.read_grbl_settings().items()}

    def set_grbl_setting(self, setting: str, value: str | float | int) -> dict[str, str]:
        code = self._normalize_grbl_setting_code(setting)
        parsed = self._parse_grbl_setting_value(value)
        with self._locked_gantry() as gantry:
            gantry.set_grbl_setting(code, parsed)
            return {str(key): str(value) for key, value in gantry.read_grbl_settings().items()}

    def move_to(self, *, x: float, y: float, z: float) -> None:
        self._require_connected()
        with self._lock:
            self._validate_manual_move_target_locked(x=x, y=y, z=z)
        thread = threading.Thread(
            target=self._move_worker,
            args=(float(x), float(y), float(z)),
            daemon=True,
        )
        thread.start()

    def move_to_blocking(self, *, x: float, y: float, z: float) -> GantryPositionSnapshot:
        with self._locked_gantry() as gantry:
            self._validate_manual_move_target_locked(x=x, y=y, z=z)
            gantry.move_to(x=float(x), y=float(y), z=float(z))
        return self.position()

    def jog(
        self,
        *,
        x: float = 0.0,
        y: float = 0.0,
        z: float = 0.0,
        feed_rate: float = 2000,
    ) -> None:
        self._require_connected()
        if x == 0 and y == 0 and z == 0:
            return
        generation = self._jog_cancel_generation
        with self._lock:
            if generation != self._jog_cancel_generation:
                # A jog cancel arrived while this request waited on the
                # operation lock. Sending the jog now would restart motion
                # the operator just stopped, so drop it.
                return
            self._validate_jog_target_locked(x=x, y=y, z=z)
            try:
                self._require_connected().jog(x=x, y=y, z=z, feed_rate=feed_rate)
            except Exception as exc:
                if self._looks_like_alarm_error(exc):
                    raise GantryAlarmError(
                        "Gantry entered an alarm state during jog. "
                        "Run limit recovery before continuing."
                    ) from exc
                raise

    def jog_blocking(
        self,
        *,
        x: float = 0.0,
        y: float = 0.0,
        z: float = 0.0,
        timeout_s: float = 10.0,
        feed_rate: float = 2000,
    ) -> GantryPositionSnapshot:
        self._require_connected()
        if x == 0 and y == 0 and z == 0:
            return self.position()
        with self._lock:
            self._validate_jog_target_locked(x=x, y=y, z=z)
            try:
                self._require_connected().jog(x=x, y=y, z=z, feed_rate=feed_rate)
                self._wait_until_idle_locked(timeout_s=timeout_s)
            except GantryAlarmError:
                raise
            except Exception as exc:
                if self._looks_like_alarm_error(exc):
                    raise GantryAlarmError(
                        "Gantry entered an alarm state during blocking jog. "
                        "Run limit recovery before continuing."
                    ) from exc
                raise
        return self.position()

    def set_work_coordinates(
        self,
        *,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
    ) -> GantryPositionSnapshot:
        with self._locked_gantry() as gantry:
            gantry.set_work_coordinates(x=x, y=y, z=z)
        return self.position()

    def configure_soft_limits(
        self,
        *,
        max_travel_x: float,
        max_travel_y: float,
        max_travel_z: float,
        status_report: float | None = None,
        homing_pull_off: float | None = None,
        hard_limits: bool | None = None,
        tolerance_mm: float = 0.25,
    ) -> None:
        with self._locked_gantry() as gantry:
            configured_hard_limits = (
                hard_limits
                if hard_limits is not None
                else self._connected_grbl_setting_locked("hard_limits")
            )
            gantry.configure_soft_limits_from_spans(
                max_travel_x=max_travel_x,
                max_travel_y=max_travel_y,
                max_travel_z=max_travel_z,
                status_report=status_report,
                homing_pull_off=homing_pull_off,
                hard_limits=configured_hard_limits,
                tolerance_mm=tolerance_mm,
            )
            self._calibration_restore_soft_limits = False
            self._restore_calibration_hard_limits_if_needed_locked(
                keep_if_configured=True
            )
            self._calibration_jog_bypass_working_volume = False
            if self._connected_gantry_config is not None:
                grbl_settings = dict(
                    self._connected_gantry_config.get("grbl_settings") or {}
                )
                grbl_settings.update({
                    "soft_limits": True,
                    "homing_enable": True,
                    "max_travel_x": max_travel_x,
                    "max_travel_y": max_travel_y,
                    "max_travel_z": max_travel_z,
                })
                if configured_hard_limits is not None:
                    grbl_settings["hard_limits"] = bool(configured_hard_limits)
                if status_report is not None:
                    grbl_settings["status_report"] = status_report
                if homing_pull_off is not None:
                    grbl_settings["homing_pull_off"] = homing_pull_off
                self._connected_gantry_config["grbl_settings"] = grbl_settings
                self._calibration_warning = self._calibration_mismatch_warning(
                    gantry,
                    self._connected_gantry_config,
                )

    def prepare_calibration_origin(self) -> GantryPositionSnapshot:
        with self._locked_gantry() as gantry:
            self._apply_calibration_grbl_baseline_locked()
            # Hard limits ($21) are the only motion backstop while soft
            # limits are disabled below, and hobby controllers ship with
            # them off — without this, a jog past a travel end grinds and
            # silently skips steps, corrupting the calibration being taken.
            # Enforced for the calibration window only; finalize/abort
            # restore the prior value (finalize keeps $21=1 when the gantry
            # YAML configures hard_limits).
            if gantry.hard_limits_enabled() is False:
                gantry.set_hard_limits_enabled(True)
                self._calibration_restore_hard_limits = True
            gantry.home()
            gantry.enforce_work_position_reporting()
            gantry.activate_work_coordinate_system("G54")
            gantry.clear_g92_offsets()
            enabled = gantry.soft_limits_enabled()
            if enabled is True:
                gantry.set_soft_limits_enabled(False)
                self._calibration_restore_soft_limits = True
            self._calibration_jog_bypass_working_volume = True
        return self.position()

    def calibration_home_and_center(self) -> CalibrationCenterResult:
        with self._locked_gantry() as gantry:
            gantry.home()
            bounds = {axis: float(value) for axis, value in dict(gantry.get_coordinates()).items()}
            center_x = round(float(bounds["x"]) / 2.0, 3)
            center_y = round(float(bounds["y"]) / 2.0, 3)
            gantry.move_to(center_x, center_y, float(bounds["z"]))
            position = {axis: float(value) for axis, value in dict(gantry.get_coordinates()).items()}
        return CalibrationCenterResult(xy_bounds=bounds, position=position)

    def restore_calibration_soft_limits(self) -> GantryPositionSnapshot:
        with self._locked_gantry():
            self._restore_calibration_soft_limits_if_needed_locked()
            self._restore_calibration_hard_limits_if_needed_locked()
            self._calibration_jog_bypass_working_volume = False
        return self.position()

    def finalize_calibration_origin(
        self,
        *,
        home_z: float,
        block_touch_z: float,
        block_height: float,
        factory_z_travel: float,
        tolerance_mm: float = 0.25,
    ) -> FinalizeOriginResult:
        with self._locked_gantry() as gantry:
            try:
                result = gantry.finalize_deck_origin_calibration(
                    home_z=home_z,
                    block_touch_z=block_touch_z,
                    block_height=block_height,
                    total_z_range=factory_z_travel,
                    status_report=0,
                    homing_pull_off=self._connected_grbl_setting_locked(
                        "homing_pull_off"
                    ),
                    hard_limits=self._connected_grbl_setting_locked("hard_limits"),
                    tolerance_mm=tolerance_mm,
                )
                max_travel = {
                    axis: float(value)
                    for axis, value in dict(result["max_travel"]).items()
                }
                measured_volume = {
                    axis: float(value)
                    for axis, value in dict(result["measured_volume"]).items()
                }
                position = {
                    axis: float(value)
                    for axis, value in dict(result["position"]).items()
                }
                homing_pull_off_mm = result.get("homing_pull_off_mm")
                if homing_pull_off_mm is not None:
                    homing_pull_off_mm = float(homing_pull_off_mm)
                self._calibration_restore_soft_limits = False
                self._restore_calibration_hard_limits_if_needed_locked(
                    keep_if_configured=True
                )
                self._calibration_jog_bypass_working_volume = False
                if self._connected_gantry_config is not None:
                    grbl_settings = dict(
                        self._connected_gantry_config.get("grbl_settings") or {}
                    )
                    grbl_settings.update({
                        "soft_limits": True,
                        "homing_enable": True,
                        "max_travel_x": max_travel["x"],
                        "max_travel_y": max_travel["y"],
                        "max_travel_z": max_travel["z"],
                        "status_report": 0,
                    })
                    if homing_pull_off_mm is not None:
                        grbl_settings["homing_pull_off"] = homing_pull_off_mm
                    self._connected_gantry_config["grbl_settings"] = grbl_settings
                    self._calibration_warning = self._calibration_mismatch_warning(
                        gantry,
                        self._connected_gantry_config,
                    )
                self._last_position = GantryPositionSnapshot(
                    x=position["x"],
                    y=position["y"],
                    z=position["z"],
                    work_x=position["x"],
                    work_y=position["y"],
                    work_z=position["z"],
                    status="Idle",
                    connected=True,
                    calibration_warning=self._calibration_warning,
                )
            except Exception:
                self._attempt_soft_limit_restore_after_calibration_failure_locked(
                    gantry
                )
                self._calibration_jog_bypass_working_volume = False
                raise

        return FinalizeOriginResult(
            measured_volume=measured_volume,
            z_calibration=dict(result["z_calibration"]),
            max_travel=max_travel,
            position=position,
            homing_pull_off_mm=homing_pull_off_mm,
        )

    def recover_calibration_limit(
        self,
        *,
        x: float = 0.0,
        y: float = 0.0,
        z: float = 0.0,
        pull_off_mm: float = 5.0,
        feed_rate: float = 2500.0,
    ) -> tuple[LimitRecoveryResult, list[str]]:
        if x == 0 and y == 0 and z == 0:
            raise ValueError("Limit recovery requires the failed jog delta.")
        messages: list[str] = []
        with self._locked_gantry() as gantry:
            result = recover_from_limit_alarm(
                gantry,
                {"x": x, "y": y, "z": z},
                pull_off_mm=pull_off_mm,
                feed_rate=feed_rate,
                output=messages.append,
            )
        return result, messages

    def run_protocol(
        self,
        *,
        gantry_path: str | Path,
        deck_path: str | Path,
        protocol_path: str | Path,
        gantry_file: str,
        deck_file: str,
        protocol_file: str,
        db_path: str | Path | None = None,
        fluid_state_id: int | None = None,
        step_observer: Any | None = None,
    ) -> ProtocolRunResult:
        context = None
        data_store = None
        with self._lock:
            gantry = self._require_connected()
            # A GRBL-settings mismatch (self._calibration_warning) is advisory,
            # not blocking: commissioning machines legitimately differ from the
            # selected gantry YAML, and the operator owns that decision. The
            # warning is still computed and surfaced; it no longer blocks runs.
            if not gantry.is_healthy():
                raise GantrySessionHealthCheckError("Gantry is not connected")

            data_store = DataStore(db_path=db_path)
            try:
                campaign_id = create_campaign_for_protocol_run(
                    data_store,
                    gantry_path=gantry_path,
                    deck_path=deck_path,
                    gantry_file=gantry_file,
                    deck_file=deck_file,
                    protocol_file=protocol_file,
                    fluid_state_id=fluid_state_id,
                )
                protocol, context = setup_protocol(
                    gantry_path,
                    deck_path,
                    protocol_path,
                    gantry=gantry,
                    data_store=data_store,
                    campaign_id=campaign_id,
                    fluid_state_id=fluid_state_id,
                    step_observer=step_observer,
                )
                gantry.prepare_for_protocol_run()
                context.gantry.connect_instruments()
                if not gantry.is_healthy():
                    raise GantrySessionHealthCheckError(
                        "Gantry health check failed before protocol execution; "
                        "aborting."
                    )
                results = protocol.execute(context)
            finally:
                if context is not None:
                    context.gantry.disconnect_instruments()
                if data_store is not None:
                    data_store.close()
        return ProtocolRunResult(
            status="ok",
            steps_executed=len(results),
            campaign_id=campaign_id,
            results=results,
        )

    def _move_worker(self, x: float, y: float, z: float) -> None:
        self._move_error = None
        try:
            with self._lock:
                self._require_connected().move_to(x=x, y=y, z=z)
        except Exception as exc:
            self.logger.error("Background move to (%s, %s, %s) failed: %s", x, y, z, exc, exc_info=True)
            self._move_error = str(exc)

    def _load_config_from_yaml(self, path: str | Path) -> dict[str, Any]:
        with Path(path).open() as handle:
            raw = yaml.safe_load(handle)
        if raw is None:
            raw = {}
        schema = GantryYamlSchema.model_validate(raw)
        return schema.model_dump(mode="json", exclude_none=True)

    def _clear_connected_state_locked(self) -> None:
        self._gantry = None
        self._last_position = None
        self._calibration_warning = None
        self._connected_gantry_config = None
        self._connected_gantry_filename = None
        self._calibration_restore_soft_limits = False
        self._calibration_restore_hard_limits = False
        self._calibration_jog_bypass_working_volume = False
        self._move_error = None

    def _read_position_locked(self) -> GantryPositionSnapshot:
        gantry = self._require_connected()
        info = gantry.get_position_info()
        coords = info["coords"]
        wpos = info["work_pos"]
        self._last_position = GantryPositionSnapshot(
            x=float(coords["x"]),
            y=float(coords["y"]),
            z=float(coords["z"]),
            work_x=float(wpos["x"]) if wpos else None,
            work_y=float(wpos["y"]) if wpos else None,
            work_z=float(wpos["z"]) if wpos else None,
            status=str(info["status"]),
            connected=True,
            calibration_warning=self._calibration_warning,
            move_error=self._move_error,
        )
        return self._last_position

    def _position_from_cache_with_status(self, status: str) -> GantryPositionSnapshot:
        if self._last_position is not None:
            return GantryPositionSnapshot(
                x=self._last_position.x,
                y=self._last_position.y,
                z=self._last_position.z,
                work_x=self._last_position.work_x,
                work_y=self._last_position.work_y,
                work_z=self._last_position.work_z,
                status=status,
                connected=True,
                calibration_warning=self._calibration_warning,
                move_error=self._move_error,
            )
        return GantryPositionSnapshot(
            connected=True,
            status=status,
            calibration_warning=self._calibration_warning,
            move_error=self._move_error,
        )

    def _runtime_connect_config(self, config: dict[str, Any]) -> dict[str, Any]:
        runtime_config = copy.deepcopy(config)
        runtime_config.pop("grbl_settings", None)
        return runtime_config

    def _calibration_mismatch_warning(
        self,
        gantry: Gantry,
        config: dict[str, Any],
    ) -> str | None:
        expected = normalize_expected_grbl_settings(config.get("grbl_settings"))
        if not expected:
            return None

        try:
            live = gantry.read_grbl_settings()
        except Exception as exc:
            return (
                "Calibration status unknown: connected, but CubOS could not "
                "read controller GRBL settings after connect "
                f"({exc}). Run calibration again before trusting coordinates "
                "or running protocols."
            )

        mismatches = []
        for code, expected_value in expected.items():
            live_raw = live.get(code)
            if live_raw is None:
                mismatches.append(f"{code}: expected {expected_value:g}, got missing")
                continue
            try:
                live_value = float(live_raw)
            except (TypeError, ValueError):
                mismatches.append(f"{code}: expected {expected_value:g}, got {live_raw}")
                continue
            if abs(live_value - float(expected_value)) > 0.001:
                mismatches.append(
                    f"{code}: expected {expected_value:g}, got {live_value:g}"
                )

        if not mismatches:
            return None
        return (
            "Calibration needed: connected, but controller GRBL settings differ "
            "from the selected gantry YAML. Run calibration again before "
            "trusting coordinates or running protocols. "
            + "; ".join(mismatches)
        )

    def _connected_working_volume_locked(self) -> dict[str, float] | None:
        if self._connected_gantry_config is None:
            return None
        volume = self._connected_gantry_config.get("working_volume")
        if not isinstance(volume, dict):
            return None
        try:
            return {
                key: float(volume[key])
                for key in ("x_min", "x_max", "y_min", "y_max", "z_min", "z_max")
            }
        except (KeyError, TypeError, ValueError):
            return None

    def _validate_manual_move_target_locked(
        self,
        *,
        x: float,
        y: float,
        z: float,
    ) -> None:
        for axis, value in (("X", x), ("Y", y), ("Z", z)):
            if not math.isfinite(float(value)):
                raise ValueError(f"Manual move {axis} target must be finite.")
        volume = self._connected_working_volume_locked()
        if volume is None:
            raise MovementOutOfBoundsError(
                "Manual absolute moves require a loaded gantry working_volume. "
                "Reconnect with a valid gantry YAML before using Move To.",
                requires_reconnect=True,
            )
        violations = []
        for axis, value in (("x", x), ("y", y), ("z", z)):
            lower = volume[f"{axis}_min"]
            upper = volume[f"{axis}_max"]
            numeric = float(value)
            if numeric < lower or numeric > upper:
                violations.append(
                    f"{axis.upper()}={numeric:g} outside [{lower:g}, {upper:g}]"
                )
        if violations:
            raise MovementOutOfBoundsError(
                "Manual move target outside configured gantry working volume: "
                + "; ".join(violations)
            )

    def _validate_jog_target_locked(self, *, x: float, y: float, z: float) -> None:
        for axis, value in (("X", x), ("Y", y), ("Z", z)):
            if not math.isfinite(float(value)):
                raise ValueError(f"Jog {axis} delta must be finite.")
        if self._calibration_jog_bypass_working_volume:
            return

        volume = self._connected_working_volume_locked()
        if volume is None:
            raise MovementOutOfBoundsError(
                "Manual jogs require a loaded gantry working_volume. Reconnect "
                "with a valid gantry YAML before jogging.",
                requires_reconnect=True,
            )
        current = self._current_work_position_locked()
        violations = []
        for axis, delta in (("x", x), ("y", y), ("z", z)):
            target = current[axis] + float(delta)
            lower = volume[f"{axis}_min"]
            upper = volume[f"{axis}_max"]
            if target < lower or target > upper:
                violations.append(
                    f"{axis.upper()} target {target:g} outside [{lower:g}, {upper:g}]"
                )
        if violations:
            raise MovementOutOfBoundsError(
                "Jog target outside configured gantry working volume: "
                + "; ".join(violations)
            )

    def _current_work_position_locked(self) -> dict[str, float]:
        info = self._require_connected().get_position_info()
        raw = info.get("work_pos") or info.get("coords")
        if not isinstance(raw, dict):
            raise MovementOutOfBoundsError(
                "Jog working-volume checks require current gantry position. "
                "Reconnect before jogging.",
                requires_reconnect=True,
            )
        try:
            return {axis: float(raw[axis]) for axis in ("x", "y", "z")}
        except (KeyError, TypeError, ValueError) as exc:
            raise MovementOutOfBoundsError(
                "Jog working-volume checks require finite current gantry "
                "position. Reconnect before jogging.",
                requires_reconnect=True,
            ) from exc

    def _wait_until_idle_locked(
        self,
        *,
        timeout_s: float = 10.0,
        poll_interval_s: float = 0.1,
    ) -> None:
        deadline = time.monotonic() + timeout_s
        last_status = ""
        while time.monotonic() < deadline:
            last_status = str(self._require_connected().get_status())
            lowered = last_status.lower()
            if "idle" in lowered:
                return
            if looks_like_limit_alarm(lowered):
                raise GantryAlarmError(
                    "Gantry entered an alarm state while waiting for motion "
                    "to finish. Run limit recovery before continuing."
                )
            if "error" in lowered:
                raise GantrySessionError(
                    f"Gantry entered {last_status} while waiting for motion "
                    "to finish"
                )
            self._sleep(poll_interval_s)
        raise GantrySessionError(
            "Timed out waiting for gantry to become idle; "
            f"last status: {last_status}"
        )

    def _apply_calibration_grbl_baseline_locked(self) -> tuple[float, float | None]:
        status_report = 0.0
        homing_pull_off = self._connected_grbl_setting_locked("homing_pull_off")
        gantry = self._require_connected()
        gantry.set_grbl_setting("$10", status_report)
        if homing_pull_off is not None:
            gantry.set_grbl_setting("$27", homing_pull_off)
        return status_report, homing_pull_off

    def _restore_calibration_soft_limits_if_needed_locked(self) -> None:
        if self._gantry is None or not self._calibration_restore_soft_limits:
            return
        self._gantry.set_soft_limits_enabled(True)
        self._calibration_restore_soft_limits = (
            self._gantry.soft_limits_enabled() is not True
        )
        if self._calibration_restore_soft_limits:
            raise GantrySessionError(
                "Soft-limit restore did not verify $20=1 on the controller."
            )

    def _restore_calibration_hard_limits_if_needed_locked(
        self,
        *,
        keep_if_configured: bool = False,
    ) -> None:
        """Undo the calibration-window $21=1 enforcement.

        Only runs when prepare actually flipped $21 on (the flag). With
        ``keep_if_configured`` (the finalize path), a gantry YAML that
        configures ``hard_limits`` keeps $21=1 permanently instead of
        reverting. A failed revert is logged, not raised — the machine is
        left in the safer state.
        """
        if self._gantry is None or not self._calibration_restore_hard_limits:
            return
        if keep_if_configured and self._connected_grbl_setting_locked("hard_limits"):
            self._calibration_restore_hard_limits = False
            return
        try:
            self._gantry.set_hard_limits_enabled(False)
        except Exception as exc:
            self.logger.warning(
                "Could not restore pre-calibration $21=0 (hard limits stay "
                "enabled): %s",
                exc,
            )
        self._calibration_restore_hard_limits = False

    def _attempt_soft_limit_restore_after_calibration_failure_locked(
        self,
        gantry: Gantry,
    ) -> None:
        self._calibration_restore_soft_limits = True
        try:
            gantry.set_soft_limits_enabled(True)
            self._calibration_restore_soft_limits = (
                gantry.soft_limits_enabled() is not True
            )
        except Exception:
            self._calibration_restore_soft_limits = True

    def _connected_grbl_setting_locked(self, field_name: str) -> float | None:
        if self._connected_gantry_config is None:
            return None
        settings = self._connected_gantry_config.get("grbl_settings") or {}
        if not isinstance(settings, dict):
            return None
        value = settings.get(field_name)
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"Connected gantry grbl_settings.{field_name} must be numeric."
            ) from None
        if not math.isfinite(numeric):
            raise ValueError(
                f"Connected gantry grbl_settings.{field_name} must be finite."
            )
        if field_name == "homing_pull_off" and numeric < 0:
            raise ValueError(
                "Connected gantry grbl_settings.homing_pull_off must be "
                "non-negative."
            )
        return numeric

    def _extract_status(self) -> str:
        gantry = self._gantry
        if gantry is None:
            return "Not connected"
        if hasattr(gantry, "_extract_status"):
            try:
                return str(gantry._extract_status())
            except Exception:
                pass
        if self._last_position is not None:
            return self._last_position.status
        return "Unknown"

    def _require_connected(self) -> Gantry:
        if self._gantry is None:
            raise GantryNotConnectedError("Gantry not connected")
        return self._gantry

    def _locked_gantry(self) -> _LockedGantry:
        return _LockedGantry(self)

    @staticmethod
    def _normalize_grbl_setting_code(setting: str) -> str:
        raw = setting.strip()
        if not re.fullmatch(r"\$?\d+", raw):
            raise ValueError("GRBL setting must be a numeric code like $20 or 20.")
        return raw if raw.startswith("$") else f"${raw}"

    @staticmethod
    def _parse_grbl_setting_value(value: str | float | int) -> float:
        raw = str(value).strip()
        if raw == "":
            raise ValueError("GRBL setting value cannot be empty.")
        if "\n" in raw or "\r" in raw:
            raise ValueError("GRBL setting value cannot contain newlines.")
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError("GRBL setting value must be numeric.") from exc

    @staticmethod
    def _looks_like_alarm_error(exc: Exception) -> bool:
        return looks_like_limit_alarm(str(exc))

    @staticmethod
    def _alarm_status_from_error(exc: Exception) -> str:
        message = str(exc).strip()
        lower = message.lower()
        if "alarm" in lower:
            alarm_text = message[lower.index("alarm") :]
            return alarm_text.split()[0].strip(",.;")
        return message or "Alarm"

    @staticmethod
    def _looks_like_feed_hold_timeout(exc: Exception) -> bool:
        message = str(exc).lower()
        return (
            "command execution timed out" in message
            and "executing command !" in message
        )


class _LockedGantry:
    def __init__(self, session: GantrySession) -> None:
        self._session = session

    def __enter__(self) -> Gantry:
        self._session._lock.acquire()
        return self._session._require_connected()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._session._lock.release()
