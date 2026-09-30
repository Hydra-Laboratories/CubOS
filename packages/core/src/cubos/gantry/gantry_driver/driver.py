"""
This module contains the Mill class, which is used to control a GRBL CNC machine.
The Mill class is used by higher-level wrappers to move instruments (pipette, electrode, etc.)
to the specified coordinates.

The Mill class contains methods to connect to the mill, execute GRBL commands,
stop/reset/home the controller, read status/settings, and move to already
resolved coordinates.
"""

# pylint: disable=line-too-long

# standard libraries
import logging
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

# third-party libraries
import serial
import serial.tools.list_ports

from .exceptions import (
    CommandExecutionError,
    LocationNotFound,
    MillConnectionError,
    StatusReturnError,
)
from ..coordinates import Coordinates

# local libraries
from .logger import set_up_command_logger, set_up_mill_logger

# Constants
DEFAULT_FEED_RATE = 3000
HOMING_TIMEOUT = 90
ERROR_22_MAX_RETRIES = 2

def looks_like_connection_loss(value: Exception | str) -> bool:
    """Return true when an error looks like the serial link itself died.

    Hardware validation on the CubXL showed the controller dropping off the
    USB bus at a hard-limit trip (supply-rail glitch reboots the board and
    re-enumerates the USB bridge). The stale file descriptor then times out
    or raises OS-level errors on every exchange.
    """
    message = str(value).lower()
    return any(
        token in message
        for token in (
            "timed out",
            "device not configured",
            "errno 6",
            "input/output error",
            "bad file descriptor",
            "device reports readiness to read but returned no data",
            "write failed",
            "no initial grbl status",
        )
    )


# Compile regex patterns for extracting coordinates from the mill status
wpos_pattern = re.compile(r"WPos:([\d.-]+),([\d.-]+),([\d.-]+)")
mpos_pattern = re.compile(r"MPos:([\d.-]+),([\d.-]+),([\d.-]+)")
wco_pattern = re.compile(r"WCO:([\d.-]+),([\d.-]+),([\d.-]+)")

class Mill:
    """
    Set up the mill connection and pass commands, including special commands.

    Attributes:
        config (dict): The configuration of the mill.
        ser_mill (serial.Serial): The serial connection to the mill.
        homed (bool): True if the mill is homed, False otherwise.
        active_connection (bool): True if the connection to the mill is active, False otherwise.
        logger_location (Path): The location of the logger.
        logger (Logger): The logger for the mill.
    """

    def __init__(self, port: Optional[str] = None, log_dir: Path | None = None):
        self.logger_location = log_dir
        self.logger = set_up_mill_logger(log_dir)
        self.port = port
        self.config = {}
        self.default_feed_rate = DEFAULT_FEED_RATE
        self._init_state()
        self.ser_mill: serial.Serial = None
        self._write_lock = threading.Lock()

    def _write_serial(self, data: bytes) -> None:
        """Write bytes to the controller under the write lock.

        Realtime characters (``?``, ``!``, ``~``, jog cancel ``0x85``, soft
        reset ``0x18``) are sent from threads that deliberately do not hold
        the session operation lock, so two threads can reach the serial port
        at once. GRBL itself strips realtime characters out of the stream
        wherever they appear — even mid-line — but pyserial makes no
        thread-safety guarantee for concurrent ``write()`` calls. This lock
        makes each write atomic at the port. It only serializes the write
        syscall, never a command/response cycle, so realtime characters
        still bypass waiting on in-flight commands.
        """
        with self._write_lock:
            self.ser_mill.write(data)

    def _init_state(self):
        """Initialize state attributes for a fresh, unconnected mill."""
        self.config = {}
        self.homed = False
        self.auto_home = False
        self.active_connection = False
        self.command_logger = set_up_command_logger(self.logger_location)
        self._wco: Optional[Coordinates] = None
        self.last_status: str = ""

    def _locate_over_serial(self, port: Optional[str] = None) -> Tuple[serial.Serial, str]:
        """
        Locate the mill over serial.

        Start with the port provided and attempt to connect and then query for the mill settings ($$) to verify the connection.
        If the response does not begin with a $, scan for connected devices and attempt to connect to each one until a valid response is received.
        """
        ser_mill = serial.Serial()
        baudrates = [115200, 9600]
        timeout = 2

        priority_port = port

        ports = []
        if priority_port:
            ports.append(priority_port)

        for p in self._get_available_ports():
             if p.device != priority_port:
                 ports.append(p.device)

        if not ports:
            self.logger.error("No serial ports found to connect to.")
            raise MillConnectionError("No serial ports found. Checked ttyUSB and usbmodem.")

        found = False
        found_on = None
        max_scan_attempts = 3
        scan_attempt = 0
        while not found:
            scan_attempt += 1
            if scan_attempt > max_scan_attempts:
                raise MillConnectionError(
                    f"Could not find GRBL device after {max_scan_attempts} scan attempts"
                )
            for port in ports:
                if not port:
                    continue

                for baudrate in baudrates:
                    try:
                        self.logger.info("Attempting connection to port: %s at %s baud", port, baudrate)
                        ser_mill = serial.Serial(
                            port=port,
                            baudrate=baudrate,
                            timeout=timeout,
                        )

                        if self._verify_connection(ser_mill):
                             self.logger.info(f"Connected successfully to {port} at {baudrate}")
                             found = True
                             found_on = port
                             break
                        else:
                             self.logger.warning(f"No valid GRBL response from {port} at {baudrate}")
                             ser_mill.close()

                    except Exception as e:
                        self.logger.error(f"Error checking {port} at {baudrate}: {e}")
                        if ser_mill.is_open:
                            ser_mill.close()

                if found:
                    break

        return ser_mill, found_on

    def _get_available_ports(self):
        """List all available serial ports."""
        if os.name == "posix":
            ports = list(serial.tools.list_ports.grep("ttyUSB|usbmodem|usbserial"))
        elif os.name == "nt":
            ports = list(serial.tools.list_ports.grep("COM"))
        else:
            raise OSError("Unsupported OS")
        return ports

    def _verify_connection(self, ser_mill: serial.Serial) -> bool:
        """Verify that the connected device is a GRBL mill."""
        ser_mill.flushInput()
        ser_mill.flushOutput()
        time.sleep(0.1)

        if not ser_mill.is_open:
            ser_mill.open()
            time.sleep(0.5)

        if not ser_mill.is_open:
             return False

        self.logger.info("Querying the mill for status")

        # Wake the controller without forcing a soft reset. Ctrl-X clears
        # transient state, but on machines with homing lock enabled it also
        # drops GRBL back into Alarm until the operator homes again.
        ser_mill.write(b"\r\n")
        time.sleep(0.1)
        ser_mill.flushInput()

        ser_mill.write(b"?")
        time.sleep(0.1)

        ser_mill.write(b"$$\n")
        time.sleep(0.2)

        statuses = ser_mill.readlines()
        self.logger.info(f"Raw response: {statuses}")

        for line in statuses:
            decoded = line.decode(errors='ignore').rstrip()
            if (
                "Grbl" in decoded
                or decoded == "ok"
                or decoded.startswith("$")
                or decoded.startswith("<")
            ):
                return True
        return False

    def connect(
        self,
        port: Optional[str] = None,
        baudrate=115200,
        timeout=3,
    ) -> serial.Serial:
        """Open the serial connection and initialize the controller."""
        self._init_state()
        self.ser_mill = None
        try:
            ser_mill, port_name = self._locate_over_serial(port)

            if ser_mill and ser_mill.is_open:
                self.logger.info("Reusing open serial connection from detection")
                self.ser_mill = ser_mill
            else:
                self.logger.info("Opening new serial connection to mill...")
                self.ser_mill = serial.Serial(
                    port=port_name,
                    baudrate=baudrate,
                    timeout=timeout,
                )
                time.sleep(2)

            if not self.ser_mill.is_open:
                self.ser_mill.open()
                time.sleep(2)

            if self.ser_mill.is_open:
                self.logger.info("Serial connection to mill opened successfully")
                self.active_connection = True
            else:
                self.logger.error("Serial connection to mill failed to open")
                raise MillConnectionError("Error opening serial connection to mill")

            self.logger.info("Mill connected: %s", self.ser_mill.is_open)

        except Exception as exep:
            self.logger.error("Error connecting to the mill: %s", str(exep))
            raise MillConnectionError("Error connecting to the mill") from exep

        # Quick alarm check before sending any commands — GRBL rejects
        # everything except $X and $H while in alarm state.
        initial_status = ""
        for _ in range(3):
            self._write_serial(b"?")
            time.sleep(0.2)
            initial_status = self._read_serial()
            if initial_status:
                break
        if not initial_status:
            raise MillConnectionError(
                "No initial GRBL status response; cannot enforce WPos mode"
            )
        if initial_status and "alarm" in initial_status.lower():
            self.logger.warning(
                "Mill is in alarm state — skipping config/setup. "
                "Unlock ($X) or home ($H) to clear. Status: %s",
                initial_status,
            )
            return self.ser_mill

        self.read_config()
        self.clear_buffers()
        self.enforce_wpos_mode()
        self.set_feed_rate(self.default_feed_rate)
        self.seed_wco()
        return self.ser_mill

    def disconnect(self):
        """Close the serial connection to the mill."""
        self.logger.info("Disconnecting from the mill")

        if self.ser_mill:
            self.ser_mill.close()
            time.sleep(2)
            if self.ser_mill.is_open:
                self.logger.error("Failed to close the serial connection to the mill")
                raise MillConnectionError("Error closing serial connection to mill")
            else:
                self.logger.info("Serial connection to mill closed successfully")
                self._init_state()
                self.ser_mill = None
        else:
             self.logger.info("Serial connection was already closed or never opened.")
             self._init_state()
        return

    def connected_port(self) -> str | None:
        """Return the active serial port name, if connected."""
        if self.ser_mill is None:
            return self.port
        port = getattr(self.ser_mill, "port", None)
        return str(port) if port else self.port

    def set_read_timeout(self, timeout: float) -> None:
        """Set the serial read timeout when a connection exists."""
        if self.ser_mill is not None:
            self.ser_mill.timeout = timeout

    def get_read_timeout(self) -> float | None:
        """Return the serial read timeout when a connection exists."""
        if self.ser_mill is None:
            return None
        return self.ser_mill.timeout

    def read_config(self):
        """Read the live mill config from the connected controller."""
        self._require_open_serial()

        self.logger.info("Reading mill config")
        mill_config = self.read_grbl_settings()
        self.config = mill_config
        self.logger.debug("Mill config: %s", mill_config)
        return mill_config

    def _collect_grbl_settings_response(self) -> dict:
        """Drain serial until ``ok`` arrives, then parse the $$ block."""
        deadline = time.time() + 5
        self._require_open_serial()
        lines = [self.ser_mill.readline().decode(encoding="ascii", errors="replace").rstrip()]
        while lines[-1] != "ok":
            if time.time() > deadline:
                raise StatusReturnError("Timed out waiting for GRBL settings response")
            if lines[-1].lower().startswith(("error", "alarm")):
                raise StatusReturnError(
                    f"Error in settings response: {lines[-1]}"
                )
            lines.append(self.ser_mill.readline().decode(encoding="ascii", errors="replace").rstrip())
        self.logger.debug("Returned %s", lines[:-1])
        return self._parse_grbl_settings_response(lines[:-1])

    def _parse_grbl_settings_response(self, response_lines: list[str]) -> dict:
        """Parse a GRBL $$ response into a settings dictionary."""
        settings_dict = {}
        self.logger.info("Parsing settings from: %s", response_lines)
        for setting in response_lines:
            if not setting:
                continue
            if setting.startswith("[MSG") or "Grbl" in setting:
                continue
            if "=" not in setting:
                self.logger.warning("Skipping non-setting line: %s", setting)
                continue
            try:
                key, value = setting.split("=", 1)
                if "(" in value:
                    value = value.split("(", 1)[0]
                settings_dict[key.strip()] = value.strip()
            except ValueError:
                self.logger.error("Failed to parse setting line: %s", setting)
                continue
        return settings_dict

    def execute_command(self, command: str, suppress_errors: bool = False):
        """Encodes and sends commands to the mill and returns the response.

        Errors are always logged — ``suppress_errors=True`` only demotes the
        log level so callers that expect transient failures (e.g. probing
        during connect) don't spam ERROR, but the forensic trail is preserved.
        ``error:22`` (undefined feed rate) triggers a bounded retry that
        re-sets the feed rate and re-sends the command; persistent ``error:22``
        raises after :data:`ERROR_22_MAX_RETRIES` attempts.
        """
        try:
            self._require_open_serial()
            self.logger.debug("Command sent: %s", command)
            self.command_logger.debug("%s", command)

            if command == "$$":
                self._write_serial(str(command).encode(encoding="ascii") + b"\n")
                return self._collect_grbl_settings_response()

            for attempt in range(ERROR_22_MAX_RETRIES + 1):
                self._write_serial(str(command).encode(encoding="ascii") + b"\n")
                mill_response = self._read_serial().lower()
                if not command.startswith("$"):
                    mill_response = self._wait_until_idle(
                        mill_response, suppress_errors=suppress_errors
                    )
                self.logger.debug("Returned %s", mill_response)

                if not re.search(r"(error|alarm)", mill_response):
                    break

                if re.search(r"error:22", mill_response) and attempt < ERROR_22_MAX_RETRIES:
                    self.logger.warning(
                        "error:22 from GRBL (attempt %d/%d); restoring feed rate and retrying: %s",
                        attempt + 1,
                        ERROR_22_MAX_RETRIES,
                        mill_response,
                    )
                    self.set_feed_rate(self.default_feed_rate)
                    continue

                level = logging.WARNING if suppress_errors else logging.ERROR
                self.logger.log(
                    level, "GRBL error in response to %s: %s", command, mill_response
                )
                raise StatusReturnError(f"Error in status: {mill_response}")

        except Exception as exep:
            level = logging.WARNING if suppress_errors else logging.ERROR
            self.logger.log(
                level, "Error executing command %s: %s", command, str(exep)
            )
            raise CommandExecutionError(
                f"Error executing command {command}: {str(exep)}"
            ) from exep

        return mill_response

    def stop(self):
        """Send GRBL feed hold and verify the controller entered Hold."""
        self.feed_hold_realtime()
        self._write_serial(b"?")
        time.sleep(0.05)
        status = self._read_serial()
        if "hold" not in status.lower():
            raise StatusReturnError(f"Feed hold was not acknowledged: {status}")

    def feed_hold_realtime(self) -> None:
        """Send GRBL realtime feed hold without line protocol or reads."""
        self._require_open_serial()
        self._write_serial(b"!")

    def resume(self) -> None:
        """Resume a feed-held GRBL controller with realtime cycle start."""
        self._require_open_serial()
        self._write_serial(b"~")

    def jog(self, x: float = 0, y: float = 0, z: float = 0,
            feed_rate: float = DEFAULT_FEED_RATE) -> None:
        """Send a GRBL jog command ($J=) for immediate, cancellable movement.

        Unlike G-code moves, jog commands are non-blocking and can be
        cancelled instantly with a jog-cancel (0x85).

        Args:
            x: Relative X distance.
            y: Relative Y distance.
            z: Relative Z distance.
            feed_rate: Feed rate in mm/min.
        """
        self._require_open_serial()
        parts = []
        if x != 0:
            parts.append(f"X{x:.3f}")
        if y != 0:
            parts.append(f"Y{y:.3f}")
        if z != 0:
            parts.append(f"Z{z:.3f}")
        if not parts:
            return
        cmd = f"$J=G91 {' '.join(parts)} F{feed_rate}"
        self.logger.debug("Jog command: %s", cmd)
        self._write_serial((cmd + "\n").encode("ascii"))
        response = self._read_serial().lower()
        if (
            "error" in response
            or "alarm" in response
            or "check limits" in response
        ):
            # error:8 = "not idle" (planner buffer full) — safe to ignore
            if "error:8" in response:
                self.logger.debug("Jog buffer full, skipping")
                return
            self.logger.error("Jog error: %s", response)
            raise CommandExecutionError(f"Jog failed: {response}")

    def jog_cancel(self) -> None:
        """Cancel any in-progress jog motion immediately."""
        self._require_open_serial()
        self._write_serial(b"\x85")

    def unlock(self):
        """Unlock the mill by sending $X directly over serial."""
        self._require_open_serial()
        self.logger.info("Sending unlock ($X)")
        self._write_serial(b"$X\n")
        deadline = time.time() + 2
        while time.time() < deadline:
            line = self.ser_mill.readline().decode("ascii", errors="replace").strip()
            if not line:
                time.sleep(0.05)
                continue
            self.logger.debug("Unlock response: %s", line)
            lowered = line.lower()
            if lowered == "ok":
                return
            if lowered.startswith(("error", "alarm")):
                raise CommandExecutionError(f"Unlock ($X) failed: {line}")
        raise CommandExecutionError("Unlock ($X) timed out waiting for ok")

    def soft_reset(self):
        """Soft reset the mill (GRBL Ctrl-X / 0x18)."""
        self._require_open_serial()
        self.logger.info("Sending soft reset (0x18)")
        self._write_serial(b"\x18")
        time.sleep(1.0)
        while self.ser_mill.in_waiting:
            line = self.ser_mill.readline().decode("ascii", errors="replace").strip()
            self.logger.debug("Soft reset response: %s", line)

    def reconnect(self, attempts: int = 4, delay_s: float = 1.0) -> None:
        """Re-open the serial port after the controller dropped off the bus.

        The USB bridge re-enumerates with the same device name, but the old
        file descriptor stays dead — every read times out. Prefer the
        previously connected port name; fall back to auto-scan on the final
        attempt in case the name changed. Enumeration is not instant, so
        retry with a delay.
        """
        port = self.connected_port()
        try:
            if self.ser_mill is not None:
                self.ser_mill.close()
        except Exception as exc:
            self.logger.warning("Closing stale serial handle failed: %s", exc)
        self.ser_mill = None
        self.active_connection = False
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            time.sleep(delay_s)
            try:
                self.connect(port=port if attempt < attempts else None)
            except MillConnectionError as exc:
                last_error = exc
                self.logger.warning(
                    "Serial reconnect attempt %d/%d failed: %s",
                    attempt,
                    attempts,
                    exc,
                )
                continue
            self.logger.info("Serial reconnect succeeded on attempt %d", attempt)
            return
        raise MillConnectionError(
            "Controller connection lost and serial reconnect failed after "
            f"{attempts} attempts"
        ) from last_error

    def soft_reset_and_unlock(self):
        """Soft reset followed by unlock — single serial sequence.

        Some controller boards drop off the USB bus when a hard-limit trip
        glitches their supply rail; the first symptom is the unlock read
        timing out here. Reconnect once and retry the unlock — after a bus
        drop the board has already rebooted, so the reset part is moot.
        """
        try:
            self.soft_reset()
            self.unlock()
        except (
            MillConnectionError,
            CommandExecutionError,
            OSError,
            serial.SerialException,
        ) as exc:
            if not looks_like_connection_loss(exc):
                raise
            self.logger.warning(
                "Controller stopped responding during reset/unlock (%s); "
                "attempting serial reconnect.",
                exc,
            )
            self.reconnect()
            self.unlock()

    def home(self, timeout=HOMING_TIMEOUT):
        """Home the mill with a timeout."""
        self.execute_command("$H")
        time.sleep(1)
        start_time = time.time()
        last_status_error = None

        while True:
            if time.time() - start_time > timeout:
                self.logger.warning("Homing timed out")
                if last_status_error is not None:
                    raise StatusReturnError(
                        f"Homing timed out after {timeout} seconds; "
                        f"last status error: {last_status_error}"
                    ) from last_status_error
                raise StatusReturnError(f"Homing timed out after {timeout} seconds")

            try:
                status = self.current_status()
            except StatusReturnError as exc:
                last_status_error = exc
                self.logger.warning("No valid status during homing; retrying: %s", exc)
                time.sleep(0.5)
                continue

            if "Idle" in status:
                self.logger.info("Homing completed")
                self.homed = True
                break

            if "hold" in status.lower():
                raise StatusReturnError(
                    "Homing paused by feed hold "
                    f"({status}). Resume or reset/unlock before retrying."
                )

            if "alarm" in status.lower():
                self.logger.warning("Homing failed, trying again...")
                self.clear_buffers()
                try:
                    self.execute_command("$H")
                except CommandExecutionError as exc:
                    last_status_error = exc
                    self.logger.warning("Homing retry command failed: %s", exc)

            time.sleep(0.5)

    def _wait_until_idle(self, incoming_status, suppress_errors: bool = False, timeout=5):
        """Wait for the mill to complete the previous command."""
        status = incoming_status
        start_time = time.time()
        while "idle" not in status.lower():
            lowered = status.lower()
            if "<run" in lowered:
                start_time = time.time()
            if "error" in lowered:
                if not suppress_errors:
                    self.logger.error("Error in status: %s", status)
                raise StatusReturnError(f"Error in status: {status}")
            if "alarm" in lowered:
                if not suppress_errors:
                    self.logger.error("Alarm in status: %s", status)
                raise StatusReturnError(f"Alarm in status: {status}")
            if time.time() - start_time > timeout:
                self.logger.warning("Command execution timed out")
                raise StatusReturnError(
                    f"Command execution timed out after {timeout} seconds: {status}"
                )
            status = self.current_status()
        return status

    def current_status(self) -> str:
        """Get the current status of the mill."""
        self._require_open_serial()
        attempt_limit = 5
        status = self._read_serial()

        while status.strip().lower() in ["", "ok"] and attempt_limit > 0:
            self._write_serial(b"?")
            time.sleep(0.05)
            status = self._read_serial()
            attempt_limit -= 1

        if not status:
            raw_lines = self.ser_mill.readlines()
            lines = [item.decode(errors="replace").rstrip() for item in raw_lines]
            if not lines:
                self.logger.error("Failed to get status from the mill")
                raise StatusReturnError("Failed to get status from the mill")
            # Find the first status line (<...>) or join all lines
            status = next((l for l in lines if l.startswith("<")), "; ".join(lines))
            self.last_status = status
            if any(re.search(r"\b(error|alarm)\b", item.lower()) for item in lines):
                self.logger.error("Error in status: %s", status)
                raise StatusReturnError(f"Error in status: {status}")
        self.last_status = status
        return status

    def _read_serial(self):
        self._require_open_serial()
        status_fragment = ""
        for _ in range(10):
            raw = self.ser_mill.readline()
            if raw == b"":
                return ""
            if isinstance(raw, str):
                line = raw
            else:
                line = raw.decode(encoding="ascii", errors="replace")
            line = line.strip()
            if not line:
                continue
            if status_fragment:
                status_fragment += line
                if ">" in status_fragment:
                    return status_fragment[: status_fragment.index(">") + 1]
                continue
            if line.startswith("<") and ">" not in line:
                status_fragment = line
                continue
            if line.startswith("<") and ">" in line:
                return line[: line.index(">") + 1]
            return line
        return ""

    def set_feed_rate(self, rate):
        """Set the feed rate."""
        self.execute_command(f"F{rate}")

    def clear_buffers(self):
        """Clear input and output buffers."""
        self._require_open_serial()
        self.ser_mill.flush()
        self.ser_mill.read_all()

    def read_grbl_settings(self) -> dict:
        """Return live GRBL settings from the connected controller."""
        self._require_open_serial()
        settings = self.execute_command("$$")
        self.config = settings
        return settings

    def set_grbl_setting(self, setting: str, value: str):
        """Set a grbl setting."""
        command = f"${setting}={value}"
        return self.execute_command(command)

    def enforce_wpos_mode(self):
        """Ensure GRBL reports WPos in status reports ($10=0) and uses absolute positioning (G90).

        A failed ``G90`` leaves the GRBL parser in whatever modal mode it was
        in before (potentially G91 relative). Subsequent ``G01 X<deck>``
        commands would then move *relatively*, which is a real-motion hazard,
        so the failure is raised — callers must decide whether to recover.
        """
        current = self.config.get("$10", "1")
        if current != "0":
            self.logger.info("Setting $10=0 (WPos status reporting)")
            self.execute_command("$10=0")
            self.config["$10"] = "0"
        self.execute_command("G90")
        self.logger.info("WPos mode and absolute positioning enforced")

    def seed_wco(self):
        """Poll GRBL status until WCO is reported, then cache it.

        GRBL includes WCO periodically in status reports. We query
        repeatedly so that we have the offset cached for any MPos→WPos
        conversion that might be needed.
        """
        self._require_open_serial()
        for _ in range(15):
            self._write_serial(b"?")
            time.sleep(0.15)
            status = self._read_serial()
            match = wco_pattern.search(status)
            if match:
                self._wco = Coordinates(
                    float(match.group(1)),
                    float(match.group(2)),
                    float(match.group(3)),
                )
                self.logger.info("WCO cached: %s", self._wco)
                return
        self.logger.warning("Could not obtain WCO from GRBL status reports")

    def is_connected(self) -> bool:
        """Check if the serial connection is open."""
        return bool(self.ser_mill and self.ser_mill.is_open)

    def query_raw_status(self) -> str:
        """Send GRBL '?' and return the raw status string (e.g. '<Idle|WPos:...>').

        Returns an empty string only when the driver is not connected — callers
        treat ``""`` as "nothing to inspect", not "no alarm". Serial errors
        propagate so that hardware-safety paths
        (:meth:`Gantry.prepare_for_protocol_run`,
        :meth:`Gantry._check_alarm_state`) cannot mistake a broken transport
        for a healthy mill.
        """
        if not self.is_connected():
            return ""
        try:
            self._write_serial(b"?")
            time.sleep(0.1)
            raw = ""
            for _ in range(5):
                raw = self._read_serial()
                if isinstance(raw, str) and "<" in raw:
                    return raw
                time.sleep(0.05)
            return str(raw) if raw else ""
        except (serial.SerialException, OSError) as exc:
            self.logger.warning("Raw status query failed: %s", exc)
            raise MillConnectionError(
                f"Raw status query failed: {exc}"
            ) from exc

    def _parse_position_from_status(self, status: str) -> Optional[Coordinates]:
        """Extract deck-frame WPos from a status frame, whatever the $10 mode.

        Prefers WPos when present; falls back to MPos minus the cached WCO.
        Also refreshes the cached WCO whenever the frame carries one.
        Returns ``None`` when the frame has neither position field (or MPos
        with no WCO available) — parsing is deliberately independent of the
        configured ``$10`` value so a controller in an unexpected reporting
        mode still yields a position.
        """
        wco_match = wco_pattern.search(status)
        if wco_match:
            self._wco = Coordinates(
                float(wco_match.group(1)),
                float(wco_match.group(2)),
                float(wco_match.group(3)),
            )

        match = wpos_pattern.search(status)
        if match:
            return Coordinates(
                round(float(match.group(1)), 3),
                round(float(match.group(2)), 3),
                round(float(match.group(3)), 3),
            )

        match = mpos_pattern.search(status)
        if match:
            if self._wco is None:
                self.seed_wco()
            if self._wco is None:
                return None
            return Coordinates(
                round(float(match.group(1)) - self._wco.x, 3),
                round(float(match.group(2)) - self._wco.y, 3),
                round(float(match.group(3)) - self._wco.z, 3),
            )
        return None

    def current_coordinates(self) -> Coordinates:
        """
        Get the current coordinates of the mill.

        Returns:
            Coordinates: current GRBL WPos in the CubOS deck frame.

        Raises:
            LocationNotFound: no parsable position after several queries —
                callers that can proceed safely without a position (absolute
                moves) may catch this and fall back.
        """
        self._require_open_serial()
        max_attempts = 4
        status = ""
        for attempt in range(max_attempts):
            self._write_serial(b"?")
            time.sleep(0.05)
            status = self._read_serial()
            # Skim past stale acknowledgments ("ok") and blank reads that
            # can queue ahead of the status frame mid-protocol.
            skims = 0
            while (not status or status[0] != "<") and skims < 6:
                if status and (
                    "alarm" in status.lower() or "error" in status.lower()
                ):
                    self.logger.error("Error in status: %s", status)
                    self.last_status = status
                    raise StatusReturnError(f"Error in status: {status}")
                status = self._read_serial()
                skims += 1

            self.last_status = status
            coords = self._parse_position_from_status(status)
            if coords is not None:
                self.logger.info(
                    "WPos coordinates: X = %s, Y = %s, Z = %s",
                    coords.x, coords.y, coords.z,
                )
                return coords

            self.logger.warning(
                "No position in GRBL status (attempt %d/%d): %r",
                attempt + 1, max_attempts, status,
            )
            time.sleep(0.2)

        self.logger.error("Error occurred while getting WPos coordinates")
        raise LocationNotFound(
            f"No parsable position in GRBL status after {max_attempts} "
            f"queries; last frame: {status!r}"
        )

    def _require_open_serial(self) -> None:
        if self.ser_mill is None or not getattr(self.ser_mill, "is_open", False):
            self.logger.error("Serial connection to mill is not open")
            raise MillConnectionError("Serial connection to mill is not open")

    def move_to(
        self,
        x_coordinate: float = 0.00,
        y_coordinate: float = 0.00,
        z_coordinate: float = 0.00,
        coordinates: Coordinates = None,
        travel_z: Optional[float] = None,
    ) -> None:
        """
        Move the mill to the specified coordinates.

        When ``travel_z`` is provided, XY travel happens at that Z rather
        than whatever Z the tip is currently at. The sequence becomes:
        lift/lower to ``travel_z`` at current XY, XY travel at ``travel_z``,
        then descend/ascend to the target Z. This lets callers
        (InstrumentedGantry, protocol commands) own their own "safe approach" height instead of
        the mill baking in a machine-wide retract.

        When ``travel_z`` is None, the mill issues a direct axis-by-axis
        move (X, then Y, then Z) — no Z detour, no diagonal interpolation.

        Args:
            x_coordinate (float): X coordinate.
            y_coordinate (float): Y coordinate.
            z_coordinate (float): Z coordinate.
            coordinates (Coordinates): Target coordinates object (overrides x/y/z params).
            travel_z (float): Machine-space Z to hold during XY travel.
        """
        goto = (
            Coordinates(x=x_coordinate, y=y_coordinate, z=z_coordinate)
            if not coordinates
            else coordinates
        )
        self._validate_target_coordinates(goto)
        if travel_z is not None:
            self._validate_finite_coordinate(travel_z, "travel Z")
        # The current position only prunes redundant steps below; every
        # emitted G-code is absolute, so a transient status-read failure
        # must not kill a run mid-motion. Fall back to emitting the full
        # unpruned sequence instead.
        try:
            current_coordinates = self.current_coordinates()
        except LocationNotFound as exc:
            self.logger.warning(
                "Position read failed before move (%s); emitting full "
                "absolute move sequence without pruning.", exc,
            )
            current_coordinates = None

        target_coordinates = goto

        if current_coordinates is not None and self._is_already_at_target(
            target_coordinates, current_coordinates
        ):
            self.logger.debug(
                "Mill is already at the target coordinates of [%s, %s, %s]",
                x_coordinate,
                y_coordinate,
                z_coordinate,
            )
            return

        self._log_target_coordinates(target_coordinates)
        self._validate_target_coordinates(target_coordinates)

        if travel_z is None:
            commands = self._build_direct_move(
                current_coordinates, target_coordinates
            )
        else:
            commands = self._build_transit_move(
                current_coordinates, target_coordinates, travel_z
            )
        for cmd in commands:
            self.execute_command(cmd)

    def _is_already_at_target(
        self, goto: Coordinates, current_coordinates: Coordinates
    ):
        """Check if the mill is already at the target coordinates."""
        return (goto.x, goto.y) == (
            current_coordinates.x,
            current_coordinates.y,
        ) and goto.z == current_coordinates.z

    def _log_target_coordinates(self, target_coordinates: Coordinates):
        self.logger.debug(
            "Target coordinates: [%s, %s, %s]",
            target_coordinates.x,
            target_coordinates.y,
            target_coordinates.z,
        )

    def _validate_target_coordinates(self, target_coordinates: Coordinates):
        self._validate_finite_coordinate(target_coordinates.x, "target X")
        self._validate_finite_coordinate(target_coordinates.y, "target Y")
        self._validate_finite_coordinate(target_coordinates.z, "target Z")

    @staticmethod
    def _validate_finite_coordinate(value: float, label: str) -> None:
        try:
            numeric_value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be finite; got {value!r}.") from exc
        if not math.isfinite(numeric_value):
            raise ValueError(f"{label} must be finite; got {value!r}.")

    def _build_direct_move(
        self,
        current_coordinates: Coordinates,
        target_coordinates: Coordinates,
    ):
        """Direct move from current to target, axis-by-axis.

        Emits one G-code per changed axis in X-then-Y-then-Z order.
        The mill never commands simultaneous multi-axis (diagonal)
        motion — combining axes in a single G01 would couple their
        motion into a straight interpolation that could graze
        obstacles the caller didn't plan for.

        ``current_coordinates`` may be ``None`` (position read failed);
        every axis is then emitted unconditionally — safe because the
        commands are absolute.
        """
        f = f" F{self.default_feed_rate}"
        self._validate_target_coordinates(target_coordinates)
        commands = []
        if current_coordinates is None or target_coordinates.x != current_coordinates.x:
            commands.append(f"G01 X{target_coordinates.x}{f}")
        if current_coordinates is None or target_coordinates.y != current_coordinates.y:
            commands.append(f"G01 Y{target_coordinates.y}{f}")
        if current_coordinates is None or target_coordinates.z != current_coordinates.z:
            commands.append(f"G01 Z{target_coordinates.z}{f}")
        return commands

    def _build_transit_move(
        self,
        current_coordinates: Coordinates,
        target_coordinates: Coordinates,
        travel_z: float,
    ):
        """Transit via ``travel_z``, axis-by-axis: lift → X → Y → descend.

        Each step is emitted only when it would produce actual motion,
        so a move already at ``travel_z`` skips the lift, a same-X
        (or same-Y) move skips that axis, and a final Z matching
        ``travel_z`` skips the descent. X and Y always move in
        separate G-codes — no diagonal.

        ``current_coordinates`` may be ``None`` (position read failed);
        the full lift → X → Y → descend sequence is then emitted
        unconditionally — safe because the commands are absolute and the
        lift happens first.
        """
        f = f" F{self.default_feed_rate}"
        self._validate_target_coordinates(target_coordinates)
        self._validate_finite_coordinate(travel_z, "travel Z")
        commands = []
        if current_coordinates is None or current_coordinates.z != travel_z:
            commands.append(f"G01 Z{travel_z}{f}")
        if current_coordinates is None or target_coordinates.x != current_coordinates.x:
            commands.append(f"G01 X{target_coordinates.x}{f}")
        if current_coordinates is None or target_coordinates.y != current_coordinates.y:
            commands.append(f"G01 Y{target_coordinates.y}{f}")
        if target_coordinates.z != travel_z:
            commands.append(f"G01 Z{target_coordinates.z}{f}")
        return commands
