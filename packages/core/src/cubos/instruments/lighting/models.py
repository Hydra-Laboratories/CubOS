"""Lighting instrument data models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LightingStatus(BaseModel):
    """Current lighting state: brightness percentage per channel (0 = off).

    Pawduino firmware has no readback for light state, so vendors shadow it
    in memory; the shadow resets to all-off whenever the board resets
    (opening the serial port toggles DTR, which physically turns the lights
    off), keeping shadow and hardware in agreement.
    """

    model_config = ConfigDict(extra="forbid")

    channels: dict[str, int] = Field(
        default_factory=dict,
        description="Mapping of channel name to active brightness pct (0 = off).",
    )
