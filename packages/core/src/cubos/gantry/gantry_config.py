"""Gantry configuration domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class YAxisMotion(str, Enum):
    """Whether Y-axis motion moves the head or the bed (base plate)."""

    HEAD = "head"
    BED = "bed"


class GantryType(str, Enum):
    """Supported physical gantry families."""

    CUB = "cub"
    CUB_XL = "cub_xl"


class OriginPolicy(str, Enum):
    """Which physical corner WPos zero is calibrated to.

    ``DECK_ORIGIN`` (default): WPos zero at the front-left-bottom deck
    corner; the entire reachable working volume is non-negative.
    ``HOME_ORIGIN``: WPos zero at the homed back-right-top corner; the
    entire reachable working volume is non-positive (mirror-symmetric with
    ``DECK_ORIGIN``).
    """

    DECK_ORIGIN = "deck_origin"
    HOME_ORIGIN = "home_origin"


@dataclass(frozen=True)
class WorkingVolume:
    """Gantry working volume bounds in millimeters.

    Bounds are inclusive and use the CubOS deck-origin frame for supported
    protocol execution.
    """

    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_min: float
    z_max: float

    def __post_init__(self) -> None:
        for axis in ("x", "y", "z"):
            lo = getattr(self, f"{axis}_min")
            hi = getattr(self, f"{axis}_max")
            if lo >= hi:
                raise ValueError(
                    f"{axis}_min ({lo}) must be < {axis}_max ({hi})"
                )

    def contains(self, x: float, y: float, z: float) -> bool:
        """Return True if (x, y, z) is within this working volume (inclusive)."""
        return (
            self.x_min <= x <= self.x_max
            and self.y_min <= y <= self.y_max
            and self.z_min <= z <= self.z_max
        )


@dataclass(frozen=True)
class GantryConfig:
    """Loaded gantry configuration."""

    serial_port: str
    gantry_type: GantryType
    factory_z_travel_mm: float
    working_volume: WorkingVolume
    y_axis_motion: YAxisMotion = YAxisMotion.HEAD
    origin_policy: OriginPolicy = OriginPolicy.DECK_ORIGIN
    calibration_block_height_mm: Optional[float] = None
    safe_z: Optional[float] = None
    default_feed_rate_mm_min: Optional[float] = None
    expected_grbl_settings: Optional[Dict[str, float]] = field(default=None)
    instruments: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "gantry_type", GantryType(self.gantry_type))
        except ValueError as exc:
            raise ValueError(
                f"Unsupported gantry_type {self.gantry_type!r}."
            ) from exc
        try:
            object.__setattr__(
                self, "origin_policy", OriginPolicy(self.origin_policy),
            )
        except ValueError as exc:
            raise ValueError(
                f"Unsupported origin_policy {self.origin_policy!r}."
            ) from exc
        if self.factory_z_travel_mm <= 0:
            raise ValueError(
                f"factory_z_travel_mm ({self.factory_z_travel_mm}) must be > 0"
            )
        if (
            self.calibration_block_height_mm is not None
            and self.calibration_block_height_mm <= 0
        ):
            raise ValueError(
                "calibration_block_height_mm "
                f"({self.calibration_block_height_mm}) must be > 0"
            )
        if self.safe_z is not None:
            if not (
                self.working_volume.z_min
                <= self.safe_z
                <= self.working_volume.z_max
            ):
                raise ValueError(
                    "safe_z must be within the configured "
                    "working-volume Z bounds."
                )
        if self.default_feed_rate_mm_min is not None and self.default_feed_rate_mm_min <= 0:
            raise ValueError(
                f"default_feed_rate_mm_min ({self.default_feed_rate_mm_min}) must be > 0"
            )

    @property
    def resolved_safe_z(self) -> float:
        """Effective safe travel Z: explicit ``safe_z`` or ``working_volume.z_max``."""
        return self.safe_z if self.safe_z is not None else self.working_volume.z_max
