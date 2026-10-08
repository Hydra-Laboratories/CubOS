"""Operator-authored active-learning campaign specifications and records."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    initial_trials: int = Field(default=3, ge=1, le=100)
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

    @model_validator(mode="after")
    def consistent(self):
        names = [p.name for p in self.parameters]
        if len(set(names)) != len(names):
            raise ValueError("Parameter names must be unique")
        if self.optimizer.initial_trials > self.stop.max_trials:
            raise ValueError("Initial trials must not exceed the trial budget")
        if self.sum_constraint:
            selected = self.sum_constraint.parameters
            if len(set(selected)) != len(selected) or not set(selected) <= set(names):
                raise ValueError("Sum constraint must name distinct campaign parameters")
        if self.mock_mode and self.fluid_state_id is not None:
            raise ValueError("Offline runs cannot modify a real fluid state")
        return self


class CampaignSubmission(CampaignModel):
    spec: CampaignSpec


class Observation(CampaignModel):
    value: float


class CampaignTrial(CampaignModel):
    index: int
    parameters: dict[str, float]
    run_id: str
    state: str = "queued"
    objective: float | None = None
    error: str | None = None


class CampaignRecord(CampaignModel):
    campaign_id: str
    spec: CampaignSpec
    state: Literal["running", "paused", "awaiting_observation", "completed", "stopped", "failed", "interrupted"] = "running"
    created_at: float
    updated_at: float
    active_run_id: str | None = None
    trials: list[CampaignTrial] = Field(default_factory=list)
    best_objective: float | None = None
    stop_reason: str | None = None
    error: str | None = None
    pause_requested: bool = False
    stop_requested: bool = False
