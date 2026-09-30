from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from .calibration_utils import (
    round_mm,
    validate_homing_pull_off_mm,
    validate_status_report,
)
from .coordinate_translator import (
    to_machine_coordinates,
    translate_status_string,
)
from .grbl_settings import format_setting_value, normalize_expected_grbl_settings
from .gantry_driver.driver import DEFAULT_FEED_RATE, Mill
from .errors import (
    CommandExecutionError,
    LocationNotFound,
    MillConnectionError,
    StatusReturnError,
)
from .origin import (
    calculate_deck_origin_z_calibration,
    format_set_work_position_command,
)

_STATUS_RE = re.compile(r"<([^|>]+)")

logger = logging.getLogger(__name__)


class Gantry:
    """High-level gantry wrapper around the low-level Mill driver.

    CubOS coordinates use a signed frame at this boundary, selected per
    gantry config by ``origin_policy`` (see ``cubos.gantry.origin``):
    ``deck_origin`` (default) puts WPos zero at the front-left-bottom deck
    corner with a non-negative working volume; ``home_origin`` puts WPos
    zero at the homed back-right-top corner with a non-positive working
    volume. Both use +X operator-right, +Y back, +Z up. Controller GRBL
    settings are expected to make WPos match the configured frame.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        offline: bool = False,
    ) -> None:
        self.config = config or {}
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self._offline = offline
        self._offline_coords = {"x": 0.0, "y": 0.0, "z": 0.0}
        self._mill: Mill | None = None if offline else Mill()
        self._expected_grbl_settings_override: Dict[str, float] | None = None
        self._expected_grbl_settings_source: str | None = None

    def _configured_default_feed_rate(self) -> Optional[float]:
        """Read cnc.default_feed_rate_mm_min from config, if set."""
        if isinstance(self.config, dict):
            cnc = self.config.get("cnc", {})
            if isinstance(cnc, dict) and "default_feed_rate_mm_min" in cnc:
                return float(cnc["default_feed_rate_mm_min"])
        return None

    @property
    def factory_z_travel_mm(self) -> Optional[float]:
        """Return configured factory Z travel span, if available."""
        if isinstance(self.config, dict):
            cnc = self.config.get("cnc", {})
            if isinstance(cnc, dict) and "factory_z_travel_mm" in cnc:
                return float(cnc["factory_z_travel_mm"])
            working_volume = self.config.get("working_volume", {})
            if isinstance(working_volume, dict) and "z_max" in working_volume:
                return float(working_volume["z_max"])
            return None

        if hasattr(self.config, "factory_z_travel_mm"):
            return float(getattr(self.config, "factory_z_travel_mm"))
        return None

    def connect(self, port: str | None = None) -> None:
        """Connect to the CNC mill.

        If ``port`` is omitted, the low-level driver auto-scans serial ports.
        Setup scripts use this public override instead of reaching into the
        driver instance.
        """
        if self._offline:
            return
        assert self._mill is not None
        feed_rate = self._configured_default_feed_rate()
        if feed_rate is not None:
            self._mill.default_feed_rate = feed_rate
        try:
            if port is None:
                self.logger.info("Connecting to gantry via auto-scan")
            else:
                self.logger.info("Connecting to gantry via %s", port)
            self._mill.connect(port=port)
            self._validate_grbl_settings()
            self._check_alarm_state()
        except MillConnectionError as exc:
            self.logger.error("Error connecting to gantry: %s", exc)
            raise

    def connected_port(self) -> str | None:
        """Return the connected serial port, if one is available."""
        if self._offline:
            return None
        assert self._mill is not None
        return self._mill.connected_port()

    def disconnect(self) -> None:
        """Close the controller connection.

        Re-raises :class:`MillConnectionError` so callers know the port may
        still be open — silently swallowing it left stale serial handles that
        could later be reused by a reconnect.
        """
        if self._offline:
            return
        assert self._mill is not None
        try:
            self._mill.disconnect()
        except MillConnectionError as exc:
            self.logger.error("Error disconnecting gantry: %s", exc)
            raise

    def is_healthy(self) -> bool:
        """Check if the gantry is connected and healthy."""
        if self._offline:
            return True
        assert self._mill is not None
        if not self._mill.active_connection:
            self.logger.warning("Health check: no active connection")
            return False

        try:
            if not self._mill.is_connected():
                self.logger.warning("Health check: serial port not open")
                return False

            status = self._mill.current_status()
            if "alarm" in status.lower() or "error" in status.lower():
                self.logger.warning("Health check: unhealthy status: %s", status)
                return False

            return True
        except (MillConnectionError, StatusReturnError) as exc:
            self.logger.warning("Health check failed: %s", exc)
            return False

    def home(self) -> None:
        """Home the gantry with GRBL's standard homing command."""
        if self._offline:
            return
        assert self._mill is not None
        try:
            self._mill.home()
        except (MillConnectionError, StatusReturnError) as exc:
            self.logger.error("Error homing gantry: %s", exc)
            raise

    def prepare_for_protocol_run(self) -> None:
        """Clear any startup alarm and restore controller state."""
        if self._offline:
            return
        assert self._mill is not None

        raw_status = self._mill.query_raw_status()
        if not raw_status or "alarm" not in raw_status.lower():
            return

        self.logger.warning(
            "GRBL alarm detected before protocol run; unlocking controller. "
            "Status: %s",
            raw_status,
        )
        self.reset_and_unlock()
        self._restore_controller_state()

        final_status = self._mill.query_raw_status()
        if final_status and "alarm" in final_status.lower():
            raise MillConnectionError(
                f"Gantry remained in alarm after unlock. Status: {final_status}"
            )

    def _restore_controller_state(self) -> None:
        """Re-run controller initialization skipped when connect saw Alarm."""
        if self._offline:
            return
        assert self._mill is not None

        self._mill.read_config()
        self._mill.clear_buffers()
        status = self._mill.query_raw_status()
        if status:
            self._mill.enforce_wpos_mode()
        self._mill.set_feed_rate(DEFAULT_FEED_RATE)
        if status:
            self._mill.seed_wco()
        self._validate_grbl_settings()

    def move_to(
        self,
        x: float,
        y: float,
        z: float,
        travel_z: Optional[float] = None,
    ) -> None:
        """Move to absolute gantry coordinates.

        ``travel_z``, if given, becomes the Z during XY travel: the gantry
        lifts/lowers to it at the current XY before moving XY, then
        descends/ascends to the target Z. This is how higher layers
        (InstrumentedGantry, protocol commands) express "travel above this labware" without the
        mill baking in a machine-wide retract.
        """
        if self._offline:
            if travel_z is not None:
                # Dry runs only record the final tip pose. Log the transit
                # height so offline protocol validation can still reason
                # about approach path (e.g. assert it stays above labware).
                self.logger.debug(
                    "Offline move to (%s, %s, %s) via travel_z=%s", x, y, z, travel_z,
                )
            self._offline_coords = {"x": x, "y": y, "z": z}
            return
        assert self._mill is not None
        try:
            machine_x, machine_y, machine_z = to_machine_coordinates(x, y, z)
            machine_travel_z = (
                to_machine_coordinates(0.0, 0.0, travel_z)[2]
                if travel_z is not None
                else None
            )
            self._mill.move_to(
                x_coordinate=machine_x,
                y_coordinate=machine_y,
                z_coordinate=machine_z,
                travel_z=machine_travel_z,
            )
        except (
            MillConnectionError,
            StatusReturnError,
            CommandExecutionError,
            ValueError,
        ) as exc:
            self.logger.error(
                "Error moving gantry to (%s, %s, %s): %s", x, y, z, exc
            )
            raise

    def jog(
        self,
        x: float = 0,
        y: float = 0,
        z: float = 0,
        feed_rate: float = 2000,
    ) -> None:
        """Jog by a relative gantry offset."""
        if self._offline:
            self._offline_coords = {
                "x": self._offline_coords["x"] + x,
                "y": self._offline_coords["y"] + y,
                "z": self._offline_coords["z"] + z,
            }
            return
        assert self._mill is not None
        try:
            self._mill.jog(x=x, y=y, z=z, feed_rate=feed_rate)
        except (MillConnectionError, CommandExecutionError) as exc:
            self.logger.error("Jog error: %s", exc)
            raise

    def jog_cancel(self) -> None:
        """Cancel any in-progress jog motion immediately."""
        if self._offline:
            return
        assert self._mill is not None
        try:
            self._mill.jog_cancel()
        except (MillConnectionError, CommandExecutionError) as exc:
            self.logger.error("Jog cancel error: %s", exc)
            raise

    def soft_reset(self) -> None:
        """Send a GRBL soft reset (Ctrl-X) to the controller."""
        if self._offline:
            return
        assert self._mill is not None
        try:
            self._mill.soft_reset()
        except Exception as exc:
            self.logger.error("Soft reset error: %s", exc)
            raise

    def unlock(self) -> None:
        """Send GRBL unlock command ($X) to clear alarm state."""
        if self._offline:
            return
        assert self._mill is not None
        try:
            self._mill.unlock()
        except (MillConnectionError, CommandExecutionError) as exc:
            self.logger.error("Unlock error: %s", exc)
            raise

    def reset_and_unlock(self) -> None:
        """Soft reset + unlock in a single serial sequence."""
        if self._offline:
            return
        assert self._mill is not None
        try:
            self._mill.soft_reset_and_unlock()
        except (MillConnectionError, CommandExecutionError) as exc:
            self.logger.error("Reset and unlock error: %s", exc)
            raise

    def get_status(self) -> str:
        """Return the current status string with normalized coordinates."""
        if self._offline:
            return "Idle"
        assert self._mill is not None
        try:
            return translate_status_string(self._mill.current_status())
        except (MillConnectionError, StatusReturnError) as exc:
            self.logger.error("Error getting status: %s", exc)
            raise

    def stop(self) -> None:
        """Immediately stop all gantry motion (GRBL feed hold)."""
        if self._offline:
            return
        assert self._mill is not None
        try:
            self._mill.stop()
        except (MillConnectionError, CommandExecutionError) as exc:
            self.logger.error("Error stopping gantry: %s", exc)
            raise

    def feed_hold_realtime(self) -> None:
        """Send GRBL feed hold without reading the serial stream."""
        if self._offline:
            return
        assert self._mill is not None
        try:
            self._mill.feed_hold_realtime()
        except MillConnectionError as exc:
            self.logger.error("Error sending realtime feed hold: %s", exc)
            raise

    def resume(self) -> None:
        """Resume a feed-held GRBL controller."""
        if self._offline:
            return
        assert self._mill is not None
        try:
            self._mill.resume()
        except MillConnectionError as exc:
            self.logger.error("Error resuming gantry: %s", exc)
            raise

    def get_coordinates(self) -> Dict[str, float]:
        """Return current gantry coordinates as a dict."""
        if self._offline:
            return dict(self._offline_coords)
        assert self._mill is not None
        coords = self._mill.current_coordinates()
        return {"x": float(coords.x), "y": float(coords.y), "z": float(coords.z)}

    def get_position_info(self) -> Dict[str, Any]:
        """Return coordinates, work position, and last-known status."""
        if self._offline:
            coords = dict(self._offline_coords)
            return {"coords": dict(coords), "work_pos": dict(coords), "status": "Idle"}

        assert self._mill is not None
        coords = self._mill.current_coordinates()
        user_coords = {"x": float(coords.x), "y": float(coords.y), "z": float(coords.z)}
        status = self._extract_status()
        return {
            "coords": dict(user_coords),
            "work_pos": dict(user_coords),
            "status": status,
        }

    def set_serial_timeout(self, timeout: float) -> None:
        """Set the serial read timeout on the active mill connection."""
        if self._offline:
            return
        assert self._mill is not None
        self._mill.set_read_timeout(timeout)

    def get_serial_timeout(self) -> float | None:
        """Return the serial read timeout on the active mill connection."""
        if self._offline:
            return None
        assert self._mill is not None
        return self._mill.get_read_timeout()

    def clear_g92_offsets(self) -> None:
        """Clear transient G92 offsets before assigning a durable WPos."""
        if self._offline:
            return
        assert self._mill is not None
        try:
            self._mill.execute_command("G92.1")
            self.logger.info("G92 offsets cleared")
        except (MillConnectionError, CommandExecutionError) as exc:
            self.logger.error("Error clearing G92 offsets: %s", exc)
            raise

    def enforce_work_position_reporting(self) -> None:
        """Force GRBL status reports to use WPos and absolute positioning."""
        if self._offline:
            return
        assert self._mill is not None
        try:
            self._mill.enforce_wpos_mode()
        except CommandExecutionError as exc:
            self.logger.error("Error enforcing WPos status reporting: %s", exc)
            raise

    def activate_work_coordinate_system(self, system: str = "G54") -> None:
        """Select the active GRBL work coordinate system."""
        if system not in {"G54", "G55", "G56", "G57", "G58", "G59"}:
            raise ValueError(f"Unsupported work coordinate system: {system!r}")
        if self._offline:
            return
        assert self._mill is not None
        try:
            self._mill.execute_command(system)
            self.logger.info("Activated work coordinate system %s", system)
        except (MillConnectionError, CommandExecutionError) as exc:
            self.logger.error(
                "Error activating work coordinate system %s: %s",
                system,
                exc,
            )
            raise

    def set_work_coordinates(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
    ) -> None:
        """Assign the current physical pose to the given work coordinates."""
        if x is None and y is None and z is None:
            raise ValueError("At least one work-coordinate axis must be supplied.")
        x_work, y_work, z_work = (None, None, None)
        if x is not None or y is not None or z is not None:
            tx = 0.0 if x is None else x
            ty = 0.0 if y is None else y
            tz = 0.0 if z is None else z
            translated = to_machine_coordinates(tx, ty, tz)
            if x is not None:
                x_work = translated[0]
            if y is not None:
                y_work = translated[1]
            if z is not None:
                z_work = translated[2]
        if self._offline:
            if x_work is not None:
                self._offline_coords["x"] = x_work
            if y_work is not None:
                self._offline_coords["y"] = y_work
            if z_work is not None:
                self._offline_coords["z"] = z_work
            return
        assert self._mill is not None
        try:
            self._mill.execute_command(
                format_set_work_position_command(x_work, y_work, z_work)
            )
            self.logger.info(
                "Work coordinates set to X=%s Y=%s Z=%s",
                x_work,
                y_work,
                z_work,
            )
        except (MillConnectionError, CommandExecutionError) as exc:
            self.logger.error("Error setting work coordinates: %s", exc)
            raise

    def configure_speeds(
        self,
        homing_feed: Optional[float] = None,
        homing_seek: Optional[float] = None,
        max_rate: Optional[float] = None,
        acceleration: Optional[float] = None,
    ) -> None:
        """Apply speed and acceleration overrides to the GRBL controller."""
        if self._offline:
            return
        assert self._mill is not None
        if homing_feed is not None:
            self._mill.set_grbl_setting("24", str(homing_feed))
        if homing_seek is not None:
            self._mill.set_grbl_setting("25", str(homing_seek))
        if max_rate is not None:
            for code in ("110", "111", "112"):
                self._mill.set_grbl_setting(code, str(max_rate))
        if acceleration is not None:
            for code in ("120", "121", "122"):
                self._mill.set_grbl_setting(code, str(acceleration))
        self.logger.info("Speed config applied")

    def set_expected_grbl_settings(
        self,
        settings: Dict[str, float] | None,
        *,
        source: str = "gantry",
    ) -> None:
        """Set runtime GRBL expectations loaded from machine configuration."""
        self._expected_grbl_settings_override = dict(settings) if settings else None
        self._expected_grbl_settings_source = source if settings else None

    def read_grbl_settings(self) -> Dict[str, str]:
        """Read live GRBL settings from the connected controller."""
        if self._offline:
            return {}
        assert self._mill is not None
        return self._mill.read_grbl_settings()

    def set_grbl_setting(self, setting: str, value: float | int | bool) -> None:
        """Set one GRBL ``$`` setting."""
        if self._offline:
            return
        assert self._mill is not None
        code = setting[1:] if setting.startswith("$") else setting
        self._mill.set_grbl_setting(code, format_setting_value(value))

    def soft_limits_enabled(self) -> bool | None:
        """Return whether GRBL soft limits are enabled, if readable."""
        settings = self.read_grbl_settings()
        raw = settings.get("$20", settings.get("20"))
        if raw is None:
            return None
        try:
            return float(raw) != 0.0
        except (TypeError, ValueError):
            return None

    def set_soft_limits_enabled(self, enabled: bool) -> None:
        """Enable or disable GRBL soft limits through Gantry semantics."""
        self.set_grbl_setting("$20", 1 if enabled else 0)

    def hard_limits_enabled(self) -> bool | None:
        """Return whether GRBL hard limits ($21) are enabled, if readable."""
        settings = self.read_grbl_settings()
        raw = settings.get("$21", settings.get("21"))
        if raw is None:
            return None
        try:
            return float(raw) != 0.0
        except (TypeError, ValueError):
            return None

    def set_hard_limits_enabled(self, enabled: bool) -> None:
        """Enable or disable GRBL hard limits through Gantry semantics."""
        self.set_grbl_setting("$21", 1 if enabled else 0)

    def homing_pull_off_mm(self) -> float:
        """Return GRBL $27 homing pull-off in millimeters."""
        if self._offline:
            settings = {}
            if isinstance(self.config, dict):
                raw_settings = self.config.get("grbl_settings") or {}
                if isinstance(raw_settings, dict):
                    settings = raw_settings
            raw = settings.get("$27", settings.get("27", settings.get("homing_pull_off")))
            return validate_homing_pull_off_mm(0.0 if raw is None else raw)

        settings = self.read_grbl_settings()
        raw = settings.get("$27", settings.get("27"))
        if raw is None:
            raise MillConnectionError(
                "Cannot finalize deck-origin calibration because live GRBL "
                "$27 homing pull-off is missing."
            )
        return validate_homing_pull_off_mm(raw)

    def _configured_grbl_setting(self, field_name: str) -> Any:
        """Return a configured GRBL setting by schema field name, if present."""
        if isinstance(self.config, dict):
            raw_settings = self.config.get("grbl_settings") or {}
            if isinstance(raw_settings, dict):
                return raw_settings.get(field_name)
        return None

    def query_raw_status(self) -> str:
        """Return one raw GRBL status string for diagnostics/recovery."""
        if self._offline:
            return "<Idle|WPos:0.000,0.000,0.000>"
        assert self._mill is not None
        return self._mill.query_raw_status()

    def configure_soft_limits_from_spans(
        self,
        *,
        max_travel_x: float,
        max_travel_y: float,
        max_travel_z: float,
        status_report: float | int | None = None,
        homing_pull_off: float | None = None,
        hard_limits: float | int | bool | None = None,
        tolerance_mm: float = 0.001,
    ) -> None:
        """Program GRBL soft limits from calibrated travel spans."""
        spans = {
            "$130": max_travel_x,
            "$131": max_travel_y,
            "$132": max_travel_z,
        }
        invalid = [
            f"{code}={value}"
            for code, value in spans.items()
            if float(value) <= tolerance_mm
        ]
        if invalid:
            raise ValueError(
                "Cannot configure soft limits with non-positive travel spans: "
                + ", ".join(invalid)
            )
        expected_reporting: Dict[str, Any] = {}
        if status_report is not None:
            expected_reporting["$10"] = validate_status_report(status_report)
        if homing_pull_off is not None:
            expected_reporting["$27"] = validate_homing_pull_off_mm(homing_pull_off)
        configured_hard_limits = (
            hard_limits
            if hard_limits is not None
            else self._configured_grbl_setting("hard_limits")
        )
        if configured_hard_limits is not None:
            expected_reporting["$21"] = configured_hard_limits
        if self._offline:
            return

        # Disable soft limits while changing travel extents, then re-enable.
        reporting_written: list[str] = []
        soft_limits_disabled = False
        try:
            for code, value in expected_reporting.items():
                self.set_grbl_setting(code, value)
                reporting_written.append(code)
            self.set_grbl_setting("$20", 0)
            soft_limits_disabled = True
            self.set_grbl_setting("$130", max_travel_x)
            self.set_grbl_setting("$131", max_travel_y)
            self.set_grbl_setting("$132", max_travel_z)
            self.set_grbl_setting("$22", 1)
            self.set_grbl_setting("$20", 1)
            soft_limits_disabled = False
        except Exception as exc:
            if reporting_written:
                logger.warning(
                    "configure_soft_limits_from_spans failed; settings already "
                    "written to controller that were not rolled back: %s",
                    ", ".join(reporting_written),
                )
            if soft_limits_disabled:
                try:
                    self.set_grbl_setting("$20", 1)
                except Exception as restore_exc:
                    raise MillConnectionError(
                        "Failed to restore GRBL soft limits after soft-limit "
                        f"programming failed. Original error: {exc}; "
                        f"restore error: {restore_exc}"
                    ) from exc
            raise

        live = self.read_grbl_settings()
        expected = {
            "$20": 1.0,
            "$22": 1.0,
            "$130": float(max_travel_x),
            "$131": float(max_travel_y),
            "$132": float(max_travel_z),
        }
        expected.update(expected_reporting)
        misses = []
        for code, expected_value in expected.items():
            live_raw = live.get(code)
            if live_raw is None:
                misses.append(f"{code}: missing")
                continue
            if abs(float(live_raw) - expected_value) > tolerance_mm:
                misses.append(f"{code}: expected {expected_value:g}, got {live_raw}")
        if misses:
            raise MillConnectionError(
                "GRBL soft-limit settings did not verify: " + "; ".join(misses)
            )

    def finalize_deck_origin_calibration(
        self,
        *,
        home_z: float,
        block_touch_z: float,
        block_height: float,
        total_z_range: float,
        status_report: float | int | None = 0,
        homing_pull_off: float | None = None,
        hard_limits: float | int | bool | None = None,
        tolerance_mm: float = 0.001,
    ) -> Dict[str, Any]:
        """Finalize single-instrument deck-origin calibration on the controller.

        The current physical pose must already have been assigned to deck-origin
        X=0, Y=0, Z=block_height. This method measures the homed top pose,
        programs GRBL travel spans, re-homes against the new span, then assigns
        that top pose to the calibrated deck-frame maxima so G54 and soft limits
        agree.
        """
        z_calibration = calculate_deck_origin_z_calibration(
            home_z=home_z,
            block_touch_z=block_touch_z,
            block_height=block_height,
            total_z_range=total_z_range,
            tolerance_mm=tolerance_mm,
        )
        configured_pull_off = (
            homing_pull_off
            if homing_pull_off is not None
            else self._configured_grbl_setting("homing_pull_off")
        )
        if self._offline:
            homing_pull_off_mm = validate_homing_pull_off_mm(
                0.0 if configured_pull_off is None else configured_pull_off
            )
        else:
            if configured_pull_off is None:
                homing_pull_off_mm = self.homing_pull_off_mm()
            else:
                homing_pull_off_mm = validate_homing_pull_off_mm(configured_pull_off)
            if status_report is not None:
                self.set_grbl_setting("$10", validate_status_report(status_report))
            if configured_pull_off is not None:
                self.set_grbl_setting("$27", homing_pull_off_mm)

        if self._offline:
            measured = dict(self._offline_coords)
            measured_volume = {
                "x": round_mm(float(measured.get("x", 0.0))),
                "y": round_mm(float(measured.get("y", 0.0))),
                "z": z_calibration.z_max,
            }
            max_travel = {
                "x": round_mm(measured_volume["x"] + homing_pull_off_mm),
                "y": round_mm(measured_volume["y"] + homing_pull_off_mm),
                "z": round_mm(
                    z_calibration.max_travel_z + homing_pull_off_mm
                ),
            }
            self._offline_coords = {
                "x": measured_volume["x"],
                "y": measured_volume["y"],
                "z": measured_volume["z"],
            }
            return {
                "measured_volume": measured_volume,
                "z_calibration": z_calibration.as_dict(),
                "max_travel": max_travel,
                "homing_pull_off_mm": homing_pull_off_mm,
                "position": dict(self._offline_coords),
            }

        self.home()
        measured = self.get_coordinates()
        expected_z_max = z_calibration.z_max
        if abs(float(measured["z"]) - expected_z_max) > tolerance_mm:
            raise MillConnectionError(
                "Homed Z after assigning the block origin did not match the "
                "calculated calibrated Z maximum: "
                f"got {float(measured['z']):g}, expected {expected_z_max:g}."
            )

        measured_volume = {
            "x": round_mm(float(measured["x"])),
            "y": round_mm(float(measured["y"])),
            "z": z_calibration.z_max,
        }
        usable_spans = {
            "x": measured_volume["x"],
            "y": measured_volume["y"],
            "z": z_calibration.max_travel_z,
        }
        invalid = [
            f"{axis}={value:g}"
            for axis, value in usable_spans.items()
            if float(value) <= tolerance_mm
        ]
        if invalid:
            raise ValueError(
                "Cannot finalize calibration with non-positive usable travel spans: "
                + ", ".join(invalid)
            )
        max_travel = {
            "x": round_mm(measured_volume["x"] + homing_pull_off_mm),
            "y": round_mm(measured_volume["y"] + homing_pull_off_mm),
            "z": round_mm(
                z_calibration.max_travel_z + homing_pull_off_mm
            ),
        }

        self.configure_soft_limits_from_spans(
            max_travel_x=max_travel["x"],
            max_travel_y=max_travel["y"],
            max_travel_z=max_travel["z"],
            status_report=status_report,
            homing_pull_off=homing_pull_off_mm,
            hard_limits=hard_limits,
            tolerance_mm=tolerance_mm,
        )
        self.activate_work_coordinate_system("G54")
        self.clear_g92_offsets()
        self.set_work_coordinates(
            x=measured_volume["x"],
            y=measured_volume["y"],
            z=z_calibration.z_max,
        )
        final_position = self.get_coordinates()

        return {
            "measured_volume": measured_volume,
            "z_calibration": z_calibration.as_dict(),
            "max_travel": max_travel,
            "homing_pull_off_mm": homing_pull_off_mm,
            "position": final_position,
        }

    def _extract_status(self) -> str:
        """Extract the GRBL state word from the last raw status string."""
        assert self._mill is not None
        raw = getattr(self._mill, "last_status", "") or ""
        if not raw:
            return "Unknown"
        match = _STATUS_RE.search(raw)
        if match:
            return match.group(1)
        if "alarm" in raw.lower():
            return "Alarm"
        try:
            return translate_status_string(raw)
        except ValueError as exc:
            self.logger.warning("Failed to translate status %r: %s", raw, exc)
            return "Unknown"

    def _expected_grbl_settings(self) -> Optional[Dict[str, float]]:
        """Return expected GRBL settings normalized to $-code keys."""
        if self._expected_grbl_settings_override:
            return dict(self._expected_grbl_settings_override)

        if isinstance(self.config, dict):
            raw = self.config.get("grbl_settings")
            if not raw:
                return None
            return normalize_expected_grbl_settings(raw)

        expected = getattr(self.config, "expected_grbl_settings", None)
        return dict(expected) if expected else None

    def _validate_grbl_settings(self) -> None:
        """Compare configured GRBL expectations against the connected controller."""
        expected = self._expected_grbl_settings()
        if not expected or self._offline:
            return

        assert self._mill is not None
        live = self._mill.read_grbl_settings()
        critical = {
            "$3",
            "$20",
            "$22",
            "$23",
            "$100",
            "$101",
            "$102",
            "$130",
            "$131",
            "$132",
        }
        mismatches = []
        for grbl_code, expected_value in expected.items():
            live_raw = live.get(grbl_code)
            if live_raw is None:
                # A missing critical setting is just as dangerous as a wrong
                # one — record it as a mismatch with a sentinel float so the
                # critical-fail path below still trips.
                level = logging.ERROR if grbl_code in critical else logging.WARNING
                self.logger.log(
                    level, "GRBL setting %s not found on controller", grbl_code
                )
                if grbl_code in critical:
                    mismatches.append((grbl_code, float(expected_value), float("nan")))
                continue
            if abs(float(live_raw) - float(expected_value)) > 0.001:
                mismatches.append((grbl_code, float(expected_value), float(live_raw)))

        if not mismatches:
            self.logger.info("GRBL settings validation passed")
            return

        critical_mismatches = [item for item in mismatches if item[0] in critical]
        for grbl_code, expected_value, live_value in mismatches:
            self.logger.error(
                "GRBL mismatch: %s expected %.3f, controller has %.3f",
                grbl_code,
                expected_value,
                live_value,
            )
        if critical_mismatches:
            details = "; ".join(
                f"{code}: expected {expected_value}, got {live_value}"
                for code, expected_value, live_value in critical_mismatches
            )
            raise MillConnectionError(
                f"Critical GRBL settings mismatch — motion would be wrong. {details}"
            )

    def _check_alarm_state(self) -> None:
        """Log an alarm state after connect without raising."""
        if self._offline:
            return
        assert self._mill is not None
        raw = self._mill.query_raw_status()
        if raw and "Alarm" in raw:
            self.logger.warning(
                "GRBL is in Alarm state after connect. Home to clear. Status: %s",
                raw,
            )
