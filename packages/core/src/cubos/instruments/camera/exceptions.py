"""Camera instrument exceptions."""

from cubos.instruments.base_instrument import InstrumentError


class CameraError(InstrumentError):
    """Base exception for camera instrument errors."""


class CameraConfigError(CameraError):
    """Invalid camera configuration or missing acquisition dependency."""


class CameraConnectionError(CameraError):
    """Camera hardware could not be reached."""


class CameraCaptureError(CameraError):
    """A capture failed at the hardware."""
