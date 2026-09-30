"""Runtime types shared by executable protocol commands."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from cubos.deck.deck import Deck
from cubos.gantry.gantry_config import GantryConfig
from cubos.gantry.instrument_mount import InstrumentedGantry
from cubos.protocol_engine.observer import StepObserver, notify


@dataclass
class ProtocolContext:
    """Runtime context injected into every command handler.

    Provides access to the instrumented gantry and the Deck
    (labware target resolution).  Optionally carries a DataStore for
    persisting measurements and a campaign_id for the current run.
    """

    gantry: InstrumentedGantry
    deck: Deck
    positions: Dict[str, Any] = field(default_factory=dict)
    gantry_config: Optional[GantryConfig] = None
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("protocol"),
    )
    data_store: Any = None
    campaign_id: int | None = None
    fluid_state_id: int | None = None
    active_step_index: int | None = None
    active_step_command: str | None = None
    active_substep: str | None = None
    step_observer: Optional[StepObserver] = None
    _manual_operation_sequence: int = 0

    def notify_step(self, hook: str, /, **kwargs: Any) -> None:
        """Emit *hook* to the configured observer, scoped to the active step.

        A no-op when no observer is attached or when called outside
        ``Protocol.execute`` (no active step). ``index``/``command``/
        ``substep`` are filled in from the current scope; callers pass only
        the hook-specific fields. Never raises -- see
        ``cubos.protocol_engine.observer``.
        """
        if self.step_observer is None or self.active_step_index is None:
            return
        notify(
            self.step_observer,
            hook,
            index=self.active_step_index,
            command=self.active_step_command or "command",
            substep=self.active_substep,
            **kwargs,
        )

    def fluid_operation_key(self, action: str) -> str:
        """Return a stable key for one persisted fluid operation.

        Protocol-driven keys are deterministic within a campaign so an
        already-applied transfer can be recognized instead of replayed.
        Direct command calls outside ``Protocol.execute`` receive a monotonic
        manual suffix scoped to this context.
        """
        if self.campaign_id is None:
            raise ValueError("Fluid operations require a campaign_id")

        if self.active_step_index is None:
            self._manual_operation_sequence += 1
            scope = f"manual:{self._manual_operation_sequence}"
        else:
            command = self.active_step_command or "command"
            scope = f"step:{self.active_step_index}:{command}"

        if self.active_substep is not None:
            scope = f"{scope}:substep:{self.active_substep}"
        return f"campaign:{self.campaign_id}:{scope}:{action}"


@dataclass
class ProtocolStep:
    """One compiled, executable protocol step."""

    index: int
    command_name: str
    handler: Callable[..., Any]
    args: Dict[str, Any]

    def execute(self, context: ProtocolContext) -> Any:
        """Run this step, passing *context* and unpacked *args* to the handler."""
        context.logger.info(
            "Step %d: %s(%s)",
            self.index,
            self.command_name,
            ", ".join(f"{k}={v!r}" for k, v in self.args.items()),
        )
        previous_index = context.active_step_index
        previous_command = context.active_step_command
        previous_substep = context.active_substep
        context.active_step_index = self.index
        context.active_step_command = self.command_name
        context.active_substep = None
        started = time.monotonic()
        context.notify_step("step_started")
        try:
            result = self.handler(context, **self.args)
        except BaseException as exc:
            context.notify_step(
                "step_failed",
                duration_s=time.monotonic() - started,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        else:
            context.notify_step(
                "step_completed", duration_s=time.monotonic() - started,
            )
            return result
        finally:
            context.active_step_index = previous_index
            context.active_step_command = previous_command
            context.active_substep = previous_substep
