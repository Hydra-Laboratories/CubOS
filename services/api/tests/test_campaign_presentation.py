from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cubos_api.models.presentation import DemoMarkerRequest
from cubos_api.services.campaign_presentation import CampaignPresentationService


class FakeCampaigns:
    def __init__(self, base: Path, record):
        self.base = base
        self.record = record

    def get(self, campaign_id):
        if campaign_id != self.record.campaign_id:
            raise KeyError(campaign_id)
        return self.record


class FakeStore:
    def __init__(self, base: Path):
        self.base_dir = base

    def run_dir(self, run_id):
        return self.base_dir / run_id

    def artifact_path(self, run_id, name):
        path = self.run_dir(run_id) / name
        return path if path.is_file() else None


class FakeRuns:
    def __init__(self, base: Path, records=None, events=None):
        self.store = FakeStore(base)
        self.records = records or {}
        self._events = events or {}

    def get(self, run_id):
        return self.records.get(run_id)

    def events(self, run_id):
        return self._events.get(run_id, [])


def campaign(tmp_path: Path, trials, *, state="completed", target_rgb=(120.0, 40.0, 160.0)):
    spec = SimpleNamespace(
        target_mode="rgb", target_rgb=target_rgb,
        objective=SimpleNamespace(direction="minimize"),
    )
    return SimpleNamespace(
        campaign_id="campaign-1", spec=spec, state=state,
        created_at=100.0, trials=trials,
    )


def trial(index, objective, status="accepted", measurement=None, run_id=None):
    return SimpleNamespace(
        index=index, parameters={"red_ul": 50.0 + index},
        run_id=run_id or f"run-{index}", state="succeeded",
        objective=objective, measurement=measurement,
        objective_status=status, error=None, sample_well=f"plate.A{index + 1}",
    )


def test_projection_uses_only_accepted_trials_for_best_and_preserves_quality(tmp_path):
    accepted_measurement = {
        "rgb": [20, 30, 40], "lab": [10, 2, -3],
        "quality": {"accepted": True}, "processing_profile_id": "profile-a",
    }
    rejected_measurement = {
        "rgb": [120, 130, 140], "lab": [55, 2, -1],
        "quality": {"accepted": False, "rejection_reasons": ["blur"]},
    }
    record = campaign(tmp_path, [
        trial(0, 4.0, measurement=accepted_measurement),
        trial(1, 0.1, status="rejected", measurement=rejected_measurement),
        trial(2, None, status="pending"),
    ])
    service = CampaignPresentationService(
        FakeCampaigns(tmp_path / "campaigns", record), FakeRuns(tmp_path / "runs"),
    )
    result = service.project("campaign-1")
    assert result.best == {"trial_id": "trial-1", "sequence": 1, "well": "plate.A1", "delta_e": 4.0}
    assert result.attempts[1]["accepted"] is False
    assert result.attempts[1]["measurement"]["quality"]["rejection_reasons"] == ["blur"]
    assert result.attempts[2]["measurement"] is None


def test_events_are_chronological_with_stable_replay_timecodes(tmp_path):
    record = campaign(tmp_path, [trial(0, 3.0), trial(1, 2.0)])
    event = lambda seq, stamp, message: SimpleNamespace(
        sequence=seq, timestamp=stamp, state="running", message=message,
        kind="lifecycle", data=None,
    )
    runs = FakeRuns(tmp_path / "runs", events={
        "run-0": [event(2, 104.0, "later"), event(1, 101.0, "first")],
        "run-1": [event(1, 102.0, "second")],
    })
    result = CampaignPresentationService(FakeCampaigns(tmp_path / "campaigns", record), runs).project("campaign-1")
    assert [item["label"] for item in result.events] == ["first", "second", "later"]
    assert [item["sequence"] for item in result.events] == [1, 2, 3]
    assert [item["elapsed_ms"] for item in result.events] == [1000, 2000, 4000]


def test_markers_are_validated_and_written_atomically(tmp_path):
    record = campaign(tmp_path, [])
    base = tmp_path / "campaigns"
    (base / record.campaign_id).mkdir(parents=True)
    service = CampaignPresentationService(FakeCampaigns(base, record), FakeRuns(tmp_path / "runs"))
    with pytest.raises(ValidationError):
        DemoMarkerRequest(label="   ")
    first = service.add_marker("campaign-1", DemoMarkerRequest(label="  camera  ready ", client_time=7.0))
    second = service.add_marker("campaign-1", DemoMarkerRequest(label="attempt one", footage_offset_ms=1200))
    assert first.label == "camera ready"
    assert second.sequence == 2
    saved = json.loads((base / "campaign-1" / "presentation-annotations.json").read_text())
    assert [item["sequence"] for item in saved["markers"]] == [1, 2]


def test_asset_catalog_rejects_outside_paths_and_symlink_escape(tmp_path, monkeypatch):
    import cubos_api.services.campaign_presentation as module

    image_root = tmp_path / "images"
    image_root.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"secret")
    link = image_root / "escape.png"
    link.symlink_to(outside)
    monkeypatch.setattr(module, "default_images_dir", lambda: image_root)
    record = campaign(tmp_path, [trial(0, 3.0, measurement={"image_path": str(link)})])
    service = CampaignPresentationService(FakeCampaigns(tmp_path / "campaigns", record), FakeRuns(tmp_path / "runs"))
    result = service.project("campaign-1")
    assert result.attempts[0]["assets"] == {}
    assert "trial-1.raw_image" in result.missing
    assert service.resolve_asset("campaign-1", "../secret") is None


def test_export_contains_projection_snapshots_assets_and_explicit_missing(tmp_path, monkeypatch):
    import cubos_api.services.campaign_presentation as module

    image_root = tmp_path / "images"
    image_root.mkdir()
    image = image_root / "well.png"
    image.write_bytes(b"png-data")
    monkeypatch.setattr(module, "default_images_dir", lambda: image_root)
    record = campaign(tmp_path, [trial(0, 1.5, measurement={"image_path": str(image)})], state="running")
    campaign_dir = tmp_path / "campaigns" / record.campaign_id
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "campaign.json").write_text("{}")
    run_dir = tmp_path / "runs" / "run-0"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("{}")
    (run_dir / "events.jsonl").write_text("")
    (run_dir / "protocol.yaml").write_text("protocol: []\n")
    service = CampaignPresentationService(FakeCampaigns(tmp_path / "campaigns", record), FakeRuns(tmp_path / "runs"))
    payload = service.export_zip("campaign-1")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())
        assert {"manifest.json", "presentation.json", "campaign/campaign.json"} <= names
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema_version"] == "cubos.campaign-presentation-export.v1"
        assert manifest["partial"] is True
        assert len(manifest["assets"]) == 1
        asset = manifest["assets"][0]
        assert archive.read(asset["file"]) == b"png-data"
        assert asset["sha256"]
        assert "campaign/gantry.yaml" in manifest["missing"]


def test_export_applies_size_limit_to_run_artifacts(tmp_path, monkeypatch):
    import cubos_api.services.campaign_presentation as module

    monkeypatch.setattr(module, "MAX_EXPORT_BYTES", 8)
    record = campaign(tmp_path, [trial(0, 1.5)])
    campaign_dir = tmp_path / "campaigns" / record.campaign_id
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "campaign.json").write_text("too-large")
    run_dir = tmp_path / "runs" / "run-0"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("also-too-large")
    service = CampaignPresentationService(
        FakeCampaigns(tmp_path / "campaigns", record), FakeRuns(tmp_path / "runs")
    )
    with zipfile.ZipFile(io.BytesIO(service.export_zip("campaign-1"))) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["partial"] is True
        assert "campaign/campaign.json:size_limit" in manifest["missing"]
        assert "runs/run-0/run.json:size_limit" in manifest["missing"]
