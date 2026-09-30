"""Crash-readable on-disk storage for versioned CubOS run resources."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from cubos_api.models.runs import RunEvent, RunRecord


INPUT_ARTIFACTS = ("gantry.yaml", "deck.yaml", "protocol.yaml")
OUTPUT_ARTIFACTS = ("result.json", "error.txt", "events.jsonl", "run.json")
ALLOWED_ARTIFACTS = frozenset((*INPUT_ARTIFACTS, *OUTPUT_ARTIFACTS))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


class RunStore:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # Last emitted sequence per run, so appending an event does not have
        # to re-read and re-parse the whole log to number it. A step-level
        # event stream emits ~2 events per protocol step (plus substeps), so
        # the previous count-the-file approach was O(n^2) in events -- on the
        # execution thread, between motion commands. Seeded lazily from disk
        # (see `_next_sequence`) so restarts and crash recovery stay correct;
        # events.jsonl remains the source of truth.
        self._sequence_cache: dict[str, int] = {}
        self._sequence_lock = threading.Lock()

    def run_dir(self, run_id: str) -> Path:
        return self.base_dir / run_id

    def exists(self, run_id: str) -> bool:
        return (self.run_dir(run_id) / "run.json").is_file()

    def create(
        self,
        record: RunRecord,
        *,
        gantry_yaml: str,
        deck_yaml: str,
        protocol_yaml: str,
    ) -> RunRecord:
        directory = self.run_dir(record.run_id)
        directory.mkdir(parents=True, exist_ok=False)
        inputs = {
            "gantry.yaml": gantry_yaml,
            "deck.yaml": deck_yaml,
            "protocol.yaml": protocol_yaml,
        }
        for name, content in inputs.items():
            _atomic_write(directory / name, content)
        record.digests = {
            "gantry_sha256": sha256_text(gantry_yaml),
            "deck_sha256": sha256_text(deck_yaml),
            "protocol_sha256": sha256_text(protocol_yaml),
        }
        record.artifacts = list(INPUT_ARTIFACTS) + ["events.jsonl", "run.json"]
        self.write(record)
        self.append_event(record.run_id, state="queued", message="run accepted")
        return record

    def read(self, run_id: str) -> RunRecord | None:
        path = self.run_dir(run_id) / "run.json"
        if not path.is_file():
            return None
        return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def write(self, record: RunRecord) -> None:
        _atomic_write(
            self.run_dir(record.run_id) / "run.json",
            record.model_dump_json(indent=2) + "\n",
        )

    def _next_sequence(self, run_id: str) -> int:
        """Return the next event sequence number for *run_id*.

        Cached in memory; seeded from the on-disk log the first time a run is
        touched by this process so a restart mid-run continues the numbering
        instead of colliding with existing events.
        """
        cached = self._sequence_cache.get(run_id)
        if cached is None:
            cached = len(self.events(run_id))
        cached += 1
        self._sequence_cache[run_id] = cached
        return cached

    def append_event(
        self,
        run_id: str,
        *,
        state: str,
        message: str,
        kind: str = "lifecycle",
        data: dict[str, Any] | None = None,
    ) -> RunEvent:
        path = self.run_dir(run_id) / "events.jsonl"
        # Sequence assignment and the append share one lock: the step
        # observer runs on the execution thread while the API thread can
        # still append lifecycle events (e.g. a cancel request).
        with self._sequence_lock:
            event = RunEvent(
                sequence=self._next_sequence(run_id),
                timestamp=time.time(),
                state=state,
                message=message,
                kind=kind,
                data=data,
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json() + "\n")
        return event

    def events(self, run_id: str) -> list[RunEvent]:
        path = self.run_dir(run_id) / "events.jsonl"
        if not path.is_file():
            return []
        return [
            RunEvent.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def write_result(self, record: RunRecord, result: Any) -> None:
        path = self.run_dir(record.run_id) / "result.json"
        _atomic_write(path, json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
        if "result.json" not in record.artifacts:
            record.artifacts.append("result.json")

    def write_error(self, record: RunRecord, error: str) -> None:
        _atomic_write(self.run_dir(record.run_id) / "error.txt", error + "\n")
        if "error.txt" not in record.artifacts:
            record.artifacts.append("error.txt")

    def artifact_path(self, run_id: str, name: str) -> Path | None:
        if name not in ALLOWED_ARTIFACTS:
            return None
        path = self.run_dir(run_id) / name
        return path if path.is_file() else None

    def incomplete_records(self) -> Iterable[RunRecord]:
        for path in self.base_dir.glob("*/run.json"):
            try:
                record = RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if record.state in {"queued", "running", "cancel_requested"}:
                yield record
