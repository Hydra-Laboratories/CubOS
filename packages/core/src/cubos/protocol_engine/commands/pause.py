"""Protocol commands: pause and breakpoint."""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

from ..registry import protocol_command
from . import _summaries

if TYPE_CHECKING:
    from ..runtime import ProtocolContext


@protocol_command("pause", summary=_summaries.pause)
def pause(
    context: ProtocolContext,
    seconds: float,
    reason: str = "",
) -> None:
    """Pause protocol execution for a fixed duration.

    Args:
        context: Runtime context.
        seconds: Duration to pause in seconds.
        reason:  Optional reason for the pause (logged).
    """
    msg = f"Pausing for {seconds}s"
    if reason:
        msg += f" ({reason})"
    context.logger.info(msg)
    time.sleep(seconds)


@protocol_command("photo_pause", summary=_summaries.pause)
def photo_pause(
    context: ProtocolContext,
    settle_seconds: float,
    capture_hold_seconds: float,
    well: str,
) -> None:
    """Hold a stable photo pose, then announce that settling completed."""
    context.logger.info("Settling photo pose for %ss (%s)", settle_seconds, well)
    time.sleep(settle_seconds)
    context.notify_step(
        "photo_pause_completed",
        seconds=settle_seconds,
        capture_hold_seconds=capture_hold_seconds,
        well=well,
    )
    context.logger.info("Holding photo pose for capture for %ss (%s)", capture_hold_seconds, well)
    time.sleep(capture_hold_seconds)


@protocol_command("breakpoint", summary=_summaries.breakpoint_cmd)
def breakpoint_cmd(
    context: ProtocolContext,
    message: str = "Press Enter to continue...",
) -> None:
    """Halt protocol execution until the user presses Enter.

    In non-interactive/headless runs, log a warning and continue instead
    of holding hardware resources forever.

    Args:
        context: Runtime context.
        message: Prompt message displayed to the user.
    """
    context.logger.info("Breakpoint: %s", message)
    if not sys.stdin.isatty():
        context.logger.warning(
            "Breakpoint skipped because stdin is not interactive: %s", message,
        )
        return
    input(message)
