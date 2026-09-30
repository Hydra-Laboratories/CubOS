from cubos.instruments.base_instrument import InstrumentError


class PipetteError(InstrumentError):
    """Base exception for all pipette instrument errors."""


class PipetteConnectionError(PipetteError):
    """Raised when the serial connection to the Arduino cannot be established."""


class PipetteCommandError(PipetteError):
    """Raised when a command fails or the Arduino returns an ERR: response."""


class PipetteTimeoutError(PipetteError):
    """Raised when the Arduino does not respond within the timeout period."""


class PipetteConfigError(PipetteError):
    """Raised when an unknown pipette model is requested."""


class PipetteBatteryError(PipetteError):
    """Raised when a battery-powered pipette is too low to start work."""


class PipetteMotorControlError(PipetteError):
    """Raised when the instrument revokes host control of its motor.

    Distinct from a command failure: the piston position after an abort is
    unknown, so the driver must fail closed rather than retry.
    """
