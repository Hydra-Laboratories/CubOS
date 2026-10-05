from __future__ import annotations

import io
import hashlib
import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cubos_api.models.presentation import DemoMarkerRequest
from cubos_api.models.campaigns import CampaignRecord, CampaignSpec, CampaignTrial
from cubos_api.models.runs import RunRecord
from cubos_api.config import get_settings
from cubos_api.services.campaign_manager import CampaignManager
from cubos_api.services.campaign_presentation import CampaignPresentationService
from cubos_api.services.run_manager import RunManager


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
        name="test campaign",
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
        "measurement_status": "accepted", "comparison_status": "accepted",
        "frame_metadata": {"width": 800, "height": 600},
        "roi": {"center_x_px": 400, "center_y_px": 300, "radius_px": 25},
    }
    rejected_measurement = {
        "rgb": [120, 130, 140], "lab": [55, 2, -1],
        "quality": {"accepted": False, "rejection_reasons": ["blur"]},
        "measurement_status": "rejected", "comparison_status": "rejected",
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
    assert result.attempts[0]["measurement"]["frame"] == {
        "width_px": 800, "height_px": 600,
    }
    assert result.attempts[0]["measurement"]["roi"]["radius_px"] == 25


def test_legacy_camera_target_is_explicitly_unverified_and_not_inferred(tmp_path):
    measurement = {
        "reference_lab": [99, 88, 77],
        "reference_processing_profile_id": "future-payload",
        "measurement_status": "accepted", "comparison_status": "accepted",
        "quality": {"accepted": True}, "rgb": [1, 2, 3], "lab": [4, 5, 6],
    }
    record = campaign(tmp_path, [trial(0, 1.0, measurement=measurement)])
    record.spec.target_mode = "camera"
    record.spec.target_rgb = None
    record.spec.target_lab = None
    record.spec.reference_processing_profile_id = None
    record.spec.target_run_id = None
    record.spec.target_analysis_revision = None
    record.spec.target_well = None
    result = CampaignPresentationService(
        FakeCampaigns(tmp_path / "campaigns", record), FakeRuns(tmp_path / "runs")
    ).project("campaign-1")
    assert result.target["accepted"] is False
    assert result.target["measurement"]["lab"] is None
    assert "target.frozen_provenance" in result.missing
    assert result.best is None


def test_camera_score_requires_matching_profile_and_accepted_quality(tmp_path):
    base = {
        "measurement_status": "accepted", "comparison_status": "accepted",
        "quality": {"accepted": True}, "rgb": [1, 2, 3], "lab": [4, 5, 6],
        "reference_processing_profile_id": "wrong-profile",
    }
    record = campaign(tmp_path, [trial(0, 1.0, measurement=base)])
    record.spec.target_mode = "camera"
    record.spec.target_rgb = None
    record.spec.target_lab = (42, 12, 18)
    record.spec.reference_processing_profile_id = "profile-v1"
    record.spec.target_run_id = "target-run"
    record.spec.target_analysis_revision = 0
    record.spec.target_well = "plate.C7"
    result = CampaignPresentationService(
        FakeCampaigns(tmp_path / "campaigns", record), FakeRuns(tmp_path / "runs")
    ).project("campaign-1")
    assert result.attempts[0]["score_eligible"] is False
    assert result.best is None


def test_events_are_chronological_with_stable_replay_timecodes(tmp_path):
    record = campaign(tmp_path, [trial(0, 3.0), trial(1, 2.0)])
    event = lambda seq, stamp, message, state="running": SimpleNamespace(
        sequence=seq, timestamp=stamp, state=state, message=message,
        kind="lifecycle", data=None,
    )
    runs = FakeRuns(tmp_path / "runs", events={
        "run-0": [event(2, 104.0, "later", "succeeded"), event(1, 101.0, "first")],
        "run-1": [event(1, 102.0, "second", "succeeded")],
    })
    result = CampaignPresentationService(FakeCampaigns(tmp_path / "campaigns", record), runs).project("campaign-1")
    assert [item["label"] for item in result.events] == ["first", "second", "later"]
    assert [item["sequence"] for item in result.events] == [1, 2, 3]
    assert [item["elapsed_ms"] for item in result.events] == [1000, 2000, 4000]
    assert result.attempts[0]["reveal_event_sequence"] == 3
    assert result.attempts[1]["reveal_event_sequence"] == 2


def test_markers_are_validated_and_written_atomically(tmp_path):
    record = campaign(tmp_path, [])
    base = tmp_path / "campaigns"
    (base / record.campaign_id).mkdir(parents=True)
    service = CampaignPresentationService(FakeCampaigns(base, record), FakeRuns(tmp_path / "runs"))
    with pytest.raises(ValidationError):
        DemoMarkerRequest(label="   ")
    first = service.add_marker("campaign-1", DemoMarkerRequest(label="  camera  ready ", client_time=7.0))
    second = service.add_marker(
        "campaign-1",
        DemoMarkerRequest(
            label="attempt one", timeline_elapsed_ms=800,
            video_time_ms=2000, footage_offset_ms=1200,
        ),
    )
    assert first.label == "camera ready"
    assert second.sequence == 2
    saved = json.loads((base / "campaign-1" / "presentation-annotations.json").read_text())
    assert [item["sequence"] for item in saved["markers"]] == [1, 2]

    identified = DemoMarkerRequest(
        label="recording sync", client_id="sync-1", client_time=10.5,
        timeline_elapsed_ms=500, video_time_ms=3000, footage_offset_ms=2500,
        recording_name="demo.mp4",
        recording_size=1234, recording_last_modified=10.0,
        recording_sha256="a" * 64,
    )
    created = service.add_marker("campaign-1", identified)
    replayed = service.add_marker("campaign-1", identified)
    assert replayed.id == created.id
    assert replayed.recording_sha256 == "a" * 64

    with pytest.raises(ValidationError, match="recording identity"):
        DemoMarkerRequest(label="incomplete", recording_name="demo.mp4")
    with pytest.raises(ValidationError, match="must equal"):
        DemoMarkerRequest(
            label="bad sync", timeline_elapsed_ms=500,
            video_time_ms=1000, footage_offset_ms=700,
        )


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


def test_tiff_asset_has_lossless_browser_png_without_changing_export_source(tmp_path, monkeypatch):
    import cv2
    import numpy as np
    import cubos_api.services.campaign_presentation as module

    image_root = tmp_path / "images"
    image_root.mkdir()
    source = image_root / "well.tiff"
    pixels = np.array([
        [[0, 10, 255], [30, 20, 10]],
        [[255, 0, 40], [80, 90, 100]],
    ], dtype=np.uint8)
    assert cv2.imwrite(str(source), pixels)
    monkeypatch.setattr(module, "default_images_dir", lambda: image_root)
    record = campaign(tmp_path, [trial(0, 2.0, measurement={"image_path": str(source)})])
    service = CampaignPresentationService(
        FakeCampaigns(tmp_path / "campaigns", record), FakeRuns(tmp_path / "runs")
    )
    projection = service.project("campaign-1")
    asset_id = projection.attempts[0]["raw_image_asset_id"]
    rendered, media_type = service.browser_asset("campaign-1", asset_id)
    assert isinstance(rendered, bytes)
    assert media_type == "image/png"
    decoded = cv2.imdecode(np.frombuffer(rendered, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert np.array_equal(decoded, pixels)
    with zipfile.ZipFile(io.BytesIO(service.export_zip("campaign-1"))) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        raw = next(item for item in manifest["assets"] if item["role"] == "raw")
        assert archive.read(raw["file"]) == source.read_bytes()


def test_browser_asset_rejects_unsupported_format(tmp_path, monkeypatch):
    import cubos_api.services.campaign_presentation as module

    image_root = tmp_path / "images"
    image_root.mkdir()
    source = image_root / "well.xyz"
    source.write_bytes(b"unknown")
    monkeypatch.setattr(module, "default_images_dir", lambda: image_root)
    record = campaign(tmp_path, [trial(0, 2.0, measurement={"image_path": str(source)})])
    service = CampaignPresentationService(
        FakeCampaigns(tmp_path / "campaigns", record), FakeRuns(tmp_path / "runs")
    )
    asset_id = service.project("campaign-1").attempts[0]["raw_image_asset_id"]
    with pytest.raises(module.AssetPreviewError, match="Unsupported"):
        service.browser_asset("campaign-1", asset_id)


def test_browser_asset_rejects_float_tiff_instead_of_coercing_pixels(tmp_path, monkeypatch):
    import cv2
    import numpy as np
    import cubos_api.services.campaign_presentation as module

    image_root = tmp_path / "images"
    image_root.mkdir()
    source = image_root / "float-well.tiff"
    pixels = np.array([[0.25, 0.75], [1.5, 2.0]], dtype=np.float32)
    assert cv2.imwrite(str(source), pixels)
    monkeypatch.setattr(module, "default_images_dir", lambda: image_root)
    record = campaign(tmp_path, [trial(0, 2.0, measurement={"image_path": str(source)})])
    service = CampaignPresentationService(
        FakeCampaigns(tmp_path / "campaigns", record), FakeRuns(tmp_path / "runs")
    )
    asset_id = service.project("campaign-1").attempts[0]["raw_image_asset_id"]
    with pytest.raises(module.AssetPreviewError, match="losslessly"):
        service.browser_asset("campaign-1", asset_id)


def test_asset_route_returns_native_browser_image_file(tmp_path, monkeypatch):
    from fastapi.responses import FileResponse
    from cubos_api.routers import presentation as routes

    image = tmp_path / "well.png"
    image.write_bytes(b"png")
    service = SimpleNamespace(browser_asset=lambda campaign_id, asset_id: (image, "image/png"))
    monkeypatch.setattr(routes, "_service", lambda: service)
    response = routes.get_asset("campaign-1", "a" * 32)
    assert isinstance(response, FileResponse)
    assert Path(response.path) == image
    assert response.media_type == "image/png"


def test_presentation_route_adds_late_server_clock_sample(tmp_path, monkeypatch):
    from cubos_api.routers import presentation as routes

    record = campaign(tmp_path, [])
    projected = CampaignPresentationService(
        FakeCampaigns(tmp_path / "campaigns", record), FakeRuns(tmp_path / "runs")
    ).project("campaign-1")
    service = SimpleNamespace(project=lambda campaign_id: projected)
    monkeypatch.setattr(routes, "_service", lambda: service)
    monkeypatch.setattr(routes.time, "time", lambda: 1_760_000_000.125)

    response = routes.get_presentation("campaign-1")

    assert response.server_now_epoch_ms == 1_760_000_000_125.0
    assert projected.server_now_epoch_ms is None


def test_target_assets_are_only_from_the_linked_frozen_target_run(tmp_path):
    record = campaign(tmp_path, [])
    record.spec.target_mode = "camera"
    record.spec.target_rgb = None
    record.spec.target_run_id = "selected-target"
    record.spec.target_analysis_revision = 2
    record.spec.target_well = "plate.C7"
    record.spec.target_lab = (42.0, 12.0, 18.0)
    record.spec.reference_processing_profile_id = "profile-v1"
    records = {}
    for run_id in ("selected-target", "other-target"):
        run_dir = tmp_path / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "color-target-source.tiff").write_bytes(run_id.encode())
        (run_dir / "color-target-analysis-2.png").write_bytes(b"annotated")
        (run_dir / "color-target-analysis-2.json").write_text(json.dumps({
            "schema": "cubos.color-target-reanalysis.v1", "revision": 2,
            "analysis": {
                "quality": {"accepted": True},
                "frame_metadata": {"width": 800, "height": 600},
                "roi": {"center_x_px": 401, "center_y_px": 302, "radius_px": 24},
            },
        }))
        records[run_id] = SimpleNamespace(
            metadata={"color_target_source_artifact": "color-target-source.tiff"},
            started_at=None, finished_at=None,
        )
    service = CampaignPresentationService(
        FakeCampaigns(tmp_path / "campaigns", record),
        FakeRuns(tmp_path / "runs", records=records),
    )
    projection = service.project("campaign-1")
    assert set(projection.target["assets"]) == {"target_raw", "target_annotated"}
    assert projection.target["raw_image_asset_id"]
    assert projection.target["annotated_image_asset_id"]
    assert projection.target["measurement"]["frame"]["width_px"] == 800
    assert projection.target["measurement"]["roi"]["radius_px"] == 24
    selected_ids = set(projection.target["assets"].values())
    other_path = tmp_path / "runs" / "other-target" / "color-target-source.tiff"
    unlinked_id = service._asset_id("campaign-1", "target", "raw", other_path)
    assert unlinked_id not in selected_ids
    assert service.resolve_asset("campaign-1", unlinked_id) is None

    with zipfile.ZipFile(io.BytesIO(service.export_zip("campaign-1"))) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        roles = {item["role"] for item in manifest["assets"]}
        assert roles == {"target_raw", "target_annotated"}
        archived = {
            item["role"]: archive.read(item["file"])
            for item in manifest["assets"]
        }
        assert archived["target_raw"] == b"selected-target"
        assert archived["target_annotated"] == b"annotated"
        snapshot_files = {item["file"] for item in manifest["snapshots"]}
        assert "runs/selected-target/color-target-analysis-2.json" in snapshot_files
        assert all(item["sha256"] for item in manifest["snapshots"])


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


def test_export_rejects_payload_over_final_size_limit(tmp_path, monkeypatch):
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
    with pytest.raises(OverflowError, match="ZIP exceeds"):
        service.export_zip("campaign-1")


def test_export_hashes_exact_snapshot_bytes_when_live_file_changes(tmp_path, monkeypatch):
    import cubos_api.services.campaign_presentation as module

    record = campaign(tmp_path, [])
    campaign_dir = tmp_path / "campaigns" / record.campaign_id
    campaign_dir.mkdir(parents=True)
    source = campaign_dir / "campaign.json"
    source.write_bytes(b"before-snapshot")
    service = CampaignPresentationService(
        FakeCampaigns(tmp_path / "campaigns", record), FakeRuns(tmp_path / "runs")
    )
    original_reader = module._read_snapshot

    def mutate_after_read(path, limit):
        payload = original_reader(path, limit)
        if path == source:
            source.write_bytes(b"after-snapshot")
        return payload

    monkeypatch.setattr(module, "_read_snapshot", mutate_after_read)
    with zipfile.ZipFile(io.BytesIO(service.export_zip("campaign-1"))) as archive:
        archived = archive.read("campaign/campaign.json")
        manifest = json.loads(archive.read("manifest.json"))
    snapshot = next(
        item for item in manifest["snapshots"]
        if item["file"] == "campaign/campaign.json"
    )
    assert archived == b"before-snapshot"
    assert snapshot["bytes"] == len(archived)
    assert snapshot["sha256"] == hashlib.sha256(archived).hexdigest()
    assert source.read_bytes() == b"after-snapshot"


def test_native_campaign_and_run_store_roundtrip_exports_frozen_evidence(tmp_path):
    settings = get_settings()
    runs = RunManager(settings)
    campaigns = CampaignManager(settings, runs)
    target = RunRecord(
        run_id="target-native", state="succeeded", created_at=90.0,
        started_at=91.0, finished_at=92.0,
        metadata={"active_learning_target": "plate.C7"},
    )
    runs.store.create(
        target, gantry_yaml="gantry: target\n", deck_yaml="deck: target\n",
        protocol_yaml="protocol: []\n",
    )
    target_dir = runs.store.run_dir(target.run_id)
    (target_dir / "color-target-source.tiff").write_bytes(b"frozen-pixels")
    (target_dir / "color-target-analysis-0.png").write_bytes(b"annotated-pixels")
    (target_dir / "color-target-analysis-0.json").write_text(json.dumps({
        "schema": "cubos.color-target-reanalysis.v1", "revision": 0,
        "source_image_sha256": "0" * 64,
        "analysis": {
            "quality": {"accepted": True},
            "frame_metadata": {"width": 800, "height": 600},
            "roi": {"center_x_px": 400, "center_y_px": 300, "radius_px": 25},
        },
    }))
    target.metadata.update({
        "color_target_source_artifact": "color-target-source.tiff",
        "color_target_source_sha256": "0" * 64,
    })
    target.artifacts.extend([
        "color-target-source.tiff", "color-target-analysis-0.png",
        "color-target-analysis-0.json",
    ])
    runs.store.write(target)

    measurement = {
        "image_path": str(settings.run_dir / "trial-native" / "candidate.png"),
        "annotated_preview_path": str(
            settings.run_dir / "trial-native" / "candidate.analysis.png"
        ),
        "rgb": [30.0, 20.0, 40.0], "lab": [9.0, 8.0, -4.0],
        "delta_e_00": 2.0, "measurement_status": "accepted",
        "comparison_status": "accepted", "quality": {"accepted": True},
        "reference_processing_profile_id": "profile-v1",
        "frame_metadata": {"width": 800, "height": 600},
        "roi": {"center_x_px": 399, "center_y_px": 301, "radius_px": 26},
    }
    child = RunRecord(
        run_id="trial-native", state="succeeded", created_at=100.0,
        started_at=101.0, finished_at=104.0,
    )
    runs.store.create(
        child, gantry_yaml="gantry: trial\n", deck_yaml="deck: trial\n",
        protocol_yaml="protocol: []\n",
    )
    child_dir = runs.store.run_dir(child.run_id)
    (child_dir / "candidate.png").write_bytes(b"candidate-pixels")
    (child_dir / "candidate.analysis.png").write_bytes(b"candidate-annotation")
    child.result = {"results": [measurement]}
    runs.store.write_result(child, child.result)
    runs.store.append_event(
        child.run_id, state="running", message="measurement started",
    )
    runs.store.append_event(
        child.run_id, state="succeeded", message="execution completed",
    )
    runs.store.write(child)

    spec = CampaignSpec(
        name="native evidence", gantry_file="g.yaml", deck_file="d.yaml",
        protocol_file="p.yaml",
        parameters=[{
            "name": "red_ul", "minimum": 0.0, "maximum": 300.0,
            "step": 5.0, "bindings": [{"step_index": 0, "argument": "volume_ul"}],
        }],
        objective={"mode": "result", "path": "0.delta_e_00", "direction": "minimize"},
        optimizer={"initial_trials": 1}, stop={"max_trials": 1},
        mock_mode=True, target_mode="camera", target_run_id=target.run_id,
        target_analysis_revision=0, target_well="plate.C7",
        target_lab=(42.0, 12.0, 18.0),
        reference_processing_profile_id="profile-v1",
    )
    record = CampaignRecord(
        campaign_id="native-campaign", spec=spec, state="completed",
        created_at=100.0, updated_at=105.0,
        trials=[CampaignTrial(
            index=0, parameters={"red_ul": 100.0}, run_id=child.run_id,
            state="succeeded", objective=2.0, measurement=measurement,
            objective_status="accepted", sample_well="plate.A3",
        )],
        best_objective=2.0,
    )
    campaigns._records[record.campaign_id] = record
    campaigns._save(record)

    service = CampaignPresentationService(campaigns, runs)
    projection = service.project(record.campaign_id)
    assert projection.target["accepted"] is True
    assert projection.campaign_name == "native evidence"
    assert projection.target["measurement"]["roi"]["radius_px"] == 25
    assert projection.attempts[0]["score_eligible"] is True
    assert projection.attempts[0]["raw_image_asset_id"]
    assert projection.attempts[0]["measurement"]["roi"]["radius_px"] == 26
    assert projection.attempts[0]["reveal_event_sequence"] is not None
    with zipfile.ZipFile(io.BytesIO(service.export_zip(record.campaign_id))) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert {item["role"] for item in manifest["assets"]} == {
            "target_raw", "target_annotated", "raw", "annotated",
        }
        assert "runs/target-native/color-target-analysis-0.json" in {
            item["file"] for item in manifest["snapshots"]
        }
