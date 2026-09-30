"""Pawduino imaging lights: white and contact (red+blue) channels.

Discrete brightness commands — white: 17=100%, 18=50%, 19=25%, 20=15%,
21=10%, 22=5%, off=2; contact: 23=50%, 24=30%, 25=20%, 26=10%, 27=5%,
off=4. Uses the shared PawduinoLink (the capper and pipette share this
board); never opens the port itself. Pass ``offline=True`` for dry runs.
"""

from __future__ import annotations

from typing import Mapping, Optional, Tuple

from cubos.instruments.controllers.pawduino import (
    PawduinoLink,
    PawduinoLinkCommandError,
    PawduinoLinkError,
    PawduinoLinkTimeoutError,
)
from cubos.instruments.lighting.exceptions import (
    LightingCommandError,
    LightingConnectionError,
    LightingTimeoutError,
)
from cubos.instruments.lighting.interface import LightingInstrument
from cubos.instruments.lighting.models import LightingStatus

_CMD_WHITE_OFF = 2
_CMD_CONTACT_OFF = 4

# channel -> {brightness_pct: on-command id}
_CHANNEL_COMMANDS: dict[str, dict[int, int]] = {
    "white": {100: 17, 50: 18, 25: 19, 15: 20, 10: 21, 5: 22},
    "contact": {50: 23, 30: 24, 20: 25, 10: 26, 5: 27},
}

_CHANNEL_OFF_COMMANDS: dict[str, int] = {
    "white": _CMD_WHITE_OFF,
    "contact": _CMD_CONTACT_OFF,
}


class PawduinoLighting(LightingInstrument):
    """White + contact (red/blue) imaging lights over shared Arduino serial."""

    def __init__(
        self,
        *,
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
            name=name,
            offset_x=offset_x,
            offset_y=offset_y,
            depth=depth,
            offline=offline,
        )
        self._port = port
        self._baud_rate = baud_rate
        self._command_timeout = command_timeout
        self._link: Optional[PawduinoLink] = None
        # No firmware readback for lights; shadow the state in memory.
        self._active: dict[str, int] = {ch: 0 for ch in _CHANNEL_COMMANDS}

    # ── BaseInstrument interface ──────────────────────────────────────────

    def connect(self) -> None:
        self._active = {ch: 0 for ch in _CHANNEL_COMMANDS}
        if self._offline:
            self.logger.info("Lighting connected (offline)")
            return
        try:
            self._link = PawduinoLink.acquire(self._port, self._baud_rate)
            self._link.connect(timeout=self._command_timeout)
        except PawduinoLinkError as exc:
            self._link = None
            raise LightingConnectionError(str(exc)) from exc

        # Command lights off explicitly for a known state on connect.
        try:
            self.all_off()
        except (LightingCommandError, LightingTimeoutError) as exc:
            self._release_link()
            raise LightingConnectionError(
                f"Arduino did not respond after connect: {exc}"
            ) from exc
        self.logger.info("Connected to lighting on %s", self._port)

    def disconnect(self) -> None:
        if self._offline:
            self._active = {ch: 0 for ch in _CHANNEL_COMMANDS}
            self.logger.info("Lighting disconnected (offline)")
            return
        if self._link is not None and self._link.is_open:
            try:
                self.all_off()
            except (LightingCommandError, LightingTimeoutError) as exc:
                self.logger.warning("Lights-off on disconnect failed: %s", exc)
        self._release_link()
        self.logger.info("Disconnected from lighting")

    def health_check(self) -> bool:
        if self._offline:
            return True
        return self._link is not None and self._link.is_open

    # ── LightingInstrument interface ──────────────────────────────────────

    @property
    def channels(self) -> Mapping[str, Tuple[int, ...]]:
        return {
            channel: tuple(sorted(levels))
            for channel, levels in _CHANNEL_COMMANDS.items()
        }

    def set_channel(self, channel: str, brightness_pct: int) -> None:
        self.validate_channel_level(channel, brightness_pct)
        if brightness_pct == 0:
            code = _CHANNEL_OFF_COMMANDS[channel]
        else:
            code = _CHANNEL_COMMANDS[channel][brightness_pct]
        if not self._offline:
            self._send_command(code)
        self._active[channel] = brightness_pct
        self.logger.info(
            "Lighting channel %s -> %s",
            channel,
            f"{brightness_pct}%" if brightness_pct else "off",
        )

    def all_off(self) -> None:
        for channel in _CHANNEL_COMMANDS:
            if not self._offline:
                self._send_command(_CHANNEL_OFF_COMMANDS[channel])
            self._active[channel] = 0
        self.logger.info("All lighting channels off")

    def status(self) -> LightingStatus:
        return LightingStatus(channels=dict(self._active))

    # ── Private helpers ───────────────────────────────────────────────────

    def _send_command(self, code: int) -> str:
        if self._link is None:
            raise LightingCommandError("Not connected to Arduino")
        try:
            return self._link.send_command(
                code, timeout=self._command_timeout,
            )
        except PawduinoLinkTimeoutError as exc:
            raise LightingTimeoutError(str(exc)) from exc
        except PawduinoLinkCommandError as exc:
            raise LightingCommandError(str(exc)) from exc

    def _release_link(self) -> None:
        if self._link is not None:
            self._link.disconnect()
            self._link = None
