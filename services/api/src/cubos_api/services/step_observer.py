"""Bridge protocol step callbacks onto a run's event stream.

The engine (``cubos.protocol_engine.observer``) defines the callback shape and
guarantees observers cannot break a run; this module supplies the API-side
implementation that turns those callbacks into ``kind="step"`` run events the
Operator UI polls.

Kept here rather than in ``cubos`` so core stays free of API imports -- the
architecture-boundary test enforces that direction.
"""

from __future__ import annotations

import logging
from typing import Optional

from cubos_api.models.runs import StepEventData
from cubos_api.services.run_store import RunStore

logger = logging.getLogger(__name__)


def _label(index: int, command: str, substep: Optional[str]) -> str:
    """Human-readable prose for the event's ``message`` field."""
    if substep:
        return f"step {index} {command} [{substep}]"
    return f"step {index} {command}"


class RunStoreStepObserver:
    """Writes step progress for one run into its ``events.jsonl``.

    Every event carries ``state="running"``: step events report progress
    *within* a run, not run-state transitions, so the run's own lifecycle
    events remain the only source of state changes.
    """

    def __init__(self, store: RunStore, run_id: str) -> None:
        self._store = store
        self._run_id = run_id

    def _emit(
        self,
        *,
        index: int,
        command: str,
        substep: Optional[str],
        outcome: str,
        duration_s: Optional[float] = None,
        error: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        data = StepEventData(
            index=index,
            command=command,
            substep=substep,
            outcome=outcome,
            duration_s=duration_s,
            error=error,
            reason=reason,
        )
        self._store.append_event(
            self._run_id,
            state="running",
            message=f"{_label(index, command, substep)} {outcome}",
            kind="step",
            data=data.model_dump(),
        )

    def step_started(
        self, *, index: int, command: str, substep: Optional[str],
    ) -> None:
        self._emit(
            index=index, command=command, substep=substep, outcome="started",
        )

    def step_completed(
        self,
        *,
        index: int,
        command: str,
        substep: Optional[str],
        duration_s: float,
    ) -> None:
        self._emit(
            index=index,
            command=command,
            substep=substep,
            outcome="completed",
            duration_s=duration_s,
        )

    def step_failed(
        self,
        *,
        index: int,
        command: str,
        substep: Optional[str],
        duration_s: float,
        error: str,
    ) -> None:
        self._emit(
            index=index,
            command=command,
            substep=substep,
            outcome="failed",
            duration_s=duration_s,
            error=error,
        )

    def step_skipped(
        self,
        *,
        index: int,
        command: str,
        substep: Optional[str],
        reason: str,
    ) -> None:
        self._emit(
            index=index,
            command=command,
            substep=substep,
            outcome="skipped",
            reason=reason,
        )


__all__ = ["RunStoreStepObserver"]
