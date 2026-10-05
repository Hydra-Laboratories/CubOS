"""Crash-readable on-disk storage for versioned CubOS run resources."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from cubos_api.models.runs import RunEvent, RunRecord


INPUT_ARTIFACTS = ("gantry.yaml", "deck.yaml", "protocol.yaml")
OUTPUT_ARTIFACTS = ("result.json", "error.txt", "events.jsonl", "run.json")
ALLOWED_ARTIFACTS = frozenset((*INPUT_ARTIFACTS, *OUTPUT_ARTIFACTS))
COLOR_TARGET_ARTIFACT = re.compile(r"^color-target-analysis-\d+\.(?:json|png)$")
COLOR_TARGET_SOURCE_ARTIFACT = "color-target-source.tiff"


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
        self._artifact_lock = threading.RLock()

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
        timestamp: float | None = None,
    ) -> RunEvent:
        path = self.run_dir(run_id) / "events.jsonl"
        # Sequence assignment and the append share one lock: the step
        # observer runs on the execution thread while the API thread can
        # still append lifecycle events (e.g. a cancel request).
        with self._sequence_lock:
            event = RunEvent(
                sequence=self._next_sequence(run_id),
                timestamp=time.time() if timestamp is None else timestamp,
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
        if (
            name not in ALLOWED_ARTIFACTS
            and name != COLOR_TARGET_SOURCE_ARTIFACT
            and COLOR_TARGET_ARTIFACT.fullmatch(name) is None
        ):
            return None
        path = self.run_dir(run_id) / name
        return path if path.is_file() else None

    def freeze_color_target_source(
        self,
        record: RunRecord,
        source: Path,
        *,
        allowed_root: Path,
        capture_sha256: str,
        initial_analysis: dict[str, Any],
        annotated_preview: Path,
    ) -> None:
        """Copy and hash the captured target frame into the immutable run store."""
        if re.fullmatch(r"[0-9a-f]{64}", capture_sha256) is None:
            raise ValueError("Capture-time image digest must be lowercase SHA-256")
        resolved_source = source.expanduser().resolve()
        resolved_root = allowed_root.expanduser().resolve()
        try:
            resolved_source.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError("Color target image is outside the configured image root") from exc
        if not resolved_source.is_file():
            raise FileNotFoundError(f"Color target image was not found: {resolved_source}")
        resolved_preview = annotated_preview.expanduser().resolve()
        try:
            resolved_preview.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError("Color target annotation is outside the configured image root") from exc
        if not resolved_preview.is_file():
            raise FileNotFoundError(
                f"Color target annotation was not found: {resolved_preview}"
            )
        with self._artifact_lock:
            destination = self.run_dir(record.run_id) / COLOR_TARGET_SOURCE_ARTIFACT
            analysis_json = self.run_dir(record.run_id) / "color-target-analysis-0.json"
            analysis_image = self.run_dir(record.run_id) / "color-target-analysis-0.png"
            if destination.exists() or analysis_json.exists() or analysis_image.exists():
                raise FileExistsError("Immutable color-target source already exists")
            source_digest = hashlib.sha256(resolved_source.read_bytes()).hexdigest()
            if source_digest != capture_sha256:
                raise ValueError(
                    "Color target image no longer matches its capture-time digest"
                )
            temporary = destination.with_suffix(".tiff.tmp")
            shutil.copyfile(resolved_source, temporary)
            copied_digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
            if copied_digest != source_digest:
                temporary.unlink(missing_ok=True)
                raise OSError("Color target source changed while it was being preserved")
            temporary.replace(destination)
            destination.chmod(0o444)
            preview_tmp = analysis_image.with_suffix(".png.tmp")
            shutil.copyfile(resolved_preview, preview_tmp)
            preview_tmp.replace(analysis_image)
            _atomic_write(
                analysis_json,
                json.dumps({
                    "schema": "cubos.color-target-reanalysis.v1",
                    "revision": 0,
                    "source_image_sha256": capture_sha256,
                    "analysis": initial_analysis,
                }, indent=2, sort_keys=True, default=str) + "\n",
            )
            record.metadata["color_target_source_artifact"] = COLOR_TARGET_SOURCE_ARTIFACT
            record.metadata["color_target_source_sha256"] = capture_sha256
            if COLOR_TARGET_SOURCE_ARTIFACT not in record.artifacts:
                record.artifacts.append(COLOR_TARGET_SOURCE_ARTIFACT)
            for name in ("color-target-analysis-0.json", "color-target-analysis-0.png"):
                if name not in record.artifacts:
                    record.artifacts.append(name)

    def append_color_target_analysis(
        self,
        record: RunRecord,
        *,
        artifact: dict[str, Any],
        annotated_preview: Path,
    ) -> tuple[RunRecord, dict[str, Any]]:
        """Persist one immutable same-frame color-analysis revision."""
        with self._artifact_lock:
            current = self.read(record.run_id) or record
            revisions = current.metadata.get("color_target_reanalyses", [])
            history = list(revisions) if isinstance(revisions, list) else []
            revision = max(
                (
                    int(item.get("revision", 0))
                    for item in history if isinstance(item, dict)
                ),
                default=0,
            ) + 1
            stem = f"color-target-analysis-{revision}"
            json_name = f"{stem}.json"
            image_name = f"{stem}.png"
            directory = self.run_dir(record.run_id)
            json_path = directory / json_name
            image_path = directory / image_name
            if json_path.exists() or image_path.exists():
                raise FileExistsError(
                    f"Color-target analysis revision {revision} already exists"
                )
            complete_artifact = {**artifact, "revision": revision}
            analysis = complete_artifact.get("analysis")
            if isinstance(analysis, dict):
                complete_artifact["analysis"] = {
                    **analysis,
                    "annotated_preview_path": str(image_path),
                }
            image_tmp = image_path.with_suffix(".png.tmp")
            shutil.copyfile(annotated_preview, image_tmp)
            image_tmp.replace(image_path)
            _atomic_write(
                json_path,
                json.dumps(
                    complete_artifact, indent=2, sort_keys=True, default=str,
                ) + "\n",
            )
            history.append({
                "revision": revision,
                "source_image_sha256": complete_artifact["source_image_sha256"],
                "json_artifact": json_name,
                "image_artifact": image_name,
                "analysis": complete_artifact["analysis"],
            })
            current.metadata["color_target_reanalyses"] = history
            for name in (json_name, image_name):
                if name not in current.artifacts:
                    current.artifacts.append(name)
            self.write(current)
            return current, complete_artifact

    @contextmanager
    def color_target_analysis_transaction(self):
        """Serialize same-frame analysis and immutable revision persistence."""
        with self._artifact_lock:
            yield

    def incomplete_records(self) -> Iterable[RunRecord]:
        for path in self.base_dir.glob("*/run.json"):
            try:
                record = RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if record.state in {"queued", "running", "cancel_requested"}:
                yield record
