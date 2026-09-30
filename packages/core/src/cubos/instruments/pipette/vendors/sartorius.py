"""Driver for the Sartorius Picus 2 electronic pipette.

The Picus 2 owns its piston and its own closed-loop control, so commands are
volumes in microlitres rather than the millimetres of plunger travel
``vendors/opentrons.py`` sends. The ``PipetteInstrument`` members that report
plunger position are therefore driver bookkeeping here; each is noted where
it is defined.

Wire protocol: line-based JSON at 230400 8N1, CRLF-terminated, each frame
numbered so replies can be correlated::

    {"no":7,"data":"RUN_ASPIRATE 500 5"}\\r\\n

A reply is ``ACK``/``BEGIN``/payload/``END``, the envelope tokens tagged with
the frame number. The payload is a result token (``OK`` or an error code) for
commands that act, or bare untagged data lines for commands that report -- a
query such as ``GET_NOMINAL_VOLUME`` never sends ``OK``, so ``END`` is the
only terminator every command shares. Replies for different frames interleave,
and with ``AUTO 1`` the pipette also pushes unsolicited JSON button events.
"""

from __future__ import annotations

import itertools
import json
import math
import re
import threading
import time
from typing import Any, Optional

import serial

from cubos.instruments.pipette.interface import PipetteInstrument
from cubos.instruments.pipette.exceptions import (
    PipetteBatteryError,
    PipetteCommandError,
    PipetteConfigError,
    PipetteConnectionError,
    PipetteMotorControlError,
    PipetteTimeoutError,
)
from cubos.instruments.pipette.liquid_class import build_liquid_classes
from cubos.instruments.pipette.models import (
    AspirateResult,
    MixResult,
    PipetteConfig,
    PipetteStatus,
    PICUS2_MODELS,
)

_TERMINATOR = b"\r\n"
_BAUD_RATE = 230400

_ENVELOPE_TOKENS = frozenset({"ACK", "BEGIN", "END"})

_RESULT_OK = "OK"
_RESULT_CODES = frozenset(
    {
        "OK",
        "FULL",
        "SYNTAX_ERROR",
        "ERROR_PARSING",
        "UNKNOWN_COMMAND",
        "MISSING_PARAMETERS",
        "ERR_RANGE_PARAMETERS",
        "CHK_ERROR",
        "NOT_ALLOWED",
        "FAILED",
        "MOTOR_CONTROL_ABORTED",
    }
)

# Never auto-retried: the piston position after an abort is unknown, so
# recovery starts from a reconnect (which re-runs RUN_INIT), not a guess.
_ABORT_CODE = "MOTOR_CONTROL_ABORTED"

# Worst case is a full-stroke 10 mL dispense at the slowest speed setting.
_MOTION_TIMEOUT = 120.0
_QUERY_TIMEOUT = 5.0

# Motor control needs an on-screen confirmation, which is a softkey press and
# so can be satisfied over the wire; re-sent on a cadence until acknowledged.
_ARM_TIMEOUT = 30.0
_ARM_TAP_INTERVAL = 0.4
_CONFIRM_BUTTON = "TRIGGER_BUTTON_RIGHT"

# PipetteInstrument speed is a normalized 0-100 percentage; the Picus takes an
# integer 1..9. The interface default of 50.0 maps to the vendor default of 5.
_SPEED_MIN = 1
_SPEED_MAX = 9


def _speed_index(speed: float) -> int:
    """Map a normalized 0-100 speed onto the Picus 1..9 scale."""
    try:
        normalized = float(speed)
    except (TypeError, ValueError):
        normalized = 50.0
    if not math.isfinite(normalized):
        normalized = 50.0
    index = _SPEED_MIN + round(normalized / 100.0 * (_SPEED_MAX - _SPEED_MIN))
    return int(max(_SPEED_MIN, min(_SPEED_MAX, index)))


class SartoriusPicus2Pipette(PipetteInstrument):
    """Sartorius Picus 2 over USB serial.

    Pass ``offline=True`` for dry runs -- simulates piston state in memory.
    """

    CONFIG_FIELD_CHOICES = {
        "pipette_model": sorted(PICUS2_MODELS.keys()),
    }

    def __init__(
        self,
        pipette_model: str = "picus2_1ch_1000",
        port: str = "",
        baud_rate: int = _BAUD_RATE,
        command_timeout: float = 30.0,
        blowout_delay_ms: int = 3000,
        blowout_go_home: bool = True,
        min_battery_percent: float = 20.0,
        verify_model: bool = True,
        whole_microlitres_only: bool = False,
        name: Optional[str] = None,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
        depth: float = 0.0,
        offline: bool = False,
        liquid_classes: Optional[dict] = None,
        **kwargs,
    ):
        super().__init__(
            name=name, offset_x=offset_x, offset_y=offset_y,
            depth=depth, offline=offline,
        )
        if pipette_model not in PICUS2_MODELS:
            raise PipetteConfigError(
                f"Unknown Picus 2 model '{pipette_model}'. "
                f"Available: {', '.join(sorted(PICUS2_MODELS.keys()))}"
            )
        self._config: PipetteConfig = PICUS2_MODELS[pipette_model]
        self._liquid_classes = build_liquid_classes(liquid_classes)
        self._port = port
        self._baud_rate = baud_rate
        self._command_timeout = command_timeout
        self._blowout_delay_ms = int(blowout_delay_ms)
        self._blowout_go_home = bool(blowout_go_home)
        self._min_battery_percent = float(min_battery_percent)
        self._verify_model = bool(verify_model)
        self._whole_microlitres_only = bool(whole_microlitres_only)

        self._serial: Optional[serial.Serial] = None
        self._lock = threading.Lock()
        self._counter = itertools.count(1)
        self._has_tip = False
        self._attached_tip_extension = 0.0
        self._initialized = False
        self._motor_control_lost = False
        # No position readback exists, so loaded volume is tracked here.
        self._loaded_volume_ul = 0.0

    # ── Configuration ─────────────────────────────────────────────────────

    @property
    def config(self) -> PipetteConfig:
        return self._config

    @property
    def attached_tip_extension(self) -> float:
        return self._attached_tip_extension

    @property
    def effective_depth(self) -> float:
        return self.depth + self._attached_tip_extension

    @property
    def loaded_volume_ul(self) -> float:
        """Driver-tracked volume currently in the tip."""
        return self._loaded_volume_ul

    @property
    def _holding_liquid(self) -> bool:
        return self._loaded_volume_ul > 0.0

    def _warn_holding(self, action: str) -> None:
        self.logger.warning(
            "Pipette still holds %g uL from an interrupted run; %s so the "
            "piston is not reset. Dispense or blow out before the next "
            "initialisation.",
            self._loaded_volume_ul, action,
        )

    def set_attached_tip_extension(self, extension_mm: float) -> None:
        if (
            isinstance(extension_mm, bool)
            or not isinstance(extension_mm, (int, float))
            or not math.isfinite(float(extension_mm))
            or float(extension_mm) < 0.0
        ):
            raise PipetteConfigError(
                f"attached tip extension must be a non-negative finite number, "
                f"got {extension_mm!r}."
            )
        self._attached_tip_extension = float(extension_mm)

    def clear_attached_tip_extension(self) -> None:
        self._attached_tip_extension = 0.0

    # ── BaseInstrument interface ──────────────────────────────────────────

    def connect(self) -> None:
        if self._offline:
            self._initialized = True
            self._motor_control_lost = False
            self._loaded_volume_ul = 0.0
            self.logger.info("Picus 2 connected (offline)")
            return
        if self._holding_liquid and self._serial is not None and self._serial.is_open:
            # The session was kept open by ``disconnect`` to hold the liquid;
            # re-arm if control was lost, otherwise there is nothing to do.
            if self._motor_control_lost:
                self._motor_control_lost = False
                self._send("AUTO 1", timeout=_QUERY_TIMEOUT)
                self._arm_motor_control()
            self._warn_holding("reusing the open session")
            self._initialized = True
            return
        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.2,
                write_timeout=2.0,
            )
        except serial.SerialException as exc:
            raise PipetteConnectionError(
                f"Cannot open serial port {self._port}: {exc}"
            ) from exc

        # Cleared before the handshake, not after: `_send` refuses to talk
        # while it is set, and a reconnect is the recovery from an abort.
        self._motor_control_lost = False
        try:
            self._serial.reset_input_buffer()
            self._send("AUTO 1", timeout=_QUERY_TIMEOUT)
            self._arm_motor_control()
            if self._verify_model:
                self._verify_attached_model()
            self._check_battery()
            if self._holding_liquid:
                self._warn_holding("skipping RUN_INIT on reconnect")
            else:
                self._send("RUN_INIT", timeout=_MOTION_TIMEOUT)
                self._loaded_volume_ul = 0.0
        except Exception:
            self._close_serial()
            raise

        self._initialized = True
        self.logger.info("Connected to %s on %s", self._config.name, self._port)

    def disconnect(self) -> None:
        """Release the pipette, unless it still holds liquid.

        Handing motor control back (``ENABLE_MOTOR_CONTROL 0``) and the
        ``RUN_INIT`` of the next connect both drive the piston through its
        reference sweep, which expels whatever is in the tip. A run that
        fails between an aspirate and its dispense would otherwise dump the
        liquid wherever the tool happens to be. While ``loaded_volume_ul``
        is positive the session is kept open and armed so the operator can
        dispense or blow out deliberately (a one-step ``blowout`` protocol
        works); the next ``disconnect`` after that releases as usual.
        """
        if self._offline:
            self._initialized = False
            self.logger.info("Picus 2 disconnected (offline)")
            return
        if self._serial is not None and self._serial.is_open:
            if self._holding_liquid:
                self._warn_holding("keeping motor control and the port open")
                return
            try:
                self._send("ENABLE_MOTOR_CONTROL 0", timeout=_QUERY_TIMEOUT)
            except (PipetteCommandError, PipetteTimeoutError,
                    PipetteMotorControlError, serial.SerialException) as exc:
                # Releasing control is courtesy; the port still has to close.
                self.logger.debug("Ignored error releasing motor control: %s", exc)
        self._close_serial()
        self._initialized = False
        self.logger.info("Disconnected from pipette")

    def health_check(self) -> bool:
        if self._offline:
            return True
        if self._serial is None or not self._serial.is_open:
            return False
        if self._motor_control_lost:
            return False
        try:
            self._send("GET_VERSION", timeout=_QUERY_TIMEOUT)
            return True
        except (PipetteCommandError, PipetteTimeoutError, PipetteMotorControlError):
            return False

    def warm_up(self) -> None:
        self.home()

    # ── Pipette-specific commands ─────────────────────────────────────────

    def home(self) -> None:
        """Return the piston home.

        ``RUN_INIT`` establishes the reference once per session; afterwards
        ``HOME`` is a plain move that skips the slower initialization sweep.
        """
        if self._offline:
            self._initialized = True
            self._loaded_volume_ul = 0.0
            return
        if self._holding_liquid:
            raise PipetteCommandError(
                f"Refusing to home: the pipette still holds "
                f"{self._loaded_volume_ul:g} uL. Dispense or blow out first."
            )
        self._send(
            "RUN_INIT" if not self._initialized else "HOME",
            timeout=_MOTION_TIMEOUT,
        )
        self._initialized = True
        self._loaded_volume_ul = 0.0

    def prime(self, speed: float = 50.0) -> None:
        """No-op: the Picus owns its piston reference and takes absolute
        volumes, so there is nothing to pre-position. ``is_primed`` therefore
        means "initialized" on this vendor.
        """
        if not self._initialized:
            self.home()

    def aspirate(self, volume_ul: float, speed: float = 50.0) -> AspirateResult:
        commanded = self._quantize(volume_ul)
        # Successive aspirates can each be in range and still overfill the
        # tip. Hardware answers FULL; offline and mock runs need this check.
        if self._loaded_volume_ul + commanded > self._config.max_volume:
            raise PipetteCommandError(
                f"Aspirating {commanded:g} uL would put "
                f"{self._loaded_volume_ul + commanded:g} uL in a "
                f"{self._config.max_volume:g} uL pipette "
                f"({self._loaded_volume_ul:g} uL already loaded)."
            )
        if self._offline:
            self._loaded_volume_ul += commanded
            return self._result(commanded)
        self._send(
            f"RUN_ASPIRATE {self._format_volume(commanded)} {_speed_index(speed)}",
            timeout=_MOTION_TIMEOUT,
        )
        self._loaded_volume_ul += commanded
        return self._result(commanded)

    def dispense(self, volume_ul: float, speed: float = 50.0) -> AspirateResult:
        commanded = self._quantize(volume_ul)
        if self._offline:
            self._loaded_volume_ul = max(0.0, self._loaded_volume_ul - commanded)
            return self._result(commanded)
        self._send(
            f"RUN_DISPENSE {self._format_volume(commanded)} {_speed_index(speed)}",
            timeout=_MOTION_TIMEOUT,
        )
        self._loaded_volume_ul = max(0.0, self._loaded_volume_ul - commanded)
        return self._result(commanded)

    def blowout(self, speed: float = 50.0) -> None:
        """Expel residual liquid using the instrument's own blow-out stroke."""
        if self._offline:
            self._loaded_volume_ul = 0.0
            return
        self._send(
            f"BLOW_OUT {1 if self._blowout_go_home else 0} "
            f"{_speed_index(speed)} {self._blowout_delay_ms}",
            timeout=_MOTION_TIMEOUT,
        )
        self._loaded_volume_ul = 0.0

    def mix(
        self,
        volume_ul: float,
        cycles: int = 3,
        speed: float = 50.0,
        *,
        gantry: Any,
        position: tuple[float, float, float],
        lift_mm: float = 1.0,
    ) -> MixResult:
        """Two-height mix; a failure part-way leaves liquid in the tip,
        which the driver tries to return before re-raising."""
        try:
            return super().mix(
                volume_ul, cycles, speed,
                gantry=gantry, position=position, lift_mm=lift_mm,
            )
        except Exception:
            self._recover_interrupted_mix(speed)
            raise

    def pick_up_tip(self, speed: float = 50.0) -> None:
        """Record that a tip was seated.

        Pickup is gantry motion: the toolhead presses the cone onto the tip.
        There is no pipette-side command and no tip sensor, so ``has_tip`` is
        bookkeeping.
        """
        if not self._offline and not self._initialized:
            self.home()
        self._has_tip = True

    def drop_tip(self, speed: float = 50.0) -> None:
        """Eject the tip with the instrument's electronic ejector."""
        if not self._offline:
            self._send("TIP_EJECT", timeout=_MOTION_TIMEOUT)
        self._has_tip = False
        self.clear_attached_tip_extension()
        self._loaded_volume_ul = 0.0

    def get_status(self) -> PipetteStatus:
        """Return current state.

        ``position_mm`` is always 0.0 -- the Picus exposes no piston-position
        query, so read ``loaded_volume_ul`` instead. ``is_homed``/``is_primed``
        are driver state, re-established by the ``RUN_INIT`` in ``connect``.
        """
        return PipetteStatus(
            is_homed=self._initialized,
            position_mm=0.0,
            max_volume=self._config.max_volume,
            has_tip=self._has_tip,
            is_primed=self._initialized,
            battery_percent=None if self._offline else self._read_battery(),
        )

    # ── Identity / telemetry ──────────────────────────────────────────────

    def get_model(self) -> str:
        return "" if self._offline else (self._send("GET_MODEL", timeout=_QUERY_TIMEOUT) or "")

    def get_serial_number(self) -> str:
        return "" if self._offline else (self._send("GET_SERIAL", timeout=_QUERY_TIMEOUT) or "")

    def get_nominal_volume(self) -> str:
        return (
            str(self._config.max_volume)
            if self._offline
            else (self._send("GET_NOMINAL_VOLUME", timeout=_QUERY_TIMEOUT) or "")
        )

    # ── Private helpers ───────────────────────────────────────────────────

    def _result(self, commanded: float) -> AspirateResult:
        return AspirateResult(
            success=True,
            volume_ul=commanded,
            position_mm=0.0,
            loaded_volume_ul=self._loaded_volume_ul,
        )

    def _quantize(self, volume_ul: float) -> float:
        """Round *volume_ul* to the model's settable increment and bounds-check.

        The returned figure is what gets commanded, and is reported back on
        ``AspirateResult.volume_ul`` so callers can see the rounding.
        """
        if (
            isinstance(volume_ul, bool)
            or not isinstance(volume_ul, (int, float))
            or not math.isfinite(float(volume_ul))
        ):
            raise PipetteCommandError(f"Volume {volume_ul!r} uL is not a finite number")
        requested = float(volume_ul)
        increment = self._config.volume_increment_ul
        quantized = (
            round(requested / increment) * increment if increment > 0 else requested
        )
        # Float dust: 0.1 * 3 must not command 0.30000000000000004.
        quantized = round(quantized, 6)
        if not (self._config.min_volume <= quantized <= self._config.max_volume):
            raise PipetteCommandError(
                f"Volume {volume_ul!r} uL (quantized to {quantized} uL) is outside "
                f"{self._config.name} range {self._config.min_volume}-"
                f"{self._config.max_volume} uL"
            )
        return quantized

    def _format_volume(self, volume_ul: float) -> str:
        """Render a volume for the wire at the model's resolution.

        Models whose increment is a whole microlitre are sent as integers;
        ``whole_microlitres_only`` forces that for the rest, should a device
        turn out to reject fractional microlitres.
        """
        increment = self._config.volume_increment_ul
        if self._whole_microlitres_only or increment >= 1.0 or increment <= 0:
            return str(int(round(volume_ul)))
        decimals = max(0, -int(math.floor(math.log10(increment))))
        return f"{volume_ul:.{decimals}f}"

    def _recover_interrupted_mix(self, speed: float) -> None:
        """Best-effort return of liquid left in the tip by a failed mix."""
        if self._loaded_volume_ul <= 0:
            return
        try:
            self.blowout(speed)
        except Exception as exc:  # noqa: BLE001 - the original error must win
            self.logger.warning(
                "Mix failed and blow-out recovery also failed (%s); "
                "tip may still hold liquid", exc,
            )

    def _arm_motor_control(self, mode: int = 2) -> None:
        """Request host motor control and satisfy the on-screen confirmation.

        No motion command is accepted until a human -- or a softkey frame
        standing in for one -- confirms on the device.
        """
        deadline = time.monotonic() + _ARM_TIMEOUT
        # Same lock as every other exchange: a concurrent reader would eat the
        # reply this loop waits for and turn a confirmation into a timeout.
        with self._lock:
            frame_no = next(self._counter)
            self._write(
                self._frame(frame_no, data=f"ENABLE_MOTOR_CONTROL {int(mode)}")
            )
            last_tap = 0.0
            while True:
                now = time.monotonic()
                if now >= deadline:
                    raise PipetteConnectionError(
                        f"Timed out after {_ARM_TIMEOUT:.0f}s enabling motor control "
                        f"on {self._port}. Confirm remote control on the pipette "
                        f"screen (right softkey) and reconnect."
                    )
                if now - last_tap >= _ARM_TAP_INTERVAL:
                    self._write(
                        self._frame(next(self._counter), button=_CONFIRM_BUTTON)
                    )
                    last_tap = now
                line = self._read_line()
                if line is None:
                    continue
                kind, reply_no, value = self._parse_line(line)
                if kind == "result" and reply_no == frame_no:
                    if value != _RESULT_OK:
                        raise PipetteConnectionError(
                            f"Pipette refused motor control: {value}"
                        )
                    return

    def _verify_attached_model(self) -> None:
        """Fail closed when the physical device is not the configured model.

        A 1000 uL config driving a 10 uL pipette would over-aspirate by 100x
        on the first command.
        """
        nominal = self._send("GET_NOMINAL_VOLUME", timeout=_QUERY_TIMEOUT)
        if not nominal:
            self.logger.warning(
                "Pipette did not report a nominal volume; skipping model check"
            )
            return
        reported = _first_number(nominal)
        if reported is None:
            self.logger.warning(
                "Could not parse nominal volume %r; skipping model check", nominal,
            )
            return
        if abs(reported - self._config.max_volume) > 0.5:
            raise PipetteConnectionError(
                f"Configured model {self._config.name} expects a "
                f"{self._config.max_volume:g} uL pipette, but the device on "
                f"{self._port} reports {reported:g} uL. Fix `pipette_model` in "
                f"the gantry YAML, or set verify_model: false to override."
            )

    def _read_battery(self) -> Optional[float]:
        try:
            reported = self._send("GET_BATTERY_LEVEL", timeout=_QUERY_TIMEOUT)
        except (PipetteCommandError, PipetteTimeoutError, PipetteMotorControlError):
            return None
        return None if not reported else _first_number(reported)

    def _check_battery(self) -> None:
        """Refuse to start below the configured charge floor, so an overnight
        campaign cannot die mid-transfer on a flat battery.
        """
        level = self._read_battery()
        if level is None:
            self.logger.warning("Pipette did not report a battery level")
            return
        if level < self._min_battery_percent:
            raise PipetteBatteryError(
                f"Pipette battery is at {level:g}%, below the "
                f"{self._min_battery_percent:g}% minimum. Charge it over USB "
                f"before starting a run."
            )
        self.logger.info("Pipette battery at %g%%", level)

    @staticmethod
    def _frame(no: int, *, data: str | None = None, button: str | None = None) -> bytes:
        payload: dict[str, Any] = {"no": no}
        if data is not None:
            payload["data"] = data
        if button is not None:
            payload["button"] = button
        return json.dumps(payload, separators=(",", ":")).encode("ascii") + _TERMINATOR

    @staticmethod
    def _parse_line(line: str) -> tuple[str, Optional[int], Optional[str]]:
        """Sort one received line into ``(kind, sequence_no, value)``.

        Kinds: ``ack``/``begin``/``end`` and ``result`` carry a sequence
        number; ``data`` is a bare payload line and carries none; ``ignore``
        is a blank line or an unsolicited button event.
        """
        stripped = line.strip()
        if not stripped:
            return ("ignore", None, None)
        if stripped.startswith("{"):
            # Async notification, e.g. {"button":"RIGHT_PRESSED"} -- never a
            # command's output, however mid-command it arrives.
            return ("ignore", None, None)
        parts = stripped.split()
        head = parts[0]
        reply_no: Optional[int] = None
        if len(parts) >= 2:
            try:
                reply_no = int(parts[1])
            except ValueError:
                reply_no = None
        if head in _ENVELOPE_TOKENS and reply_no is not None:
            return (head.lower(), reply_no, None)
        if head in _RESULT_CODES and reply_no is not None:
            return ("result", reply_no, head)
        return ("data", None, stripped)

    def _write(self, frame: bytes) -> None:
        if self._serial is None:
            raise PipetteCommandError("Not connected to pipette")
        try:
            self._serial.write(frame)
            self._serial.flush()
        except serial.SerialException as exc:
            raise PipetteConnectionError(f"Serial write failed: {exc}") from exc

    def _read_line(self) -> Optional[str]:
        if self._serial is None:
            raise PipetteCommandError("Not connected to pipette")
        try:
            raw = self._serial.readline()
        except serial.SerialException as exc:
            raise PipetteConnectionError(f"Serial read failed: {exc}") from exc
        if not raw:
            return None
        return raw.decode("ascii", errors="replace")

    def _send(self, data: str, *, timeout: Optional[float] = None) -> Optional[str]:
        """Send one command and wait for its terminal result token.

        Returns the joined data lines, or ``None`` when the command produced
        none. Raises on any non-OK result.
        """
        if self._serial is None or not self._serial.is_open:
            raise PipetteCommandError("Not connected to pipette")
        if self._motor_control_lost:
            raise PipetteMotorControlError(
                "Motor control was aborted; reconnect before sending further "
                "commands (the piston position is unknown)."
            )
        wait = self._command_timeout if timeout is None else timeout
        responses: list[str] = []
        # Open BEGIN scopes. Data lines carry no sequence number, so one
        # belongs to whichever scope is innermost.
        scopes: list[int] = []

        with self._lock:
            frame_no = next(self._counter)
            self._write(self._frame(frame_no, data=data))
            deadline = time.monotonic() + wait
            while True:
                if time.monotonic() >= deadline:
                    raise PipetteTimeoutError(
                        f"Timed out ({wait}s) waiting for a reply to {data!r}"
                    )
                line = self._read_line()
                if line is None:
                    continue
                kind, reply_no, value = self._parse_line(line)
                if kind == "begin":
                    scopes.append(reply_no)
                elif kind == "end":
                    if reply_no in scopes:
                        scopes.remove(reply_no)
                    # Only terminator every command sends: a query replies
                    # with data lines and no result code at all.
                    if reply_no == frame_no:
                        return "\n".join(responses) if responses else None
                elif kind == "result" and reply_no == frame_no:
                    if value == _ABORT_CODE:
                        self._motor_control_lost = True
                        raise PipetteMotorControlError(
                            f"Pipette aborted motor control during {data!r}. The "
                            f"piston position is now unknown; reconnect to "
                            f"re-initialize before any further liquid handling."
                        )
                    if value != _RESULT_OK:
                        raise PipetteCommandError(f"{data!r} failed: {value}")
                elif (
                    kind == "data"
                    and value is not None
                    and scopes[-1:] == [frame_no]
                ):
                    responses.append(value)

    def _close_serial(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except serial.SerialException:
                pass
            self._serial = None


# Tolerates a thousands separator and a trailing unit: "1,000 uL" and
# "battery 87 %". Splitting on whitespace would read "1,000" as 1 and make the
# connect-time model check reject a correct 1000 uL pipette.
_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _first_number(text: str) -> Optional[float]:
    """Pull the first numeric value out of an instrument reply."""
    match = _NUMBER_RE.search(text)
    if match is None:
        return None
    try:
        return float(match.group().replace(",", ""))
    except ValueError:
        return None
