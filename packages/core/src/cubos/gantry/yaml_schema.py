"""Strict Pydantic schemas for gantry YAML."""

from __future__ import annotations

from typing import Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cubos.instruments.yaml_schema import InstrumentYamlEntry

from .grbl_settings import GrblSettingsYaml


class WorkingVolumeYaml(BaseModel):
    """Gantry working volume bounds in millimeters."""

    model_config = ConfigDict(extra="forbid")

    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_min: float
    z_max: float

    @model_validator(mode="after")
    def _validate_min_less_than_max(self) -> "WorkingVolumeYaml":
        for axis in ("x", "y", "z"):
            min_val = getattr(self, f"{axis}_min")
            max_val = getattr(self, f"{axis}_max")
            if min_val >= max_val:
                raise ValueError(
                    f"{axis}_min ({min_val}) must be < {axis}_max ({max_val})"
                )
        return self


class CncYaml(BaseModel):
    """CNC gantry settings."""

    model_config = ConfigDict(extra="forbid")

    factory_z_travel_mm: float
    calibration_block_height_mm: Optional[float] = None
    y_axis_motion: Literal["head", "bed"] = "head"
    safe_z: Optional[float] = None
    default_feed_rate_mm_min: Optional[float] = None

    @model_validator(mode="after")
    def _validate_cnc_z_values(self) -> "CncYaml":
        if self.factory_z_travel_mm <= 0:
            raise ValueError("factory_z_travel_mm must be > 0.")
        if (
            self.calibration_block_height_mm is not None
            and self.calibration_block_height_mm <= 0
        ):
            raise ValueError("calibration_block_height_mm must be > 0.")
        return self


class GantryYamlSchema(BaseModel):
    """Root gantry YAML schema.

    A gantry YAML is the machine configuration: motion envelope, controller
    expectations, and mounted instruments.
    """

    model_config = ConfigDict(extra="forbid")

    serial_port: str
    gantry_type: Literal["cub", "cub_xl"]
    cnc: CncYaml
    working_volume: WorkingVolumeYaml
    origin_policy: Literal["deck_origin", "home_origin"] = "deck_origin"
    grbl_settings: Optional[GrblSettingsYaml] = None
    instruments: Dict[str, InstrumentYamlEntry] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_factory_z_travel_covers_working_z_span(self) -> "GantryYamlSchema":
        z_span = self.working_volume.z_max - self.working_volume.z_min
        if self.cnc.factory_z_travel_mm < z_span:
            raise ValueError(
                "cnc.factory_z_travel_mm must be >= "
                "(working_volume.z_max - working_volume.z_min)."
            )
        return self

    @model_validator(mode="after")
    def _validate_safe_z_within_working_volume(self) -> "GantryYamlSchema":
        if self.cnc.safe_z is None:
            return self
        if not (
            self.working_volume.z_min
            <= self.cnc.safe_z
            <= self.working_volume.z_max
        ):
            raise ValueError(
                f"cnc.safe_z ({self.cnc.safe_z}) must be within "
                f"[{self.working_volume.z_min}, {self.working_volume.z_max}]."
            )
        return self

    @property
    def safe_z(self) -> float:
        """Resolved absolute deck-frame safe travel Z.

        Defaults to ``working_volume.z_max`` when ``cnc.safe_z`` is omitted.
        """
        if self.cnc.safe_z is not None:
            return self.cnc.safe_z
        return self.working_volume.z_max
