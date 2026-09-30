"""Versioned, read-only campaign presentation resources."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PresentationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DemoMarkerRequest(PresentationModel):
    label: str = Field(min_length=1, max_length=120)
    client_time: float | None = None
    footage_offset_ms: int | None = Field(default=None, ge=0, le=86_400_000)

    @field_validator("label")
    @classmethod
    def clean_label(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("label cannot be blank")
        return cleaned


class DemoMarker(PresentationModel):
    id: str
    sequence: int = Field(ge=1)
    server_time: float
    client_time: float | None = None
    footage_offset_ms: int | None = None
    label: str


class PresentationResponse(PresentationModel):
    schema_version: str = "cubos.campaign-presentation.v1"
    campaign_id: str
    status: str
    target: dict[str, Any]
    attempts: list[dict[str, Any]]
    best: dict[str, Any] | None = None
    events: list[dict[str, Any]]
    markers: list[DemoMarker]
    partial: bool
    missing: list[str]
