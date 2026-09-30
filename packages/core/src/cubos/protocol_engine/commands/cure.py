"""Protocol command: cure with a UV-curing instrument at a deck position."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from cubos.instruments.uv_curing.interface import UVCuringInstrument

from ..errors import ProtocolExecutionError
from ..registry import protocol_command
from . import _summaries
from .measure import measure

if TYPE_CHECKING:
    from ..runtime import ProtocolContext


@protocol_command("cure", summary=_summaries.cure)
def cure(
    context: "ProtocolContext",
    instrument: str,
    position: str,
    measurement_height: float,
    exposure_time: float,
    intensity: float | None = None,
) -> Any:
    """Cure at a deck position using a UV-curing instrument.

    Thin wrapper over ``measure`` (same travel/persistence machinery) that
    makes ``exposure_time`` (seconds) and ``intensity`` (percent) first-class
    numeric arguments instead of hiding them inside ``measure``'s free-form
    ``method_kwargs`` -- the operator-web step editor renders typed command
    arguments automatically but has no UI for ``method_kwargs``, so curing
    previously had no way to set exposure time or intensity outside the raw
    YAML panel.

    Args:
        context: Runtime context.
        instrument: Name of a ``uv_curing`` type instrument on the gantry.
        position: Deck position to cure at (see ``measure``).
        measurement_height: Labware-relative Z offset to act at (see ``measure``).
        exposure_time: UV exposure duration in seconds (vendor minimum 0.1s).
        intensity: UV lamp intensity as a percent (1-100). Falls back to the
            instrument's configured default when omitted.
    """
    if instrument not in context.gantry.instruments:
        raise ProtocolExecutionError(
            f"Unknown instrument '{instrument}'. "
            f"Available: {', '.join(sorted(context.gantry.instruments.keys()))}"
        )
    instr = context.gantry.instruments[instrument]
    if not isinstance(instr, UVCuringInstrument):
        raise ProtocolExecutionError(
            f"Instrument {instrument!r} is a {type(instr).__name__}, not a "
            "UVCuringInstrument. cure requires a `uv_curing` type instrument."
        )

    method_kwargs: dict[str, float] = {"exposure_time": exposure_time}
    if intensity is not None:
        method_kwargs["intensity"] = intensity

    return measure(
        context,
        instrument=instrument,
        position=position,
        measurement_height=measurement_height,
        method="cure",
        method_kwargs=method_kwargs,
    )
