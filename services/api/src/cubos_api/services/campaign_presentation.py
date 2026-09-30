"""Derive demo-ready campaign evidence from native campaign and run records."""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from cubos.protocol_engine.commands.camera import default_images_dir
from cubos.optimization import rgb_to_lab
from cubos_api.models.presentation import DemoMarker, DemoMarkerRequest, PresentationResponse
from cubos_api.services.campaign_manager import CampaignManager
from cubos_api.services.run_manager import RunManager

SCHEMA_VERSION = "1"
EXPORT_SCHEMA_VERSION = "cubos.campaign-presentation-export.v1"
MAX_EXPORT_BYTES = 128 * 1024 * 1024
MAX_ASSETS = 256
_annotation_locks: dict[str, threading.Lock] = {}
_annotation_locks_guard = threading.Lock()


def _lock_for(campaign_id: str) -> threading.Lock:
    with _annotation_locks_guard:
        return _annotation_locks.setdefault(campaign_id, threading.Lock())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _measurement_quality(measurement: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(measurement, dict):
        return None
    quality = measurement.get("quality")
    if isinstance(quality, dict):
        return quality
    return {
        key: measurement[key]
        for key in ("accepted", "rejection_reasons", "valid_pixel_count", "clipped_fraction")
        if key in measurement
    } or None


def _profile(measurement: dict[str, Any] | None) -> dict[str, Any] | str | None:
    if not isinstance(measurement, dict):
        return None
    profile = measurement.get("processing_profile")
    if profile is None:
        profile = measurement.get("processing_profile_id")
    return profile


def _image_paths(measurement: dict[str, Any] | None) -> list[tuple[str, Path]]:
    if not isinstance(measurement, dict):
        return []
    result = []
    for role, key in (("raw", "image_path"), ("annotated", "annotated_preview_path")):
        value = measurement.get(key)
        if isinstance(value, str) and value:
            result.append((role, Path(value)))
    return result


class CampaignPresentationService:
    def __init__(self, campaigns: CampaignManager, runs: RunManager):
        self.campaigns = campaigns
        self.runs = runs

    def _campaign_dir(self, campaign_id: str) -> Path:
        return self.campaigns.base / campaign_id

    def _annotation_path(self, campaign_id: str) -> Path:
        return self._campaign_dir(campaign_id) / "presentation-annotations.json"

    def _read_markers(self, campaign_id: str) -> list[DemoMarker]:
        path = self._annotation_path(campaign_id)
        if not path.is_file():
            return []
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("schema_version") != SCHEMA_VERSION:
                return []
            return [DemoMarker.model_validate(item) for item in document.get("markers", [])]
        except (OSError, ValueError, TypeError):
            return []

    def add_marker(self, campaign_id: str, request: DemoMarkerRequest) -> DemoMarker:
        self.campaigns.get(campaign_id)
        with _lock_for(campaign_id):
            markers = self._read_markers(campaign_id)
            marker = DemoMarker(
                id=uuid.uuid4().hex,
                sequence=len(markers) + 1,
                server_time=time.time(),
                **request.model_dump(),
            )
            markers.append(marker)
            path = self._annotation_path(campaign_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps({
                "schema_version": SCHEMA_VERSION,
                "campaign_id": campaign_id,
                "markers": [item.model_dump() for item in markers],
            }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temporary.replace(path)
            return marker

    @staticmethod
    def _asset_id(campaign_id: str, trial_id: str, role: str, path: Path) -> str:
        material = f"{campaign_id}\0{trial_id}\0{role}\0{path}".encode()
        return hashlib.sha256(material).hexdigest()[:32]

    def _allowed_path(self, path: Path) -> Path | None:
        try:
            resolved = path.expanduser().resolve(strict=True)
        except OSError:
            return None
        roots = (self.runs.store.base_dir.resolve(), default_images_dir().expanduser().resolve())
        if not resolved.is_file():
            return None
        for root in roots:
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue
        return None

    def _asset_catalog(self, campaign_id: str) -> dict[str, dict[str, Any]]:
        record = self.campaigns.get(campaign_id)
        catalog: dict[str, dict[str, Any]] = {}
        target_run = getattr(record.spec, "target_run_id", None)
        if target_run:
            run = self.runs.get(target_run)
            if run:
                artifact = run.metadata.get("color_target_source_artifact")
                if isinstance(artifact, str):
                    path = self.runs.store.artifact_path(target_run, artifact)
                    if path:
                        asset_id = self._asset_id(campaign_id, "target", "raw", path)
                        catalog[asset_id] = {"path": path, "role": "target_raw", "trial_id": "target"}
                revision = getattr(record.spec, "target_analysis_revision", None)
                if revision is not None:
                    name = f"color-target-analysis-{revision}.png"
                    path = self.runs.store.artifact_path(target_run, name)
                    if path:
                        asset_id = self._asset_id(campaign_id, "target", "annotated", path)
                        catalog[asset_id] = {"path": path, "role": "target_annotated", "trial_id": "target"}
        for trial in record.trials:
            for role, raw_path in _image_paths(trial.measurement):
                path = self._allowed_path(raw_path)
                if path is None:
                    continue
                trial_id = f"trial-{trial.index + 1}"
                asset_id = self._asset_id(campaign_id, trial_id, role, path)
                catalog[asset_id] = {"path": path, "role": role, "trial_id": trial_id}
        return catalog

    def resolve_asset(self, campaign_id: str, asset_id: str) -> Path | None:
        if len(asset_id) != 32 or any(ch not in "0123456789abcdef" for ch in asset_id):
            return None
        entry = self._asset_catalog(campaign_id).get(asset_id)
        return self._allowed_path(entry["path"]) if entry else None

    def project(self, campaign_id: str) -> PresentationResponse:
        record = self.campaigns.get(campaign_id)
        catalog = self._asset_catalog(campaign_id)
        missing: list[str] = []
        target_assets = {
            entry["role"]: asset_id
            for asset_id, entry in catalog.items() if entry["trial_id"] == "target"
        }
        first_measurement = next(
            (
                trial.measurement for trial in sorted(record.trials, key=lambda item: item.index)
                if isinstance(trial.measurement, dict)
            ),
            None,
        )
        target_rgb = list(record.spec.target_rgb) if record.spec.target_rgb else None
        target_lab = (
            list(rgb_to_lab(record.spec.target_rgb))
            if record.spec.target_rgb else
            (first_measurement.get("reference_lab") if first_measurement else None)
        )
        profile_id = (
            getattr(record.spec, "reference_processing_profile_id", None)
            or (first_measurement.get("reference_processing_profile_id") if first_measurement else None)
        )
        target = {
            "source": (
                "selected_srgb" if record.spec.target_mode == "rgb"
                else "accepted_camera_measurement"
            ),
            "mode": record.spec.target_mode,
            "well": getattr(record.spec, "target_well", None),
            "run_id": getattr(record.spec, "target_run_id", None),
            "analysis_revision": getattr(record.spec, "target_analysis_revision", None),
            "measurement": {
                "rgb": target_rgb,
                "lab": target_lab,
                "delta_e": 0.0,
                "quality": None,
                "profile": (
                    {"id": profile_id} if isinstance(profile_id, str) else profile_id
                ),
            },
            "processing_profile_id": profile_id,
            "accepted": bool(target_lab),
            "image_asset_id": (
                target_assets.get("target_annotated")
                or target_assets.get("target_raw")
            ),
            "assets": target_assets,
        }
        if record.spec.target_mode == "camera" and "target_raw" not in target_assets:
            missing.append("target.raw_image")
        attempts: list[dict[str, Any]] = []
        for trial in sorted(record.trials, key=lambda item: item.index):
            trial_id = f"trial-{trial.index + 1}"
            measurement = trial.measurement if isinstance(trial.measurement, dict) else None
            assets = {
                entry["role"]: asset_id
                for asset_id, entry in catalog.items() if entry["trial_id"] == trial_id
            }
            if measurement and "raw" not in assets:
                missing.append(f"{trial_id}.raw_image")
            run = self.runs.get(trial.run_id)
            attempts.append({
                "sequence": trial.index + 1,
                "trial_id": trial_id,
                "run_id": trial.run_id,
                "well": trial.sample_well,
                "recipe_ul": dict(trial.parameters),
                "status": trial.state,
                "accepted": trial.objective_status == "accepted",
                "objective_status": trial.objective_status,
                "delta_e": trial.objective,
                "measurement": ({
                    "rgb": measurement.get("rgb"),
                    "lab": measurement.get("lab"),
                    "delta_e": trial.objective,
                    "reference_rgb": measurement.get("reference_rgb"),
                    "reference_lab": measurement.get("reference_lab"),
                    "quality": _measurement_quality(measurement),
                    "profile": _profile(measurement),
                } if measurement else None),
                "image_asset_id": assets.get("annotated") or assets.get("raw"),
                "assets": assets,
                "started_at": run.started_at if run else None,
                "completed_at": run.finished_at if run else None,
                "error": trial.error,
            })
        accepted = [item for item in attempts if item["accepted"] and item["delta_e"] is not None]
        best = None
        if accepted:
            chosen = (min if record.spec.objective.direction == "minimize" else max)(
                accepted, key=lambda item: item["delta_e"]
            )
            best = {key: chosen[key] for key in ("trial_id", "sequence", "well", "delta_e")}
        events: list[dict[str, Any]] = []
        for trial in record.trials:
            for event in self.runs.events(trial.run_id):
                events.append({
                    "kind": event.kind,
                    "server_time": event.timestamp,
                    "trial_id": f"trial-{trial.index + 1}",
                    "label": event.message,
                    "data": event.data,
                    "source_sequence": event.sequence,
                })
        events.sort(key=lambda item: (item["server_time"], item["trial_id"], item["source_sequence"]))
        for sequence, event in enumerate(events, 1):
            event["sequence"] = sequence
            event["elapsed_ms"] = max(0, round((event["server_time"] - record.created_at) * 1000))
        partial = record.state not in {"completed", "stopped", "failed", "interrupted"} or bool(missing)
        return PresentationResponse(
            campaign_id=campaign_id, status=record.state, target=target,
            attempts=attempts, best=best, events=events,
            markers=self._read_markers(campaign_id), partial=partial,
            missing=sorted(set(missing)),
        )

    def export_zip(self, campaign_id: str) -> bytes:
        projection = self.project(campaign_id)
        record = self.campaigns.get(campaign_id)
        catalog = self._asset_catalog(campaign_id)
        manifest: dict[str, Any] = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "campaign_id": campaign_id,
            "created_at": time.time(),
            "partial": projection.partial,
            "missing": list(projection.missing),
            "assets": [],
        }
        output = io.BytesIO()
        total = 0
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("presentation.json", projection.model_dump_json(indent=2))
            campaign_dir = self._campaign_dir(campaign_id)
            for name in ("campaign.json", "gantry.yaml", "deck.yaml", "protocol.yaml", "presentation-annotations.json"):
                path = campaign_dir / name
                if path.is_file():
                    size = path.stat().st_size
                    if total + size <= MAX_EXPORT_BYTES:
                        archive.write(path, f"campaign/{name}")
                        total += size
                    else:
                        manifest["missing"].append(f"campaign/{name}:size_limit")
                else:
                    manifest["missing"].append(f"campaign/{name}")
            run_ids = sorted({trial.run_id for trial in record.trials})
            for run_id in run_ids:
                run_dir = self.runs.store.run_dir(run_id)
                for name in ("run.json", "events.jsonl", "gantry.yaml", "deck.yaml", "protocol.yaml", "result.json", "error.txt"):
                    path = run_dir / name
                    if path.is_file():
                        size = path.stat().st_size
                        if total + size <= MAX_EXPORT_BYTES:
                            archive.write(path, f"runs/{run_id}/{name}")
                            total += size
                        else:
                            manifest["missing"].append(
                                f"runs/{run_id}/{name}:size_limit"
                            )
                    elif name in {"run.json", "events.jsonl", "protocol.yaml"}:
                        manifest["missing"].append(f"runs/{run_id}/{name}")
            for index, (asset_id, entry) in enumerate(catalog.items()):
                if index >= MAX_ASSETS:
                    manifest["missing"].append("assets:limit_exceeded")
                    break
                path = self._allowed_path(entry["path"])
                if path is None:
                    manifest["missing"].append(f"asset:{asset_id}")
                    continue
                size = path.stat().st_size
                if total + size > MAX_EXPORT_BYTES:
                    manifest["missing"].append("assets:size_limit_exceeded")
                    break
                total += size
                suffix = path.suffix.lower() or ".bin"
                member = f"assets/{asset_id}{suffix}"
                archive.write(path, member)
                manifest["assets"].append({
                    "id": asset_id, "file": member, "role": entry["role"],
                    "trial_id": entry["trial_id"], "sha256": _sha256(path), "bytes": size,
                })
            manifest["missing"] = sorted(set(manifest["missing"]))
            manifest["partial"] = bool(manifest["partial"] or manifest["missing"])
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return output.getvalue()
