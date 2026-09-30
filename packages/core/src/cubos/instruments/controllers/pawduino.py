"""Shared serial transport for the PANDA-family Arduino ("Pawduino").

One Arduino serves several instruments (capper, pipette, lights) over one
serial port, and opening the port twice resets the board mid-session. The
link makes the port the shared resource: one instance per port string,
refcounted connect/disconnect (only the first open pays the DTR reset and
boot-banner drain), and one lock per command round-trip so concurrent
drivers get correctly paired responses. Offline simulation stays in the
vendors; the link only represents real hardware.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional

import serial


_ARDUINO_SETTLE_TIME = 2.0
_CMD_HELLO = 0
_HELLO_MARKER = "Hello"
_HELLO_TIMEOUT = 5.0


class PawduinoLinkError(Exception):
    """Base exception for Pawduino link failures."""


class PawduinoLinkConfigError(PawduinoLinkError):
    """Conflicting or invalid link configuration."""


class PawduinoLinkConnectionError(PawduinoLinkError):
    """The serial port could not be opened."""


class PawduinoLinkCommandError(PawduinoLinkError):
    """A command could not be sent or the firmware answered ``ERR:``."""


class PawduinoLinkTimeoutError(PawduinoLinkError):
    """No terminal response arrived within the command timeout."""


class PawduinoLink:
    """Refcounted, lock-serialized serial link to one Pawduino board."""

    _registry: Dict[str, "PawduinoLink"] = {}
    _registry_lock = threading.Lock()

    def __init__(self, port: str, baud_rate: int) -> None:
        # Use acquire(), not direct construction.
        self._port = port
        self._baud_rate = baud_rate
        self._serial: Optional[serial.Serial] = None
        self._holders = 0
        self._lock = threading.Lock()

    # ── Registry ──────────────────────────────────────────────────────────

    @classmethod
    def acquire(cls, port: str, baud_rate: int = 115200) -> "PawduinoLink":
        """Return the shared link for *port*, creating it on first use."""
        if not port:
            raise PawduinoLinkConfigError(
                "Pawduino link requires a non-empty serial port."
            )
        with cls._registry_lock:
            link = cls._registry.get(port)
            if link is None:
                link = cls(port, baud_rate)
                cls._registry[port] = link
            elif link._baud_rate != baud_rate:
                raise PawduinoLinkConfigError(
                    f"Pawduino port {port} is already registered at "
                    f"{link._baud_rate} baud; cannot re-acquire at {baud_rate}."
                )
            return link

    @classmethod
    def reset_registry(cls) -> None:
        """Drop all registered links (test isolation only)."""
        with cls._registry_lock:
            for link in cls._registry.values():
                link._force_close()
            cls._registry.clear()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    @property
    def port(self) -> str:
        return self._port

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def connect(self, timeout: float = 30.0) -> None:
        """Open the port on the first holder; later connects just count."""
        with self._lock:
            if self._holders > 0 and self.is_open:
                self._holders += 1
                return
            try:
                self._serial = serial.Serial(
                    port=self._port,
                    baudrate=self._baud_rate,
                    timeout=timeout,
                )
            except serial.SerialException as exc:
                self._serial = None
                raise PawduinoLinkConnectionError(
                    f"Cannot open serial port {self._port}: {exc}"
                ) from exc
            time.sleep(_ARDUINO_SETTLE_TIME)
            self._drain_input_locked()
            # The boot banner can trail the drain window and would then be
            # consumed as the first command's response, skewing every reply
            # one command behind. A hello round-trip resynchronizes: expect=
            # skips stale lines until the hello answer arrives.
            try:
                self._send_command_locked(
                    _CMD_HELLO, timeout=min(timeout, _HELLO_TIMEOUT),
                    expect=_HELLO_MARKER,
                )
            except PawduinoLinkError as exc:
                self._close_locked()
                raise PawduinoLinkConnectionError(
                    f"Pawduino on {self._port} did not answer hello: {exc}"
                ) from exc
            self._holders = 1

    def disconnect(self) -> None:
        """Release one holder; close the port when the last one leaves."""
        with self._lock:
            if self._holders == 0:
                return
            self._holders -= 1
            if self._holders == 0:
                self._close_locked()

    def _force_close(self) -> None:
        with self._lock:
            self._holders = 0
            self._close_locked()

    def _close_locked(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except serial.SerialException:
                pass
            self._serial = None

    # ── Commands ──────────────────────────────────────────────────────────

    def send_command(
        self,
        code: int,
        *args: float,
        timeout: float = 30.0,
        expect: Optional[str] = None,
    ) -> str:
        """Send one command and return its ``OK:`` response line.

        ``expect`` skips ``OK:`` lines not containing it (stale responses
        from a prior timeout).
        """
        with self._lock:
            return self._send_command_locked(
                code, *args, timeout=timeout, expect=expect,
            )

    def _send_command_locked(
        self,
        code: int,
        *args: float,
        timeout: float = 30.0,
        expect: Optional[str] = None,
    ) -> str:
        if not self.is_open:
            raise PawduinoLinkCommandError(
                f"Pawduino link on {self._port} is not connected."
            )
        parts = [str(code)] + [str(a) for a in args]
        message = ",".join(parts) + "\n"
        try:
            self._serial.write(message.encode())
            self._serial.flush()
        except serial.SerialException as exc:
            raise PawduinoLinkCommandError(
                f"Failed to send command {code}: {exc}"
            ) from exc

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._serial.readline().decode().strip()
            except serial.SerialException as exc:
                raise PawduinoLinkCommandError(
                    f"Serial read error for command {code}: {exc}"
                ) from exc
            if not line:
                continue
            if line.startswith("OK:"):
                if expect is not None and expect not in line:
                    continue
                return line
            if line.startswith("ERR:"):
                raise PawduinoLinkCommandError(
                    f"Command {code} failed: {line}"
                )
        raise PawduinoLinkTimeoutError(
            f"Timed out ({timeout}s) waiting for response to command {code}"
        )

    def _drain_input_locked(self, quiet_s: float = 0.6, max_s: float = 5.0) -> None:
        """Discard pending input until the port stays quiet for *quiet_s*."""
        if self._serial is None:
            return
        deadline = time.monotonic() + max_s
        while time.monotonic() < deadline:
            if self._serial.in_waiting:
                self._serial.reset_input_buffer()
            time.sleep(quiet_s)
            if not self._serial.in_waiting:
                return
