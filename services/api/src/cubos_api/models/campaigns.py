"""Operator-authored active-learning campaign specifications and records."""
from __future__ import annotations

import math
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CampaignModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class Binding(CampaignModel):
    step_index: int = Field(ge=0)
    argument: str = Field(min_length=1, max_length=160)


class Parameter(CampaignModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
    minimum: float
    maximum: float
    step: float = Field(gt=0)
    bindings: list[Binding] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def bounds(self):
        if self.maximum <= self.minimum:
            raise ValueError("maximum must be greater than minimum")
        if self.step > self.maximum - self.minimum:
            raise ValueError("step must fit within parameter bounds")
        return self


class TargetSequence(CampaignModel):
    name: str = Field(min_length=1, max_length=80)
    values: list[str] = Field(min_length=1, max_length=100)
    bindings: list[Binding] = Field(min_length=1, max_length=32)


class Objective(CampaignModel):
    mode: Literal["result", "manual"] = "result"
    path: str = Field(default="", max_length=256)
    direction: Literal["minimize", "maximize"] = "minimize"


class Optimizer(CampaignModel):
    method: Literal["ei", "lcb", "random"] = "ei"
    kernel: Literal["matern52", "rbf"] = "matern52"
    initial_trials: int = Field(default=3, ge=1, le=100)
    initial_points: list[dict[str, float]] = Field(default_factory=list, max_length=100)
    exploration: float = Field(default=0.05, ge=0, le=10)
    seed: int = Field(default=7, ge=0, le=2**31-1)


class StopConditions(CampaignModel):
    max_trials: int = Field(default=20, ge=1, le=100)
    target_value: float | None = None
    patience: int = Field(default=0, ge=0, le=100)
    min_improvement: float = Field(default=0, ge=0)
    max_seconds: float | None = Field(default=None, gt=0, le=604800)


class SumConstraint(CampaignModel):
    parameters: list[str] = Field(min_length=2, max_length=8)
    total: float


class CampaignSpec(CampaignModel):
    name: str = Field(min_length=1, max_length=120)
    gantry_file: str = Field(min_length=1, max_length=255)
    deck_file: str = Field(min_length=1, max_length=255)
    protocol_file: str = Field(min_length=1, max_length=255)
    parameters: list[Parameter] = Field(min_length=1, max_length=8)
    sequences: list[TargetSequence] = Field(default_factory=list, max_length=32)
    objective: Objective = Field(default_factory=Objective)
    optimizer: Optimizer = Field(default_factory=Optimizer)
    stop: StopConditions = Field(default_factory=StopConditions)
    sum_constraint: SumConstraint | None = None
    mock_mode: bool = False
    fluid_state_id: int | None = Field(default=None, gt=0)
    batch_size: int = Field(default=1, ge=1, le=6)
    source_protocol_file: str | None = Field(default=None, min_length=1, max_length=255)
    target_mode: Literal["camera", "rgb"] = "camera"
    target_rgb: tuple[float, float, float] | None = None
    # Immutable target provenance for presentation/export. Optional so records
    # written before this evidence link existed remain readable.
    target_run_id: str | None = Field(default=None, min_length=1, max_length=160)
    target_analysis_revision: int | None = Field(default=None, ge=0)
    target_well: str | None = Field(default=None, min_length=1, max_length=160)
    target_lab: tuple[float, float, float] | None = None
    reference_processing_profile_id: str | None = Field(
        default=None, min_length=1, max_length=128,
    )

    @field_validator("target_rgb", "target_lab", mode="before")
    @classmethod
    def accept_json_target_rgb(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def consistent(self):
        names = [p.name for p in self.parameters]
        if len(set(names)) != len(names):
            raise ValueError("Parameter names must be unique")
        if self.optimizer.initial_trials > self.stop.max_trials:
            raise ValueError("Initial trials must not exceed the trial budget")
        if len(self.optimizer.initial_points) > self.optimizer.initial_trials:
            raise ValueError("Initial design must fit within initial_trials")
        if self.sum_constraint:
            selected = self.sum_constraint.parameters
            if len(set(selected)) != len(selected) or not set(selected) <= set(names):
                raise ValueError("Sum constraint must name distinct campaign parameters")
        if self.mock_mode and self.fluid_state_id is not None:
            raise ValueError("Offline runs cannot modify a real fluid state")
        if self.batch_size > 1 and self.source_protocol_file is None:
            raise ValueError("Batched color campaigns require a source protocol file")
        if self.target_rgb is not None and any(
            value < 0 or value > 255 for value in self.target_rgb
        ):
            raise ValueError("target_rgb values must be between 0 and 255")
        if self.target_mode == "rgb" and self.target_rgb is None:
            raise ValueError("RGB campaigns require target_rgb")
        if self.target_mode == "rgb" and any((
            self.target_run_id is not None,
            self.target_analysis_revision is not None,
            self.target_lab is not None,
            self.reference_processing_profile_id is not None,
        )):
            raise ValueError("RGB campaigns cannot include camera-target provenance")
        linked_target = (self.target_run_id, self.target_analysis_revision)
        if self.target_mode == "camera" and any(
            value is not None for value in linked_target
        ) and not all(value is not None for value in linked_target):
            raise ValueError(
                "Camera-target provenance must include both run and revision"
            )
        if self.target_mode == "camera" and self.target_run_id is not None and (
            self.target_lab is None or self.reference_processing_profile_id is None
        ):
            raise ValueError("Linked camera targets require Lab and processing profile")
        return self


class CampaignSubmission(CampaignModel):
    spec: CampaignSpec


class PendingBatch(CampaignModel):
    """Exact compiled batch retained while inventory is being replenished."""

    batch_index: int = Field(ge=1)
    parameters: list[dict[str, float]] = Field(min_length=1, max_length=6)
    protocol_yaml: str = Field(min_length=1)
    objective_paths: list[str] = Field(min_length=1, max_length=6)
    sample_map: list[dict[str, Any]] = Field(min_length=1, max_length=6)
    run_id: str | None = None


class RefillRequirement(CampaignModel):
    target: str = Field(min_length=1, max_length=160)
    available_ul: float = Field(ge=0)
    required_ul: float = Field(ge=0)
    capacity_ul: float = Field(ge=0)


class Observation(CampaignModel):
    value: float


class ColorTargetRequest(CampaignModel):
    gantry_file: str = Field(min_length=1, max_length=255)
    deck_file: str = Field(min_length=1, max_length=255)
    target_well: str = Field(default="plate.A1", min_length=1, max_length=160)
    camera_instrument: str = Field(default="camera", min_length=1, max_length=80)
    roi_fraction: float = Field(default=0.5, gt=0, le=1)
    expected_center: tuple[float, float] | None = None
    expected_center_source: Literal["operator_selected"] | None = None
    image_height: float | None = None
    mock_mode: bool = False

    @field_validator("expected_center", mode="before")
    @classmethod
    def accept_json_center_pair(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def center_is_complete(self):
        if (self.expected_center is None) != (self.expected_center_source is None):
            raise ValueError("expected_center and expected_center_source must be supplied together")
        if self.expected_center is not None and any(
            value < 0 or value > 1 for value in self.expected_center
        ):
            raise ValueError("expected_center values must be between 0 and 1")
        return self


class ColorCampaignSetup(CampaignModel):
    gantry_file: str = Field(min_length=1, max_length=255)
    deck_file: str = Field(min_length=1, max_length=255)
    source_protocol_file: str = Field(min_length=1, max_length=255)
    batch_size: int = Field(default=1, ge=1, le=6)
    target_mode: Literal["camera", "rgb"] = "camera"
    target_run_id: str | None = Field(default=None, min_length=1, max_length=160)
    target_analysis_revision: int | None = Field(default=None, ge=0)
    target_well: str = Field(default="plate.A1", min_length=1, max_length=160)
    target_rgb: tuple[float, float, float] | None = None
    target_lab: tuple[float, float, float] | None = None
    reference_processing_profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    reference_origin: Literal["accepted_camera_measurement", "user_selected_srgb"] | None = None
    red_source: str = Field(default="stocks.A1", min_length=1, max_length=160)
    yellow_source: str = Field(default="stocks.A2", min_length=1, max_length=160)
    blue_source: str = Field(default="stocks.A3", min_length=1, max_length=160)
    diluent_source: str | None = Field(default=None, min_length=1, max_length=160)
    component_min_ul: float = Field(default=50.0, ge=5)
    component_max_ul: float = Field(default=200.0)
    candidate_wells: list[str] = Field(min_length=6, max_length=96)
    camera_instrument: str = Field(default="camera", min_length=1, max_length=80)
    roi_fraction: float = Field(default=0.5, gt=0, le=1)
    expected_center: tuple[float, float] | None = None
    expected_center_source: Literal["operator_selected", "frame_center"] | None = None
    image_height: float | None = None
    photo_position: tuple[float, float, float] | None = None
    fluid_state_id: int | None = Field(default=None, gt=0)
    mock_mode: bool = False

    @field_validator("target_rgb", "target_lab", "expected_center", "photo_position", mode="before")
    @classmethod
    def accept_json_tuple(cls, value):
        # JSON has arrays rather than tuples. Normalize the browser payload before
        # strict validation while retaining a fixed-length tuple in the model.
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("photo_position")
    @classmethod
    def finite_photo_position(cls, value):
        if value is not None and any(not math.isfinite(coordinate) for coordinate in value):
            raise ValueError("photo_position coordinates must be finite")
        return value

    @model_validator(mode="after")
    def unique_resources(self):
        if len(set(self.candidate_wells)) != len(self.candidate_wells):
            raise ValueError("Candidate wells must be unique")
        if self.target_mode == "camera" and self.target_well in self.candidate_wells:
            raise ValueError("Target well cannot also be a candidate well")
        component_count = 4 if self.diluent_source is not None else 3
        if self.batch_size == 1:
            required_tips = len(self.candidate_wells) * component_count
        else:
            required_tips = sum(
                component_count + min(self.batch_size, len(self.candidate_wells) - start)
                for start in range(0, len(self.candidate_wells), self.batch_size)
            )
        if required_tips > 96:
            raise ValueError(
                f"Color matching requires {required_tips} fresh tips for "
                f"{len(self.candidate_wells)} candidates with batch size {self.batch_size}; "
                "a tip rack holds 96"
            )
        if self.diluent_source is not None:
            if self.component_max_ul < self.component_min_ul:
                raise ValueError(
                    "component_max_ul must be greater than or equal to component_min_ul"
                )
            if not math.isclose(
                self.component_min_ul, round(self.component_min_ul / 5.0) * 5.0,
                abs_tol=1e-9,
            ):
                raise ValueError(
                    "component_min_ul must be a multiple of the 5 µL dosing step "
                    "so every component lands on the shared volume grid"
                )
            if 3 * self.component_min_ul + 5 > 300:
                raise ValueError(
                    "component_min_ul leaves no room for at least 5 µL of water "
                    "once red, yellow, and blue are all at their minimum"
                )
        if self.expected_center is not None and any(
            value < 0 or value > 1 for value in self.expected_center
        ):
            raise ValueError("expected_center values must be between 0 and 1")
        if self.target_rgb is not None and any(
            value < 0 or value > 255 for value in self.target_rgb
        ):
            raise ValueError("target_rgb values must be between 0 and 255")
        if self.target_mode == "camera":
            if self.target_rgb is not None:
                raise ValueError("Camera targets cannot include target_rgb")
            if self.expected_center is None or self.expected_center_source != "operator_selected":
                raise ValueError(
                    "Camera targets require an operator-selected expected center"
                )
        else:
            if self.target_rgb is None:
                raise ValueError("RGB targets require target_rgb")
            if self.target_run_id is not None or self.target_analysis_revision is not None:
                raise ValueError("RGB targets cannot include camera target-run evidence")
            if self.reference_processing_profile_id is not None:
                raise ValueError("RGB targets cannot include a camera processing profile")
            if self.expected_center is None:
                self.expected_center = (0.5, 0.5)
            if self.expected_center_source is None:
                self.expected_center_source = "frame_center"
            if self.expected_center_source != "frame_center":
                raise ValueError("RGB targets require a frame-center expected center")
        if not self.mock_mode and self.fluid_state_id is None:
            raise ValueError("A real color campaign requires a durable fluid-state ID")
        if self.target_mode == "camera" and not self.mock_mode and (
            self.target_run_id is None or self.target_analysis_revision is None
        ):
            raise ValueError(
                "A real color campaign requires an accepted target run and analysis revision"
            )
        return self


class ColorRgbPreviewRequest(CampaignModel):
    rgb: tuple[float, float, float]

    @field_validator("rgb", mode="before")
    @classmethod
    def accept_json_rgb_tuple(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def rgb_in_srgb_range(self):
        if any(value < 0 or value > 255 for value in self.rgb):
            raise ValueError("rgb values must be between 0 and 255")
        return self


class ColorTargetReanalysisRequest(CampaignModel):
    expected_center: tuple[float, float]
    expected_center_source: Literal["operator_selected"] = "operator_selected"

    @field_validator("expected_center", mode="before")
    @classmethod
    def accept_json_center_pair(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def center_in_normalized_frame(self):
        if any(value < 0 or value > 1 for value in self.expected_center):
            raise ValueError("expected_center values must be between 0 and 1")
        return self


class CampaignTrial(CampaignModel):
    index: int
    parameters: dict[str, float]
    run_id: str
    state: str = "queued"
    objective: float | None = None
    measurement: dict[str, Any] | None = None
    objective_status: Literal["pending", "accepted", "unverified", "rejected"] = "pending"
    error: str | None = None
    objective_path: str | None = None
    sample_well: str | None = None
    batch_index: int | None = None


class CampaignStateBinding(CampaignModel):
    fluid_state_id: int = Field(gt=0)
    reconciliation_note: str = Field(min_length=1, max_length=1000)


class ColorCampaignPresetDraft(CampaignModel):
    source_protocol_file: str | None = Field(default=None, min_length=1, max_length=255)
    batch_size: int = Field(default=1, ge=1, le=6)
    target_mode: Literal["camera", "rgb"] = "camera"
    target_well: str = Field(default="plate.A1", min_length=1, max_length=160)
    target_rgb: tuple[float, float, float] | None = None
    red_source: str = Field(default="stocks.A1", min_length=1, max_length=160)
    yellow_source: str = Field(default="stocks.A2", min_length=1, max_length=160)
    blue_source: str = Field(default="stocks.A3", min_length=1, max_length=160)
    diluent_source: str | None = Field(default=None, min_length=1, max_length=160)
    component_min_ul: float = Field(default=50.0, ge=5)
    component_max_ul: float = Field(default=200.0)
    candidate_wells: list[str] = Field(min_length=1, max_length=96)
    camera_instrument: str = Field(default="camera", min_length=1, max_length=80)
    roi_fraction: float = Field(default=0.5, gt=0, le=1)
    image_height: float | None = None
    photo_position: tuple[float, float, float] | None = None

    @field_validator("target_rgb", "photo_position", mode="before")
    @classmethod
    def accept_json_rgb_tuple(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @field_validator("photo_position")
    @classmethod
    def finite_photo_position(cls, value):
        if value is not None and any(not math.isfinite(coordinate) for coordinate in value):
            raise ValueError("photo_position coordinates must be finite")
        return value

    @field_validator("candidate_wells")
    @classmethod
    def unique_candidate_wells(cls, value: list[str]):
        if len(set(value)) != len(value):
            raise ValueError("Candidate wells must be unique")
        return value

    @model_validator(mode="after")
    def target_is_not_a_candidate(self):
        if self.target_mode == "camera" and self.target_well in self.candidate_wells:
            raise ValueError("Target well cannot also be a candidate well")
        if self.target_rgb is not None and any(
            value < 0 or value > 255 for value in self.target_rgb
        ):
            raise ValueError("target_rgb values must be between 0 and 255")
        if self.target_mode == "camera" and self.target_rgb is not None:
            raise ValueError("Camera targets cannot include target_rgb")
        if self.target_mode == "rgb" and self.target_rgb is None:
            raise ValueError("RGB targets require target_rgb")
        if self.diluent_source is not None and self.component_max_ul < self.component_min_ul:
            raise ValueError(
                "component_max_ul must be greater than or equal to component_min_ul"
            )
        return self


class CampaignPresetSaveRequest(CampaignModel):
    name: str = Field(min_length=1, max_length=120)
    spec: CampaignSpec
    color_setup: ColorCampaignPresetDraft | None = None


class CampaignPresetDocument(CampaignModel):
    schema_version: Literal["cubos.campaign-preset.v1"] = "cubos.campaign-preset.v1"
    name: str = Field(min_length=1, max_length=120)
    spec: CampaignSpec
    color_setup: ColorCampaignPresetDraft | None = None
    requires_fresh_state: Literal[True] = True
    requires_fresh_target: Literal[True] = True

    @model_validator(mode="after")
    def excludes_runtime_evidence(self):
        if self.spec.fluid_state_id is not None:
            raise ValueError("Campaign presets cannot retain a fluid-state ID")
        return self


class CampaignPresetResponse(CampaignModel):
    filename: str
    preset: CampaignPresetDocument


class CampaignPresetSummary(CampaignModel):
    filename: str
    name: str
    modified_at: float


class CampaignRecord(CampaignModel):
    campaign_id: str
    spec: CampaignSpec
    state: Literal["running", "paused", "awaiting_observation", "awaiting_refill", "completed", "stopped", "failed", "interrupted"] = "running"
    created_at: float
    updated_at: float
    active_run_id: str | None = None
    trials: list[CampaignTrial] = Field(default_factory=list)
    best_objective: float | None = None
    stop_reason: str | None = None
    error: str | None = None
    fluid_state_reconciliation_note: str | None = None
    pause_requested: bool = False
    stop_requested: bool = False
    pause_reason: Literal["operator", "inventory_refill"] | None = None
    pending_batch: PendingBatch | None = None
    refill_requirements: list[RefillRequirement] = Field(default_factory=list)
