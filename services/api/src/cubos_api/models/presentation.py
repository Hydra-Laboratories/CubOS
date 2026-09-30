"""Versioned, read-only campaign presentation resources."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PresentationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DemoMarkerRequest(PresentationModel):
    label: str = Field(min_length=1, max_length=120)
    client_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    client_time: float | None = None
    timeline_elapsed_ms: int | None = Field(default=None, ge=0, le=604_800_000)
    video_time_ms: int | None = Field(default=None, ge=0, le=604_800_000)
    footage_offset_ms: int | None = Field(
        default=None, ge=-604_800_000, le=604_800_000,
    )
    recording_name: str | None = Field(default=None, min_length=1, max_length=255)
    recording_size: int | None = Field(default=None, ge=0, le=1_099_511_627_776)
    recording_last_modified: float | None = None
    recording_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("label")
    @classmethod
    def clean_label(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("label cannot be blank")
        return cleaned

    @model_validator(mode="after")
    def recording_identity_is_complete(self):
        values = (
            self.recording_name, self.recording_size,
            self.recording_last_modified, self.recording_sha256,
        )
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("recording identity requires name, size, modified time, and SHA-256")
        sync = (self.timeline_elapsed_ms, self.video_time_ms, self.footage_offset_ms)
        if any(value is not None for value in sync):
            if self.timeline_elapsed_ms is None or self.video_time_ms is None:
                raise ValueError("timeline/video sync requires both elapsed and video time")
            expected = self.video_time_ms - self.timeline_elapsed_ms
            if self.footage_offset_ms is None:
                self.footage_offset_ms = expected
            elif self.footage_offset_ms != expected:
                raise ValueError("footage_offset_ms must equal video_time_ms - timeline_elapsed_ms")
        return self


class DemoMarker(PresentationModel):
    id: str
    sequence: int = Field(ge=1)
    server_time: float
    client_time: float | None = None
    timeline_elapsed_ms: int | None = None
    video_time_ms: int | None = None
    footage_offset_ms: int | None = None
    label: str
    client_id: str | None = None
    recording_name: str | None = None
    recording_size: int | None = None
    recording_last_modified: float | None = None
    recording_sha256: str | None = None


class PresentationResponse(PresentationModel):
    schema_version: str = "1"
    campaign_id: str
    status: str
    target: dict[str, Any]
    attempts: list[dict[str, Any]]
    best: dict[str, Any] | None = None
    events: list[dict[str, Any]]
    markers: list[DemoMarker]
    partial: bool
    missing: list[str]
