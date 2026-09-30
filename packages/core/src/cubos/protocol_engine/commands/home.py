"""Protocol command: home the gantry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..registry import protocol_command
from . import _summaries

if TYPE_CHECKING:
    from ..runtime import ProtocolContext


@protocol_command("home", summary=_summaries.home)
def home(context: "ProtocolContext") -> None:
    """Home the gantry without redefining a calibrated deck-origin WCS.

    Deck-origin configs rely on persistent G54 WPos established by the
    calibration script. This command intentionally does not apply ``G92`` or
    otherwise rewrite work coordinates after homing.

    Args:
        context: Runtime context (instrumented gantry, deck, logger).
    """
    context.logger.info("home: homing gantry")
    gantry = context.gantry.controller
    previous_timeout = None
    if hasattr(gantry, "get_serial_timeout"):
        previous_timeout = gantry.get_serial_timeout()
    gantry.set_serial_timeout(10)
    try:
        gantry.home()
    finally:
        if previous_timeout is not None:
            gantry.set_serial_timeout(previous_timeout)
