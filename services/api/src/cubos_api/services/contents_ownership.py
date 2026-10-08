"""Process-wide ownership gate for the persistent physical setup.

The data-store transaction protects persistence, while this gate protects the
workflow boundary around it.  A stateful run owns the contents from admission
through terminal cleanup; manual edits and active-setup changes hold the same
gate for their complete transaction.  The lock is deliberately process-local:
CubOS has one API process and SQLite remains the durable source of truth.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


class ContentsOwnershipError(RuntimeError):
    """Raised when a contents mutation races with an owning run/campaign."""


@dataclass(frozen=True)
class ContentsOwner:
    kind: str
    owner_id: str
    fluid_state_id: int | None = None
    campaign_id: int | None = None


class ContentsOwnership:
    """Serialize setup selection/edit transactions against stateful runs."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._owner: ContentsOwner | None = None

    def snapshot(self) -> ContentsOwner | None:
        with self._lock:
            return self._owner

    def claim_run(self, run_id: str) -> None:
        """Reserve ownership before a stateful run resolves its setup."""
        with self._lock:
            if self._owner is not None:
                raise ContentsOwnershipError(self._busy_message())
            self._owner = ContentsOwner(kind="run", owner_id=run_id)

    def bind_fluid_state(self, run_id: str, fluid_state_id: int) -> None:
        with self._lock:
            owner = self._require_run(run_id)
            self._owner = ContentsOwner(
                kind=owner.kind,
                owner_id=owner.owner_id,
                fluid_state_id=int(fluid_state_id),
                campaign_id=owner.campaign_id,
            )

    def bind_campaign(self, run_id: str, campaign_id: int | None) -> None:
        """Record the durable campaign once core creates it for the run."""
        with self._lock:
            owner = self._require_run(run_id)
            self._owner = ContentsOwner(
                kind="campaign" if campaign_id is not None else owner.kind,
                owner_id=owner.owner_id,
                fluid_state_id=owner.fluid_state_id,
                campaign_id=None if campaign_id is None else int(campaign_id),
            )

    def release(self, owner_id: str) -> None:
        """Release ownership only for the matching run (idempotent cleanup)."""
        with self._lock:
            if self._owner is not None and self._owner.owner_id == owner_id:
                self._owner = None

    @contextmanager
    def manual_transaction(self) -> Iterator[None]:
        """Hold the gate for the full active-selection/edit transaction."""
        with self._lock:
            if self._owner is not None:
                raise ContentsOwnershipError(self._busy_message())
            yield

    def _require_run(self, run_id: str) -> ContentsOwner:
        owner = self._owner
        if owner is None or owner.owner_id != run_id:
            raise ContentsOwnershipError(
                f"run {run_id!r} does not own the persistent contents"
            )
        return owner

    def _busy_message(self) -> str:
        assert self._owner is not None
        owner = self._owner
        label = f"{owner.kind} {owner.owner_id!r}"
        if owner.campaign_id is not None:
            label += f" (campaign {owner.campaign_id})"
        return f"persistent contents are owned by {label}"


_contents_ownership = ContentsOwnership()


def get_contents_ownership() -> ContentsOwnership:
    """Return the API-process singleton used by routers and RunManager."""
    return _contents_ownership


def reset_contents_ownership() -> None:
    """Reset the singleton for app/test shutdown; never called during a run."""
    with _contents_ownership._lock:  # noqa: SLF001 - test/app lifecycle hook
        _contents_ownership._owner = None  # noqa: SLF001
