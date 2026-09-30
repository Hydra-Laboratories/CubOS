"""Generic lighting instrument interface.

Lighting is non-positional: it owns named channels (e.g. ``white``,
``contact``) with vendor-declared discrete brightness levels. Sequencing
belongs to the protocol commands; as a fail-safe,
``InstrumentedGantry.disconnect_instruments`` turns every lighting
instrument off at the end of every run.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Mapping, Optional, Tuple

from cubos.instruments.base_instrument import BaseInstrument
from cubos.instruments.lighting.exceptions import LightingConfigError
from cubos.instruments.lighting.models import LightingStatus


class LightingInstrument(BaseInstrument):
    """Base class for multi-channel lighting implementations."""

    def __init__(
        self,
        name: Optional[str] = None,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
        depth: float = 0.0,
        offline: bool = False,
    ):
        super().__init__(
            name=name,
            offset_x=offset_x,
            offset_y=offset_y,
            depth=depth,
            offline=offline,
        )

    @property
    @abstractmethod
    def channels(self) -> Mapping[str, Tuple[int, ...]]:
        """Supported brightness percentages per channel, ascending."""

    @abstractmethod
    def set_channel(self, channel: str, brightness_pct: int) -> None:
        """Turn *channel* on at *brightness_pct* (0 turns the channel off).

        Raises :class:`LightingConfigError` for an unknown channel or a
        brightness the channel does not support.
        """

    @abstractmethod
    def all_off(self) -> None:
        """Turn every channel off."""

    @abstractmethod
    def status(self) -> LightingStatus:
        """Return the active brightness per channel (0 = off)."""

    def validate_channel_level(self, channel: str, brightness_pct: int) -> None:
        """Shared exact-match validation against :attr:`channels`."""
        levels = self.channels.get(channel)
        if levels is None:
            raise LightingConfigError(
                f"Unknown lighting channel {channel!r}. "
                f"Available: {', '.join(sorted(self.channels))}"
            )
        if brightness_pct == 0:
            return
        if brightness_pct not in levels:
            raise LightingConfigError(
                f"Channel {channel!r} does not support {brightness_pct}%. "
                f"Supported levels: {', '.join(str(v) for v in levels)} "
                "(or 0 to turn the channel off)."
            )
