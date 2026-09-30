"""Generic vial capper/decapper instrument interface.

The public surface is fully vendor-agnostic: it exposes the two physical
actions a capper mechanism performs (grabbing/holding a cap, releasing a
held cap) and a sensor-confirmed readback of whether a cap is currently
held. Any vendor-specific protocol (serial command IDs, electromagnet vs.
another actuation mechanism, how a "line-break" or similar sensor is
represented) lives entirely in ``vendors/<vendor>.py`` implementations --
the protocol engine and this interface never branch on a machine name.

Motion sequencing (approach at ``safe_z`` -> engage Z -> capture/release ->
retract) is owned by the protocol-engine ``decap``/``cap`` commands
(``cubos.protocol_engine.commands.capper``), built from the same generic
gantry primitives every other instrument uses
(``InstrumentedGantry.move_to_labware`` / ``.move``). This interface only
owns the parameters that PARAMETERIZE that sequence so it is never
hardcoded in the protocol commands:

* ``engage_depth_mm`` -- labware-relative Z offset (mm; typically negative)
  from the vial's calibrated rim reference the tool head descends to while
  capturing/releasing a cap.
* ``capture_retries`` -- number of additional actuate-then-sense attempts
  before failing closed.
* ``capture_settle_s`` -- seconds to wait after actuation before reading the
  sensor.
"""

from __future__ import annotations

import math
from abc import abstractmethod
from typing import Optional

from cubos.instruments.base_instrument import BaseInstrument
from cubos.instruments.capper.exceptions import CapperConfigError
from cubos.instruments.capper.models import CapperStatus


class CapperInstrument(BaseInstrument):
    """Base class for vial capper/decapper implementations."""

    def __init__(
        self,
        *,
        engage_depth_mm: float,
        capture_retries: int = 2,
        capture_settle_s: float = 1.0,
        name: Optional[str] = None,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
        depth: float = 0.0,
        offline: bool = False,
    ):
        super().__init__(
            name=name, offset_x=offset_x, offset_y=offset_y,
            depth=depth, offline=offline,
        )
        self.engage_depth_mm = _finite_number(engage_depth_mm, "engage_depth_mm")
        self.capture_retries = _nonnegative_int(capture_retries, "capture_retries")
        self.capture_settle_s = _finite_nonnegative(
            capture_settle_s, "capture_settle_s",
        )

    @abstractmethod
    def capture_cap(self) -> None:
        """Actuate the mechanism to grab/hold a cap (the ``decap`` capture step)."""

    @abstractmethod
    def release_cap(self) -> None:
        """Actuate the mechanism to release a held cap (the ``cap`` release step)."""

    @abstractmethod
    def read_cap_present(self) -> bool:
        """Sensor-confirmed read of whether a cap is currently held at the head."""

    @abstractmethod
    def get_status(self) -> CapperStatus:
        """Return the current capper status."""


def _finite_number(value, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise CapperConfigError(f"{field_name} must be a finite number, got {value!r}.")
    return float(value)


def _finite_nonnegative(value, field_name: str) -> float:
    result = _finite_number(value, field_name)
    if result < 0:
        raise CapperConfigError(f"{field_name} must be >= 0, got {value!r}.")
    return result


def _nonnegative_int(value, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CapperConfigError(
            f"{field_name} must be a non-negative integer, got {value!r}."
        )
    return value


