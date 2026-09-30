"""Lighting instrument exceptions."""

from cubos.instruments.base_instrument import InstrumentError


class LightingError(InstrumentError):
    """Base exception for lighting instrument errors."""


class LightingConfigError(LightingError):
    """Invalid lighting configuration or unsupported channel/level."""


class LightingConnectionError(LightingError):
    """Lighting hardware could not be reached."""


class LightingCommandError(LightingError):
    """A lighting command failed at the hardware."""


class LightingTimeoutError(LightingError):
    """A lighting command timed out."""
