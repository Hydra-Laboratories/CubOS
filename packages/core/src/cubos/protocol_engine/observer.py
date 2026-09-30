"""Step-execution observability seam.

A :class:`StepObserver` receives one callback per protocol step (and per
substep of a compound command) as the protocol runs. It exists so callers
outside the engine -- the API's run-event stream, a CLI progress renderer --
can report *where a run is* without polling or scraping logs.

Safety contract
---------------
Observers are advisory. Every hook is dispatched through :func:`notify`,
which swallows and logs any exception the observer raises. An observability
callback must never be able to abort a hardware run mid-motion, so a broken
or disconnected consumer degrades to "no progress reporting" rather than
"protocol stops with liquid in the tip".

For the same reason, hooks must be cheap and non-blocking: they run on the
execution thread, between motion commands.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class StepObserver(Protocol):
    """Receives progress callbacks during :meth:`Protocol.execute`.

    ``substep`` is ``None`` for the step itself and a colon-joined path
    (e.g. ``"leg2"``, ``"cycle0:fill"``) for the nested scopes that compound
    commands such as ``serial_transfer`` and ``flush_pipette`` open. It
    mirrors ``ProtocolContext.active_substep``.
    """

    def step_started(
        self, *, index: int, command: str, substep: Optional[str],
    ) -> None:
        ...

    def step_completed(
        self,
        *,
        index: int,
        command: str,
        substep: Optional[str],
        duration_s: float,
    ) -> None:
        ...

    def step_failed(
        self,
        *,
        index: int,
        command: str,
        substep: Optional[str],
        duration_s: float,
        error: str,
    ) -> None:
        ...

    def step_skipped(
        self,
        *,
        index: int,
        command: str,
        substep: Optional[str],
        reason: str,
    ) -> None:
        ...


def notify(observer: Any, hook: str, /, **kwargs: Any) -> None:
    """Dispatch *hook* on *observer*, never raising.

    A ``None`` observer is a no-op. Any exception raised by the observer --
    including a missing hook on a partial implementation -- is logged at
    WARNING and discarded, so protocol execution is unaffected.
    """
    if observer is None:
        return
    try:
        getattr(observer, hook)(**kwargs)
    except Exception:  # noqa: BLE001 - deliberate: observers cannot break runs
        logger.warning(
            "Step observer hook %r failed; continuing run", hook, exc_info=True,
        )


__all__ = ["StepObserver", "notify"]
