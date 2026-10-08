"""Shared instrument YAML schemas for machine configuration."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


_RELOCATED_HEIGHT_FIELDS = {
    "measurement_height": (
        "`measurement_height` is no longer an instrument-config field — "
        "it is a first-class argument on the protocol `scan` and `measure` "
        "commands. Move it from `instruments.<name>.measurement_height` "
        "in the gantry YAML to the protocol step."
    ),
    "interwell_scan_height": (
        "`interwell_scan_height` is no longer an instrument-config field — "
        "it is a first-class argument on the protocol `scan` command. "
        "Move it from `instruments.<name>.interwell_scan_height` in the "
        "gantry YAML to the protocol step."
    ),
}


class MotionVector3Yaml(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    x: float
    y: float
    z: float


class MotionBoxYaml(BaseModel):
    """Tool box relative to the instrument's calibrated bare TCP."""

    model_config = ConfigDict(extra="forbid")

    offset: MotionVector3Yaml
    size: MotionVector3Yaml

    @model_validator(mode="after")
    def _validate_positive_size(self) -> "MotionBoxYaml":
        if self.size.x <= 0 or self.size.y <= 0 or self.size.z <= 0:
            raise ValueError("motion_envelope.box size x/y/z must all be positive.")
        return self


class ToolMotionEnvelopeYaml(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    box: MotionBoxYaml
    additional_boxes: List[MotionBoxYaml] = Field(default_factory=list)
    attached_tip_radius_mm: Optional[float] = Field(default=None, gt=0)


class InstrumentYamlEntry(BaseModel):
    """Schema for one gantry-mounted instrument.

    Common fields are declared explicitly. Driver-specific fields
    (e.g. serial_number, dll_path) pass through via extra="allow".

    Z semantics
    -----------
    Instruments declare only their physical mounting (``offset_x``,
    ``offset_y``, ``depth``). Labware-relative motion heights live on the
    protocol commands that engage with labware:

    * ``measurement_height`` — first-class arg to ``measure`` and ``scan``.
    * ``interwell_scan_height`` — first-class arg to ``scan``.

    Inter-labware travel uses the gantry-level ``safe_z`` (absolute).

    Stale ``measurement_height``/``interwell_scan_height`` keys are
    rejected explicitly — the ``extra="allow"`` policy would otherwise
    silently swallow them for drivers that accept ``**kwargs``.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    vendor: str
    offset_x: float = 0.0
    offset_y: float = 0.0
    depth: float = 0.0
    motion_envelope: Optional[ToolMotionEnvelopeYaml] = None

    @model_validator(mode="before")
    @classmethod
    def _reject_relocated_height_fields(cls, data):
        if isinstance(data, dict):
            for key, hint in _RELOCATED_HEIGHT_FIELDS.items():
                if key in data:
                    raise ValueError(hint)
        return data
