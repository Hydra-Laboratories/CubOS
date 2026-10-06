"""Persistent server-owned queue records for independent color campaigns."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from cubos_api.models.campaigns import CampaignModel, ColorCampaignSetup


QueueState = Literal[
    "prepared", "running", "completed", "failed", "cancelled",
    "interrupted", "blocked",
]
JobState = Literal[
    "pending", "starting", "active", "completed", "failed", "cancelled",
    "interrupted", "blocked",
]


class OvernightQueueJobInput(CampaignModel):
    name: str = Field(min_length=1, max_length=120)
    target_rgb: tuple[float, float, float]
    optimizer_seed: int = Field(default=7, ge=0, le=2**31 - 1)
    color_setup: ColorCampaignSetup

    @field_validator("target_rgb", mode="before")
    @classmethod
    def accept_json_rgb(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def queue_campaign_shape(self):
        if any(value < 0 or value > 255 for value in self.target_rgb):
            raise ValueError("target_rgb values must be between 0 and 255")
        if self.color_setup.target_mode != "rgb":
            raise ValueError("Overnight queue jobs require user-selected RGB targets")
        if self.color_setup.target_rgb != self.target_rgb:
            raise ValueError("Job target_rgb must match color_setup.target_rgb")
        if self.color_setup.batch_size != 3:
            raise ValueError("Overnight queue jobs require batch_size 3")
        if len(self.color_setup.candidate_wells) != 8:
            raise ValueError("Each overnight queue job requires exactly 8 candidate wells")
        if self.color_setup.total_volume_ul != 150.0:
            raise ValueError("Overnight queue jobs require 150 uL samples")
        if (
            self.color_setup.component_min_ul != 25.0
            or self.color_setup.component_max_ul != 100.0
        ):
            raise ValueError("Overnight queue jobs require component bounds 25..100 uL")
        return self


class OvernightQueuePrepare(CampaignModel):
    name: str = Field(min_length=1, max_length=120)
    jobs: list[OvernightQueueJobInput] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def disjoint_resources(self):
        wells = [
            well
            for job in self.jobs
            for well in job.color_setup.candidate_wells
        ]
        if len(set(wells)) != 40:
            raise ValueError("The five jobs require 40 distinct candidate wells")
        if any(well in {"plate.A1", "plate.A2", "plate.A3"} for well in wells):
            raise ValueError("Used wells plate.A1 through plate.A3 cannot be queued")
        state_ids = {job.color_setup.fluid_state_id for job in self.jobs}
        if len(state_ids) != 1:
            raise ValueError("All queue jobs must use the same durable fluid state")
        setups = {
            (job.color_setup.gantry_file, job.color_setup.deck_file,
             job.color_setup.source_protocol_file)
            for job in self.jobs
        }
        if len(setups) != 1:
            raise ValueError("All queue jobs must use the same accepted setup files")
        return self


class OvernightQueueEvent(CampaignModel):
    sequence: int = Field(ge=1)
    timestamp: float
    kind: str = Field(min_length=1, max_length=80)
    job_index: int | None = Field(default=None, ge=0, le=4)
    campaign_id: str | None = None
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class OvernightQueueJob(CampaignModel):
    job_id: str
    index: int = Field(ge=0, le=4)
    name: str
    state: JobState = "pending"
    target_rgb: tuple[float, float, float]
    optimizer_seed: int
    color_setup: ColorCampaignSetup
    campaign_id: str | None = None
    created_at: float
    started_at: float | None = None
    completed_at: float | None = None
    best_objective: float | None = None
    best_parameters: dict[str, float] | None = None
    trials_completed: int = 0
    trials_attempted: int = 0
    unscored_count: int = 0
    stop_reason: str | None = None
    error: str | None = None


class OvernightResourceSummary(CampaignModel):
    campaign_count: int
    sample_count: int
    tip_count: int
    total_volume_ul: float
    maximum_per_stock_ul: float
    candidate_wells: list[str]
    fluid_state_id: int | None
    gantry_file: str
    deck_file: str


class OvernightQueueRecord(CampaignModel):
    queue_id: str
    name: str
    state: QueueState = "prepared"
    created_at: float
    updated_at: float
    started_at: float | None = None
    completed_at: float | None = None
    current_job_index: int | None = None
    stop_reason: str | None = None
    error: str | None = None
    cancel_requested: bool = False
    skip_underexposed: bool = False
    resource_summary: OvernightResourceSummary
    jobs: list[OvernightQueueJob]
    events: list[OvernightQueueEvent] = Field(default_factory=list)
