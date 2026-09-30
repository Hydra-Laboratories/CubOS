"""Driver for the PANDA-family Arduino ("Pawduino") capper/decapper serial link.

Protocol extracted from PANDA-BEAR's ``panda_lib.hardware.arduino_interface``
(``ArduinoLink`` / ``PawduinoFunctions`` / ``PawduinoReturnCodes``, read-only
source -- never imported from here). The wire format is identical to
``cubos.instruments.pipette.vendors.opentrons.OpentronsPipette`` (same
Arduino, same firmware family, hence the vendor key ``pawduino`` rather than
a machine name): a comma-separated ``"<command_id>,<args...>\\n"`` line out,
a single ``"OK:..."`` / ``"ERR:..."``-prefixed line back within
``command_timeout`` seconds.

Command IDs used here (``PawduinoFunctions`` in the source):

* ``5``  ``CMD_EMAG_ON``   -- energize the electromagnet (grabs/holds a cap
  against the tool head). Response ``"OK:Electromagnet on"``.
* ``6``  ``CMD_EMAG_OFF``  -- de-energize the electromagnet (releases a held
  cap). Response ``"OK:Electromagnet off"``.
* ``7``  ``CMD_LINE_BREAK`` -- read the line-break sensor mounted at the
  tool head. Response body is ``{"value1":1}`` (beam broken -- a cap is
  present/held at the head) or ``{"value1":0}`` (beam unbroken -- no cap at
  the head). Note this response is JSON-quoted-key style, unlike the
  pipette status response on the same link (``{homed:1,pos:...}``,
  unquoted) -- both are handled by the same Arduino firmware but were
  evidently serialized by different code paths, so the parser here
  tolerates quoted *and* unquoted keys.

Electromagnet-grab semantics (from ``vessel_handling.decapping_sequence`` /
``capping_sequence`` in the source): decapping engages the electromagnet
*before* retracting, so success is confirmed by the line-break sensor
reporting a cap present (``value1 == 1``) after retract; capping
de-energizes the electromagnet before retracting, so success is confirmed by
the sensor reporting *no* cap present (``value1 == 0``). That
capture-then-confirm / release-then-confirm mapping is exactly
``CapperInstrument.capture_cap``/``release_cap`` plus
``read_cap_present``; the retry-then-fail-closed policy the source
implements inline (``for attempt in range(3): ...``) is reimplemented
generically in ``cubos.protocol_engine.commands.capper`` using
``capture_retries``, not duplicated per vendor.
"""

from __future__ import annotations

from typing import Optional

from cubos.instruments.controllers.pawduino import (
    PawduinoLink,
    PawduinoLinkCommandError,
    PawduinoLinkError,
    PawduinoLinkTimeoutError,
)
from cubos.instruments.capper.exceptions import (
    CapperCommandError,
    CapperConnectionError,
    CapperSensorFault,
    CapperTimeoutError,
)
from cubos.instruments.capper.interface import CapperInstrument
from cubos.instruments.capper.models import CapperStatus

_CMD_EMAG_ON = 5
_CMD_EMAG_OFF = 6
_CMD_LINE_BREAK = 7


class PawduinoCapper(CapperInstrument):
    """Electromagnet-actuated capper/decapper driven over Arduino serial.

    Pass ``offline=True`` for dry runs -- simulates the electromagnet/sensor
    state in memory, identically to ``OpentronsPipette``.
    """

    def __init__(
        self,
        *,
        engage_depth_mm: float,
        capture_retries: int = 2,
        capture_settle_s: float = 1.0,
        port: str = "",
        baud_rate: int = 115200,
        command_timeout: float = 30.0,
        name: Optional[str] = None,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
        depth: float = 0.0,
        offline: bool = False,
        **kwargs,
    ):
        super().__init__(
            engage_depth_mm=engage_depth_mm,
            capture_retries=capture_retries,
            capture_settle_s=capture_settle_s,
            name=name, offset_x=offset_x, offset_y=offset_y,
            depth=depth, offline=offline,
        )
        self._port = port
        self._baud_rate = baud_rate
        self._command_timeout = command_timeout
        self._link: Optional[PawduinoLink] = None
        self._cap_present = False

    # ── BaseInstrument interface ──────────────────────────────────────────

    def connect(self) -> None:
        if self._offline:
            self.logger.info("Capper connected (offline)")
            return
        # The pipette and lights share this Arduino via one link per port.
        try:
            self._link = PawduinoLink.acquire(self._port, self._baud_rate)
            self._link.connect(timeout=self._command_timeout)
        except PawduinoLinkError as exc:
            self._link = None
            raise CapperConnectionError(str(exc)) from exc

        try:
            self.read_cap_present()
        except (CapperCommandError, CapperTimeoutError, CapperSensorFault) as exc:
            self._release_link()
            raise CapperConnectionError(
                f"Arduino did not respond after connect: {exc}"
            ) from exc

        self.logger.info("Connected to capper on %s", self._port)

    def disconnect(self) -> None:
        if self._offline:
            self.logger.info("Capper disconnected (offline)")
            return
        self._release_link()
        self.logger.info("Disconnected from capper")

    def health_check(self) -> bool:
        if self._offline:
            return True
        if self._link is None or not self._link.is_open:
            return False
        try:
            self.read_cap_present()
            return True
        except (CapperCommandError, CapperTimeoutError, CapperSensorFault):
            return False

    # ── Capper-specific commands ──────────────────────────────────────────

    def capture_cap(self) -> None:
        if self._offline:
            self._cap_present = True
            return
        self._send_command(_CMD_EMAG_ON)

    def release_cap(self) -> None:
        if self._offline:
            self._cap_present = False
            return
        self._send_command(_CMD_EMAG_OFF)

    def read_cap_present(self) -> bool:
        if self._offline:
            return self._cap_present
        response = self._send_command(_CMD_LINE_BREAK)
        return self._parse_line_break_response(response)

    def get_status(self) -> CapperStatus:
        return CapperStatus(cap_present=self.read_cap_present())

    # ── Private helpers ───────────────────────────────────────────────────

    def _send_command(self, code: int, *args: float) -> str:
        if self._link is None:
            raise CapperCommandError("Not connected to Arduino")
        try:
            return self._link.send_command(
                code, *args, timeout=self._command_timeout,
            )
        except PawduinoLinkTimeoutError as exc:
            raise CapperTimeoutError(str(exc)) from exc
        except PawduinoLinkCommandError as exc:
            raise CapperCommandError(str(exc)) from exc

    @staticmethod
    def _parse_line_break_response(response: str) -> bool:
        """Parse the line-break sensor response into a cap-present bool.

        Tolerates both the quoted-key JSON-ish form the real firmware sends
        for this command (``{"value1":1}``) and an unquoted form, in case a
        future firmware revision aligns it with the pipette-status format.
        """
        body = response.removeprefix("OK:").strip()
        if body.startswith("{") and body.endswith("}"):
            body = body[1:-1]
        for pair in body.split(","):
            if ":" not in pair:
                continue
            key, _, raw_value = pair.partition(":")
            key = key.strip().strip('"')
            if key != "value1":
                continue
            value = raw_value.strip().strip('"')
            try:
                return int(value) == 1
            except ValueError:
                raise CapperSensorFault(
                    f"Unparseable line-break sensor value {value!r} in "
                    f"response {response!r}."
                ) from None
        raise CapperSensorFault(
            f"No 'value1' field in line-break sensor response {response!r}."
        )

    def _release_link(self) -> None:
        if self._link is not None:
            self._link.disconnect()
            self._link = None
