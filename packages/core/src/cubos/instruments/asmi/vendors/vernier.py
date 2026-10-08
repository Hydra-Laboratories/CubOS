from __future__ import annotations

import math
import statistics
import time
from typing import Optional

from cubos.instruments.asmi.interface import (
    ASMIInstrument,
    DEFAULT_SURFACE_FORCE_THRESHOLD_N,
    DEFAULT_SURFACE_SEARCH_MAX_TRAVEL_MM,
    DEFAULT_SURFACE_SEARCH_STEP_MM,
)
from cubos.instruments.asmi.exceptions import (
    ASMICommandError,
    ASMIConnectionError,
)
from cubos.instruments.asmi.models import ASMIStatus, MeasurementResult

_DEFAULT_FORCE_THRESHOLD = -100
_DEFAULT_SENSOR_CHANNELS = [1]
_STEP_COUNT_SAFETY_MARGIN = 10


def _step_count_bound(z_upper: float, z_lower: float, step_size: float) -> int:
    """Upper bound on steps needed to cross [z_lower, z_upper] at step_size.

    Ceil-based so that a non-integer number of steps still reaches the
    clamped endpoint. Used as a loop-iteration cap to guarantee termination
    if hardware stalls or rounding otherwise prevents the geometric exit
    condition from firing, and — in offline mode — as the actual step count.
    """
    if step_size <= 0:
        raise ValueError(f"step_size must be positive, got {step_size}")
    span = abs(z_upper - z_lower)
    raw_steps = math.ceil(span / step_size) if span > 0 else 0
    return raw_steps + _STEP_COUNT_SAFETY_MARGIN


class VernierASMI(ASMIInstrument):
    """Driver for the ASMI force sensor (Vernier GoDirect).

    Connects to a GoDirect force sensor over USB and provides force
    measurements.  All positioning is handled by the instrumented gantry.

    Pass ``offline=True`` for dry runs and testing — no USB connection,
    all readings return ``default_force``.
    """

    def __init__(
        self,
        name: Optional[str] = None,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
        depth: float = 0.0,
        offline: bool = False,  # passed through to BaseInstrument
        default_force: float = 0.0,
        force_threshold: float = _DEFAULT_FORCE_THRESHOLD,
        sensor_channels: Optional[list[int]] = None,
        step_size: float = 0.01,
        force_limit: float = 15.0,
        baseline_samples: int = 10,
        idle_timeout: float = 10.0,
        surface_search_step: float = DEFAULT_SURFACE_SEARCH_STEP_MM,
        surface_force_threshold: float = DEFAULT_SURFACE_FORCE_THRESHOLD_N,
        surface_search_max_travel: float = DEFAULT_SURFACE_SEARCH_MAX_TRAVEL_MM,
    ):
        super().__init__(
            name=name, offset_x=offset_x, offset_y=offset_y,
            depth=depth, offline=offline,
        )
        self._default_force = default_force
        self._force_threshold = force_threshold
        self._sensor_channels = sensor_channels or list(_DEFAULT_SENSOR_CHANNELS)
        self._step_size = step_size
        self._force_limit = force_limit
        self._baseline_samples = baseline_samples
        self._idle_timeout = idle_timeout
        self._surface_search_step = surface_search_step
        self._surface_force_threshold = surface_force_threshold
        self._surface_search_max_travel = surface_search_max_travel
        self._godirect = None
        self._device = None
        self._sensor = None

    # ── BaseInstrument interface ──────────────────────────────────────────

    def connect(self) -> None:
        if self._offline:
            self.logger.info("ASMI connected (offline)")
            return
        try:
            from godirect import GoDirect
        except ImportError as exc:
            raise ASMIConnectionError(
                "godirect package required: pip install godirect"
            ) from exc

        try:
            self._godirect = GoDirect(use_ble=False, use_usb=True)
            device = self._godirect.get_device(threshold=self._force_threshold)
            if device is None:
                raise ASMIConnectionError(
                    "No GoDirect force sensor found. Check USB connection."
                )
            if not device.open(auto_start=False):
                raise ASMIConnectionError("Failed to open GoDirect device")

            device.enable_sensors(self._sensor_channels)
            sensors = device.get_enabled_sensors()
        except ASMIConnectionError:
            raise
        except Exception as exc:
            raise ASMIConnectionError(f"GoDirect connection failed: {exc}") from exc
        if not sensors:
            device.close()
            raise ASMIConnectionError("No sensors enabled on GoDirect device")

        self._device = device
        self._sensor = sensors[0]
        self.logger.info(
            "Connected to force sensor: %s", self._sensor.sensor_description
        )

    def disconnect(self) -> None:
        if self._offline:
            self.logger.info("ASMI disconnected (offline)")
            return
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
            self._device = None
            self._sensor = None
        if self._godirect is not None:
            try:
                self._godirect.quit()
            except Exception:
                pass
            self._godirect = None
        self.logger.info("ASMI force sensor disconnected")

    def health_check(self) -> bool:
        if self._offline:
            return True
        if self._device is None or self._sensor is None:
            return False
        try:
            self.measure()
            return True
        except Exception:
            return False

    # ── ASMI-specific commands ────────────────────────────────────────────

    def measure(self, n_samples: int = 1) -> MeasurementResult:
        """Take one or more force readings and return the result."""
        if self._offline:
            readings = tuple(self._default_force for _ in range(n_samples))
            return MeasurementResult(
                readings=readings,
                mean_n=self._default_force,
                std_n=0.0,
                timestamp=time.time(),
            )

        if self._device is None or self._sensor is None:
            raise ASMICommandError("Force sensor not connected")

        readings: list[float] = []
        for _ in range(n_samples):
            self._device.start()
            try:
                if not self._device.read():
                    raise ASMICommandError("GoDirect force sensor read failed")
                value = self._sensor.values[0]
                self._sensor.clear()
            except ASMICommandError:
                raise
            except Exception as exc:
                raise ASMICommandError(f"GoDirect force sensor read failed: {exc}") from exc
            finally:
                self._device.stop()
            readings.append(value)

        mean = statistics.mean(readings)
        std = statistics.stdev(readings) if len(readings) > 1 else 0.0
        return MeasurementResult(
            readings=tuple(readings),
            mean_n=mean,
            std_n=std,
            timestamp=time.time(),
        )

    def get_status(self) -> ASMIStatus:
        """Return a snapshot of the sensor state."""
        if self._offline:
            return ASMIStatus(
                is_connected=True,
                sensor_description="OfflineSensor",
            )
        description = None
        if self._sensor is not None:
            try:
                description = self._sensor.sensor_description
            except Exception:
                pass
        return ASMIStatus(
            is_connected=self._device is not None and self._sensor is not None,
            sensor_description=description,
        )

    # ── Convenience methods ───────────────────────────────────────────────

    def get_force_reading(self) -> float:
        """Take a single force reading and return the value in Newtons."""
        return self.measure(n_samples=1).mean_n

    def get_baseline_force(self, samples: int = 10) -> tuple[float, float]:
        """Collect multiple force readings and return (mean, std) in Newtons."""
        result = self.measure(n_samples=samples)
        return (result.mean_n, result.std_n)

    def is_connected(self) -> bool:
        """Check if the force sensor is connected and operational."""
        if self._offline:
            return True
        return self._device is not None and self._sensor is not None

    # ── Indentation measurement ───────────────────────────────────────────

    def _wait_for_idle(self, gantry) -> bool:
        start = time.time()
        while time.time() - start < self._idle_timeout:
            if "Idle" in gantry.get_status():
                return True
            time.sleep(0.02)
        return False

    def _move_z(self, gantry, x, y, z):
        gantry.move_to(x, y, z)
        if not self._wait_for_idle(gantry):
            raise ASMICommandError(
                f"Gantry did not become Idle within {self._idle_timeout:.2f}s "
                f"after ASMI Z move to {z}."
            )

    @staticmethod
    def _validate_indentation_parameters(
        measurement_height: float,
        indentation_limit_height: float,
        step_size: float,
        *,
        detect_surface: bool = False,
        surface_search_step: float | None = None,
        surface_force_threshold: float | None = None,
        surface_search_max_travel: float | None = None,
    ) -> None:
        """Validate indentation parameters.

        ``measurement_height`` and ``indentation_limit_height`` are
        labware-relative offsets (mm above the well surface; +above,
        -below). ``indentation_limit_height`` must be at or below
        ``measurement_height``; equality is legal (a zero-descent
        indentation collects baseline force samples and returns) and
        matches the inclusive ``≤`` spec used by the engine and
        validator.

        With ``detect_surface`` the limit is anchored to the detected
        surface instead, so it must be at or below zero — the two heights
        live in different frames and are not compared to each other.
        """
        if step_size <= 0:
            raise ValueError(f"step_size must be positive, got {step_size}")
        if detect_surface:
            if surface_search_step is None or surface_search_step <= 0:
                raise ValueError(
                    f"surface_search_step must be positive, got "
                    f"{surface_search_step}"
                )
            if surface_force_threshold is None or surface_force_threshold <= 0:
                raise ValueError(
                    f"surface_force_threshold must be positive, got "
                    f"{surface_force_threshold}"
                )
            if surface_search_max_travel is None or surface_search_max_travel <= 0:
                raise ValueError(
                    f"surface_search_max_travel must be positive, got "
                    f"{surface_search_max_travel}"
                )
            if indentation_limit_height > 0:
                raise ValueError(
                    f"indentation_limit_height ({indentation_limit_height}) "
                    "must be at or below 0 when detect_surface is enabled — "
                    "it is anchored to the detected sample surface "
                    "(negative = into the sample)."
                )
        elif indentation_limit_height > measurement_height:
            raise ValueError(
                f"indentation_limit_height ({indentation_limit_height}) "
                f"must be at or below measurement_height "
                f"({measurement_height}) — indentation descends in -Z."
            )

    def indentation(
        self,
        gantry,
        *,
        measurement_height: float,
        indentation_limit_height: float,
        well_z: float,
        step_size: float | None = None,
        force_limit: float | None = None,
        baseline_samples: int | None = None,
        measure_with_return: bool = False,
        detect_surface: bool = False,
        surface_search_step: float | None = None,
        surface_force_threshold: float | None = None,
        surface_search_max_travel: float | None = None,
    ) -> dict:
        """Perform step-by-step indentation at the current XY position.

        Z inputs are labware-relative offsets resolved against ``well_z``.
        Descent decreases deck-frame Z; optional return samples increase Z
        back to the action plane. Every measurement is tagged with direction.

        With ``detect_surface`` the probe first coarse-searches downward
        from the measurement plane until the baseline-corrected force
        changes by more than ``surface_force_threshold`` on two
        consecutive reads at the same position (a crossing that doesn't
        hold on re-read is treated as sensor noise and the search
        continues), backs off one ``surface_search_step``, and anchors
        ``indentation_limit_height`` to that detected surface. Search
        samples are not recorded in the measurement list — only the
        detected surface is reported in the result. The search aborts
        with :class:`ASMICommandError` if no surface is found within
        ``surface_search_max_travel`` mm.
        """
        _step_size, _force_limit, _baseline_samples = (
            self._resolve_indentation_settings(
                step_size=step_size,
                force_limit=force_limit,
                baseline_samples=baseline_samples,
            )
        )
        _search_step, _search_threshold, _search_max_travel = (
            self._resolve_surface_search_settings(
                surface_search_step=surface_search_step,
                surface_force_threshold=surface_force_threshold,
                surface_search_max_travel=surface_search_max_travel,
            )
        )
        self._validate_indentation_parameters(
            measurement_height, indentation_limit_height, _step_size,
            detect_surface=detect_surface,
            surface_search_step=_search_step,
            surface_force_threshold=_search_threshold,
            surface_search_max_travel=_search_max_travel,
        )
        action_z, target_z = self._resolve_indentation_z_targets(
            well_z=well_z,
            measurement_height=measurement_height,
            indentation_limit_height=indentation_limit_height,
        )

        if self._offline:
            # Offline detection is deterministic: the surface is "found"
            # at the measurement plane itself, so the geometry matches a
            # normal run with the limit re-anchored to ``action_z``.
            surface_info = None
            if detect_surface:
                target_z = action_z + indentation_limit_height
                surface_info = self._surface_detection_info(
                    surface_z=action_z,
                    trigger_force_n=0.0,
                    search_step=_search_step,
                    force_threshold=_search_threshold,
                )
            result = self._offline_indentation(
                gantry,
                target_z,
                _step_size,
                action_z,
                _force_limit,
                measure_with_return=measure_with_return,
            )
            result["detect_surface"] = detect_surface
            if surface_info is not None:
                result.update(surface_info)
                result["z_target_mm"] = target_z
            return result

        cur_x, cur_y = self._move_to_indentation_start(
            gantry,
            action_z=action_z,
        )
        baseline_avg, baseline_std = self._collect_indentation_baseline(
            baseline_samples=_baseline_samples,
        )

        surface_info = None
        if detect_surface:
            surface_z, trigger_force = self._run_surface_search(
                gantry,
                cur_x=cur_x,
                cur_y=cur_y,
                action_z=action_z,
                search_step=_search_step,
                force_threshold=_search_threshold,
                force_limit=_force_limit,
                max_travel=_search_max_travel,
                baseline_avg=baseline_avg,
            )
            # Re-anchor: the descent starts at the detected surface and
            # the deepest plane is surface-relative.
            action_z = surface_z
            target_z = surface_z + indentation_limit_height
            surface_info = self._surface_detection_info(
                surface_z=surface_z,
                trigger_force_n=trigger_force,
                search_step=_search_step,
                force_threshold=_search_threshold,
            )

        measurements, force_exceeded = self._run_indentation_descent(
            gantry,
            cur_x=cur_x,
            cur_y=cur_y,
            action_z=action_z,
            target_z=target_z,
            step_size=_step_size,
            force_limit=_force_limit,
            baseline_avg=baseline_avg,
        )

        if measure_with_return and measurements:
            self._run_indentation_return(
                gantry,
                cur_x=cur_x,
                cur_y=cur_y,
                action_z=action_z,
                target_z=target_z,
                step_size=_step_size,
                baseline_avg=baseline_avg,
                measurements=measurements,
            )

        result = self._indentation_result(
            measurements=measurements,
            baseline_avg=baseline_avg,
            baseline_std=baseline_std,
            force_exceeded=force_exceeded,
            measure_with_return=measure_with_return,
            step_size_mm=_step_size,
            z_target_mm=target_z,
            force_limit_n=_force_limit,
        )
        result["detect_surface"] = detect_surface
        if surface_info is not None:
            result.update(surface_info)
        return result

    def _resolve_indentation_settings(
        self,
        *,
        step_size: float | None,
        force_limit: float | None,
        baseline_samples: int | None,
    ) -> tuple[float, float, int]:
        resolved_step_size = step_size if step_size is not None else self._step_size
        resolved_force_limit = (
            force_limit if force_limit is not None else self._force_limit
        )
        resolved_baseline_samples = (
            baseline_samples
            if baseline_samples is not None
            else self._baseline_samples
        )
        return resolved_step_size, resolved_force_limit, resolved_baseline_samples

    def _resolve_surface_search_settings(
        self,
        *,
        surface_search_step: float | None,
        surface_force_threshold: float | None,
        surface_search_max_travel: float | None,
    ) -> tuple[float, float, float]:
        resolved_step = (
            surface_search_step
            if surface_search_step is not None
            else self._surface_search_step
        )
        resolved_threshold = (
            surface_force_threshold
            if surface_force_threshold is not None
            else self._surface_force_threshold
        )
        resolved_max_travel = (
            surface_search_max_travel
            if surface_search_max_travel is not None
            else self._surface_search_max_travel
        )
        return resolved_step, resolved_threshold, resolved_max_travel

    @staticmethod
    def _surface_detection_info(
        *,
        surface_z: float,
        trigger_force_n: float,
        search_step: float,
        force_threshold: float,
    ) -> dict:
        return {
            "surface_z_mm": surface_z,
            "surface_trigger_force_n": trigger_force_n,
            "surface_search_step_mm": search_step,
            "surface_force_threshold_n": force_threshold,
        }

    def _run_surface_search(
        self,
        gantry,
        *,
        cur_x: float,
        cur_y: float,
        action_z: float,
        search_step: float,
        force_threshold: float,
        force_limit: float,
        max_travel: float,
        baseline_avg: float,
    ) -> tuple[float, float]:
        """Coarse-step down from ``action_z`` until the surface is felt.

        A threshold crossing must hold on an immediate re-read at the same
        position before it's accepted as the surface; a crossing that
        doesn't repeat is treated as sensor noise and the coarse search
        continues downward from there (see #332 — a single noisy reading
        was enough to false-lock the old single-sample check).

        ``force_limit`` is the hard safety cutoff (same value the fine
        descent enforces): any single reading past it aborts immediately,
        independent of the noise-confirmation heuristic above — a real,
        sustained contact must never be re-classified as noise and stepped
        through just because the very next read happens to fall back under
        ``force_threshold``.

        Returns ``(surface_z, trigger_force_n)`` where ``surface_z`` is one
        search step above the trigger height (clamped to ``action_z``) and
        the gantry has been backed off to it. Search readings are not part
        of the measurement record. Raises :class:`ASMICommandError` when
        the corrected force never exceeds ``force_threshold`` on two
        consecutive reads before the search floor ``action_z - max_travel``,
        or immediately when a reading exceeds ``force_limit``.
        """
        floor_z = action_z - max_travel
        max_steps = _step_count_bound(action_z, floor_z, search_step)

        for _ in range(max_steps):
            coords = gantry.get_coordinates()
            current_z = coords["z"]
            if current_z <= floor_z:
                break

            next_z = max(current_z - search_step, floor_z)
            self._move_z(gantry, cur_x, cur_y, next_z)
            coords = gantry.get_coordinates()
            force = self.get_force_reading()
            corrected = force - baseline_avg
            if abs(corrected) > force_limit:
                raise ASMICommandError(
                    f"Force limit exceeded during surface search: "
                    f"{corrected:.4f} N > {force_limit:g} N at "
                    f"Z={coords['z']:.3f} mm. Aborting immediately — this "
                    "is the hard safety cutoff, independent of the "
                    "surface-detection noise confirmation."
                )
            if abs(corrected) > force_threshold:
                confirm_corrected = self.get_force_reading() - baseline_avg
                if abs(confirm_corrected) > force_limit:
                    raise ASMICommandError(
                        f"Force limit exceeded during surface search "
                        f"confirmation re-read: {confirm_corrected:.4f} N "
                        f"> {force_limit:g} N at Z={coords['z']:.3f} mm."
                    )
                if abs(confirm_corrected) <= force_threshold:
                    self.logger.info(
                        "Threshold crossing at Z=%.3f mm (dF=%.4f N) did "
                        "not hold on re-read (dF=%.4f N); treating as "
                        "noise and continuing search.",
                        coords["z"], corrected, confirm_corrected,
                    )
                    continue
                surface_z = min(coords["z"] + search_step, action_z)
                self.logger.info(
                    "Surface detected at Z=%.3f mm (dF=%.4f N, confirmed "
                    "%.4f N); backing off to %.3f mm",
                    coords["z"], corrected, confirm_corrected, surface_z,
                )
                self._move_z(gantry, cur_x, cur_y, surface_z)
                return surface_z, confirm_corrected

        raise ASMICommandError(
            f"Surface not detected within {max_travel:g} mm below the "
            f"measurement plane (corrected force never sustained "
            f"{force_threshold:g} N across two consecutive reads). Check "
            "surface_force_threshold, surface_search_max_travel, or the "
            "well's calibrated Z."
        )

    @staticmethod
    def _resolve_indentation_z_targets(
        *,
        well_z: float,
        measurement_height: float,
        indentation_limit_height: float,
    ) -> tuple[float, float]:
        action_z = well_z + measurement_height
        target_z = well_z + indentation_limit_height
        return action_z, target_z

    def _move_to_indentation_start(
        self,
        gantry,
        *,
        action_z: float,
    ) -> tuple[float, float]:
        coords = gantry.get_coordinates()
        cur_x, cur_y = coords["x"], coords["y"]
        self._move_z(gantry, cur_x, cur_y, action_z)
        return cur_x, cur_y

    def _collect_indentation_baseline(
        self,
        *,
        baseline_samples: int,
    ) -> tuple[float, float]:
        baseline_avg, baseline_std = self.get_baseline_force(
            samples=baseline_samples
        )
        self.logger.info(
            "Baseline: %.3f +/- %.3f N", baseline_avg, baseline_std
        )
        return baseline_avg, baseline_std

    @staticmethod
    def _measurement_point(
        *,
        z_mm: float,
        raw_force_n: float,
        corrected_force_n: float,
        direction: str,
    ) -> dict:
        return {
            "timestamp": time.time(),
            "z_mm": z_mm,
            "raw_force_n": raw_force_n,
            "corrected_force_n": corrected_force_n,
            "direction": direction,
        }

    def _record_force_at_z(
        self,
        *,
        z_mm: float,
        baseline_avg: float,
        direction: str,
    ) -> tuple[dict, float, float]:
        force = self.get_force_reading()
        corrected = force - baseline_avg
        return (
            self._measurement_point(
                z_mm=z_mm,
                raw_force_n=force,
                corrected_force_n=corrected,
                direction=direction,
            ),
            force,
            corrected,
        )

    def _run_indentation_descent(
        self,
        gantry,
        *,
        cur_x: float,
        cur_y: float,
        action_z: float,
        target_z: float,
        step_size: float,
        force_limit: float,
        baseline_avg: float,
    ) -> tuple[list[dict], bool]:
        measurements: list[dict] = []
        force_exceeded = False
        max_steps = _step_count_bound(action_z, target_z, step_size)

        # Deck-origin +Z-up: descent decreases Z toward the deck.
        for _ in range(max_steps):
            coords = gantry.get_coordinates()
            current_z = coords["z"]
            if current_z <= target_z:
                self.logger.info("Reached target_z %.3f mm", target_z)
                break

            next_z = max(current_z - step_size, target_z)
            self._move_z(gantry, cur_x, cur_y, next_z)
            coords = gantry.get_coordinates()
            point, force, corrected = self._record_force_at_z(
                z_mm=coords["z"],
                baseline_avg=baseline_avg,
                direction="down",
            )
            measurements.append(point)

            if len(measurements) % 10 == 0:
                self.logger.info(
                    "Step #%d: Z=%.3f mm, F=%.3f N, dF=%.3f N",
                    len(measurements), point["z_mm"], force, corrected,
                )

            if abs(corrected) > force_limit:
                self.logger.info(
                    "Force limit exceeded: %.3f N > %.1f N",
                    corrected, force_limit,
                )
                force_exceeded = True
                break
        else:
            self.logger.warning(
                "Descent hit iteration cap %d before reaching target_z %.3f",
                max_steps, target_z,
            )

        return measurements, force_exceeded

    def _run_indentation_return(
        self,
        gantry,
        *,
        cur_x: float,
        cur_y: float,
        action_z: float,
        target_z: float,
        step_size: float,
        baseline_avg: float,
        measurements: list[dict],
    ) -> None:
        self.logger.info(
            "Starting return sweep (%d descent samples collected)",
            len(measurements),
        )
        return_cap = _step_count_bound(action_z, target_z, step_size)

        # Deck-origin +Z-up: return increases Z back to the action plane.
        for _ in range(return_cap):
            coords = gantry.get_coordinates()
            current_z = coords["z"]
            if current_z >= action_z:
                break

            next_z = min(current_z + step_size, action_z)
            self._move_z(gantry, cur_x, cur_y, next_z)
            coords = gantry.get_coordinates()
            if coords["z"] <= current_z:
                self.logger.warning(
                    "Return sweep aborted: gantry Z did not retract (%.3f)",
                    current_z,
                )
                break

            point, _, _ = self._record_force_at_z(
                z_mm=coords["z"],
                baseline_avg=baseline_avg,
                direction="up",
            )
            measurements.append(point)
        else:
            self.logger.warning(
                "Return sweep hit iteration cap %d before reaching action_z %.3f",
                return_cap, action_z,
            )

    @staticmethod
    def _indentation_result(
        *,
        measurements: list[dict],
        baseline_avg: float,
        baseline_std: float,
        force_exceeded: bool,
        measure_with_return: bool,
        step_size_mm: float,
        z_target_mm: float,
        force_limit_n: float,
    ) -> dict:
        return {
            "measurements": measurements,
            "baseline_avg": baseline_avg,
            "baseline_std": baseline_std,
            "force_exceeded": force_exceeded,
            "data_points": len(measurements),
            "measure_with_return": measure_with_return,
            "step_size_mm": step_size_mm,
            "z_target_mm": z_target_mm,
            "force_limit_n": force_limit_n,
        }

    def _offline_indentation(
        self,
        gantry,
        target_z,
        step_size,
        action_z,
        force_limit,
        measure_with_return: bool = False,
    ) -> dict:
        """Fast offline indentation — no idle-wait, synthetic data.

        Deck-origin +Z-up convention: descent DECREASES z toward
        ``target_z`` (which is at or below ``action_z``); the optional
        return sweep walks z back UP to ``action_z``. Both parameters are
        absolute deck-frame Z, resolved by the public ``indentation()``
        method from the labware-relative offsets it receives.
        """
        coords = gantry.get_coordinates()
        cur_x, cur_y = coords["x"], coords["y"]
        gantry.move_to(cur_x, cur_y, action_z)

        # Integer step counting avoids float accumulation drift at loop boundaries.
        n_down = _step_count_bound(
            action_z, target_z, step_size,
        ) - _STEP_COUNT_SAFETY_MARGIN
        measurements = []
        for i in range(1, n_down + 1):
            z = max(action_z - i * step_size, target_z)
            gantry.move_to(cur_x, cur_y, z)
            measurements.append({
                "timestamp": time.time(),
                "z_mm": z,
                "raw_force_n": self._default_force,
                "corrected_force_n": 0.0,
                "direction": "down",
            })
            if z <= target_z:
                break

        if measure_with_return:
            z_bottom = measurements[-1]["z_mm"] if measurements else action_z
            n_up = _step_count_bound(action_z, z_bottom, step_size) - _STEP_COUNT_SAFETY_MARGIN
            for i in range(1, n_up + 1):
                z = min(z_bottom + i * step_size, action_z)
                gantry.move_to(cur_x, cur_y, z)
                measurements.append({
                    "timestamp": time.time(),
                    "z_mm": z,
                    "raw_force_n": self._default_force,
                    "corrected_force_n": 0.0,
                    "direction": "up",
                })
                if z >= action_z:
                    break

        return {
            "measurements": measurements,
            "baseline_avg": self._default_force,
            "baseline_std": 0.0,
            "force_exceeded": False,
            "data_points": len(measurements),
            "measure_with_return": measure_with_return,
            "step_size_mm": step_size,
            "z_target_mm": target_z,
            "force_limit_n": force_limit,
        }
