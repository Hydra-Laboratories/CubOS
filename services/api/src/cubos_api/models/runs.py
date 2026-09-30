"""Versioned CubOS run-resource request and response models."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field, model_validator

from cubos_api.models.state import RunStateSelection


RunState = Literal[
    "queued",
    "running",
    "cancel_requested",
    "succeeded",
    "failed",
    "cancelled",
]

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class RunSubmission(BaseModel):
    run_id: str | None = None
    gantry_file: str | None = None
    deck_file: str | None = None
    protocol_file: str | None = None
    gantry_config: str | None = None
    deck_config: str | None = None
    protocol_yaml: str | None = None
    mock_mode: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)
    state: RunStateSelection | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "RunSubmission":
        if self.run_id is not None and not _RUN_ID_RE.fullmatch(self.run_id):
            raise ValueError(
                "run_id must start with an alphanumeric character and contain only "
                "letters, numbers, '.', '_' or '-' (maximum 128 characters)"
            )

        files = (self.gantry_file, self.deck_file, self.protocol_file)
        inline = (self.gantry_config, self.deck_config, self.protocol_yaml)
        has_files = any(value is not None for value in files)
        has_inline = any(value is not None for value in inline)
        if has_files == has_inline:
            raise ValueError("provide exactly one complete filename or inline YAML bundle")
        selected = files if has_files else inline
        if not all(isinstance(value, str) and value for value in selected):
            raise ValueError("gantry, deck, and protocol inputs are all required")
        return self


RunEventKind = Literal["lifecycle", "step"]

StepOutcome = Literal["started", "completed", "failed", "skipped"]


class StepEventData(BaseModel):
    """Payload carried by ``kind="step"`` run events.

    ``substep`` is ``None`` for a protocol step itself and a colon-joined
    path (``"leg2"``, ``"cycle0:fill"``) for the nested scopes compound
    commands open, mirroring ``ProtocolContext.active_substep``.
    """

    index: int
    command: str
    substep: str | None = None
    outcome: StepOutcome
    duration_s: float | None = None
    error: str | None = None
    reason: str | None = None


class RunEvent(BaseModel):
    sequence: int
    timestamp: float
    state: RunState
    message: str
    # `kind` and `data` are additive: events written before they existed
    # parse with these defaults, and consumers that do not understand a
    # future kind must ignore it rather than fail.
    kind: RunEventKind = "lifecycle"
    data: Dict[str, Any] | None = None


class RunRecord(BaseModel):
    run_id: str
    state: RunState
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    mock_mode: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)
    digests: Dict[str, str] = Field(default_factory=dict)
    result: Any = None
    error: str | None = None
    artifacts: List[str] = Field(default_factory=list)
    fluid_state_id: int | None = None


class RunEventsResponse(BaseModel):
    run_id: str
    events: List[RunEvent]


class PlanStep(BaseModel):
    """One compiled protocol step, for display before/while it runs."""

    index: int
    command: str
    summary: str
    args: Dict[str, Any] = Field(default_factory=dict)


class RunPlanResponse(BaseModel):
    """The compiled step list for a submitted run.

    Keyed on ``run_id`` rather than a submitted bundle so it stays available
    after a page reload and after the run finishes -- the run's stored
    ``protocol.yaml`` is the source, so the plan is deterministic.
    """

    run_id: str
    steps: List[PlanStep]


class RunArtifactsResponse(BaseModel):
    run_id: str
    artifacts: List[str]
