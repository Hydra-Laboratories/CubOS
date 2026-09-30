import pytest
import hashlib
from pathlib import Path
import time
from types import SimpleNamespace
from tests.api_client import api_request
from cubos_api.app import create_app
from cubos_api.models.campaigns import Observation
from cubos_api.models.runs import RunSubmission
from cubos_api.models.runs import RunRecord
from cubos_api.models.campaigns import ColorTargetRequest
from cubos_api.services.run_store import RunStore
from cubos_api.services.run_manager import get_run_manager, RunConflictError


def test_station_reservation_blocks_external_submissions_and_setup_changes():
    manager = get_run_manager()
    manager.reserve_campaign('owner')
    try:
        with pytest.raises(RunConflictError, match='reserved'):
            manager.submit(RunSubmission(gantry_config='g',deck_config='d',protocol_yaml='p',mock_mode=True))
        app=create_app()
        assert api_request(app,'PUT','/api/v1/deck/new.yaml',json={'labware':{}}).status_code==409
        assert api_request(app,'POST','/api/v1/gantry/jog',json={'x':1}).status_code==409
        assert api_request(app,'POST','/api/v1/runs',json={}).status_code==409
        assert api_request(app,'GET','/api/v1/health').status_code==200
        assert api_request(app,'GET','/api/v1/campaigns').status_code==200
    finally:
        manager.release_campaign('owner')
    assert manager.campaign_owner is None


def test_inventory_edit_guard_rejects_campaign_owner():
    manager = get_run_manager()
    manager.reserve_campaign("owner")
    try:
        with pytest.raises(RunConflictError, match="active run or campaign"):
            with manager.inventory_edit():
                pass
    finally:
        manager.release_campaign("owner")


@pytest.mark.parametrize('value',[True, '1', float('nan'), float('inf')])
def test_manual_observation_rejects_invalid_types(value):
    with pytest.raises(ValueError): Observation(value=value)


def test_campaign_routes_report_missing_records():
    app=create_app()
    assert api_request(app,'GET','/api/v1/campaigns/not-found').status_code==404
    assert api_request(app,'POST','/api/v1/campaigns/not-found/stop',json={}).status_code==404
    assert api_request(app,'POST','/api/v1/campaigns/not-found/observation',json={'value':1}).status_code==404


def test_color_target_snapshot_preserves_selected_gantry_envelopes_and_filenames(
    monkeypatch, tmp_path: Path,
):
    import yaml
    from cubos_api.routers import campaigns as campaign_routes

    configs = tmp_path / "configs"
    (configs / "gantry").mkdir(parents=True)
    (configs / "deck").mkdir()
    selected_gantry = {
        "serial_port": "/dev/example",
        "gantry_type": "cub",
        "cnc": {
            "factory_z_travel_mm": 56.0,
            "safe_z": 66.5,
            "default_feed_rate_mm_min": 2000.0,
        },
        "working_volume": {
            "x_min": 0.0, "x_max": 258.205,
            "y_min": 0.0, "y_max": 144.645,
            "z_min": 10.5, "z_max": 66.5,
        },
        "origin_policy": "deck_origin",
        "instruments": {
            "pipette": {
                "type": "pipette", "vendor": "sartorius",
                "offset_x": 0.0, "offset_y": 0.0, "depth": -70.0,
                "motion_envelope": {
                    "box": {
                        "offset": {"x": -2.0, "y": -2.0, "z": 0.0},
                        "size": {"x": 4.0, "y": 4.0, "z": 30.0},
                    },
                    "attached_tip_radius_mm": 4.0,
                },
            },
            "camera": {
                "type": "camera", "vendor": "opencv",
                "offset_x": 0.0, "offset_y": -46.5, "depth": -114.964,
                "motion_envelope": {
                    "box": {
                        "offset": {"x": -8.0, "y": -8.0, "z": 0.0},
                        "size": {"x": 16.0, "y": 16.0, "z": 20.0},
                    },
                },
            },
        },
    }
    gantry_path = configs / "gantry" / "picus1000_routing_review.yaml"
    gantry_path.write_text(yaml.safe_dump(selected_gantry, sort_keys=False))
    deck_path = configs / "deck" / "color_matching_routing_review.yaml"
    deck_path.write_text("labware: {}\n")
    store = RunStore(tmp_path / "runs")

    class CapturingManager:
        def submit(self, submission):
            record = RunRecord(
                run_id=submission.run_id,
                state="queued",
                created_at=time.time(),
                mock_mode=submission.mock_mode,
                metadata=submission.metadata,
            )
            store.create(
                record,
                gantry_yaml=submission.gantry_config,
                deck_yaml=submission.deck_config,
                protocol_yaml=submission.protocol_yaml,
            )
            return record

    monkeypatch.setattr(
        campaign_routes,
        "get_settings",
        lambda: SimpleNamespace(configs_dir=configs),
    )
    monkeypatch.setattr(
        campaign_routes, "get_run_manager", lambda: CapturingManager(),
    )

    record = campaign_routes.read_color_target(ColorTargetRequest(
        gantry_file=gantry_path.name,
        deck_file=deck_path.name,
        target_well="plate.A1",
        camera_instrument="camera",
    ))

    snapshot = yaml.safe_load(
        (store.run_dir(record.run_id) / "gantry.yaml").read_text()
    )
    assert snapshot["cnc"]["safe_z"] == 66.5
    assert snapshot["cnc"]["default_feed_rate_mm_min"] == 2000.0
    assert snapshot["instruments"]["camera"]["motion_envelope"] == (
        selected_gantry["instruments"]["camera"]["motion_envelope"]
    )
    assert snapshot["instruments"]["pipette"]["motion_envelope"] == (
        selected_gantry["instruments"]["pipette"]["motion_envelope"]
    )
    assert record.metadata["source_gantry_file"] == gantry_path.name
    assert record.metadata["source_deck_file"] == deck_path.name


def test_color_target_reanalysis_uses_saved_frame_and_persists_revision(
    monkeypatch, tmp_path: Path,
):
    from cubos_api.routers import campaigns as campaign_routes

    image_root = tmp_path / "images"
    image_root.mkdir()
    monkeypatch.setenv("CUBOS_IMAGES_DIR", str(image_root))
    raw_image = image_root / "target.tiff"
    import cv2
    import numpy as np
    assert cv2.imwrite(str(raw_image), np.full((12, 16, 3), 127, dtype=np.uint8))
    initial_preview = image_root / "target.analysis.png"
    assert cv2.imwrite(
        str(initial_preview), np.full((12, 16, 3), 95, dtype=np.uint8),
    )
    manager = get_run_manager()
    acquisition = {
        "requested_capture_profile": {"fingerprint": "capture-profile"},
        "actual_capture_profile": {"fingerprint": "capture-profile"},
        "image_height": None,
    }
    record = RunRecord(
        run_id="target-run",
        state="succeeded",
        created_at=time.time(),
        finished_at=time.time(),
        mock_mode=False,
        metadata={"active_learning_target": "plate.A1"},
        result={"results": [None, {
            "image_path": str(raw_image),
            "annotated_preview_path": str(initial_preview),
            "roi_fraction": 0.5,
            "measurement_status": "rejected",
            "processing_profile": {
                "configuration": {"acquisition": acquisition},
            },
            "frame_metadata": {
                "image_sha256": hashlib.sha256(raw_image.read_bytes()).hexdigest(),
            },
        }]},
    )
    manager.store.create(record, gantry_yaml="g", deck_yaml="d", protocol_yaml="p")
    manager.store.freeze_color_target_source(
        record,
        raw_image,
        allowed_root=image_root,
        capture_sha256=hashlib.sha256(raw_image.read_bytes()).hexdigest(),
        initial_analysis=record.result["results"][1],
        annotated_preview=initial_preview,
    )
    manager.store.write_result(record, record.result)
    manager.store.write(record)
    frozen_image = manager.store.artifact_path("target-run", "color-target-source.tiff")
    raw_image.write_bytes(b"mutated after immutable run copy")

    analysis_calls = []

    def analyze(path, **kwargs):
        assert Path(path) == frozen_image
        assert kwargs["expected_center"] == (0.25, 0.75)
        assert kwargs["acquisition_context"] == acquisition
        analysis_calls.append(len(analysis_calls) + 1)
        source_path = Path(path)
        revised_preview = source_path.with_name(f"{source_path.stem}.analysis.png")
        revised_preview.write_bytes(f"revised preview {analysis_calls[-1]}".encode())
        return {
            "image_path": str(frozen_image),
            "annotated_preview_path": str(revised_preview),
            "measurement_status": "accepted",
            "comparison_status": "not_requested",
            "quality": {"accepted": True},
            "lab": [40.0, 1.0, 2.0],
            "processing_profile": {
                "schema": "cubos.camera-well-cielab.v1", "id": "profile-1",
                "configuration": {
                    "expected_center_normalized": [0.25, 0.75],
                    "expected_center_source": "operator_selected",
                    "roi_fraction_of_detected_radius": 0.5,
                    "acquisition": acquisition,
                },
            },
        }

    monkeypatch.setattr(campaign_routes, "analyze_color_image", analyze)
    app = create_app()
    raw = api_request(app, "GET", "/api/v1/campaigns/color-target/target-run/image")
    response = api_request(
        app,
        "POST",
        "/api/v1/campaigns/color-target/target-run/reanalyze",
        json={"expected_center": [0.25, 0.75], "expected_center_source": "operator_selected"},
    )

    assert raw.status_code == 200
    assert raw.headers["content-type"] == "image/png"
    decoded = cv2.imdecode(np.frombuffer(raw.content, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert decoded is not None and decoded.shape == (12, 16, 3)
    assert response.status_code == 200
    assert response.json()["analysis_revision"] == 1
    saved = manager.get("target-run")
    assert saved.result == record.result
    revision = saved.metadata["color_target_reanalyses"][0]
    assert revision["source_image_sha256"] == response.json()["source_image_sha256"]
    assert manager.store.artifact_path("target-run", revision["json_artifact"]).is_file()
    annotated = api_request(
        app, "GET", "/api/v1/campaigns/color-target/target-run/analysis-image",
    )
    assert annotated.status_code == 200

    from concurrent.futures import ThreadPoolExecutor
    from cubos_api.models.campaigns import ColorTargetReanalysisRequest

    request = ColorTargetReanalysisRequest(
        expected_center=(0.25, 0.75),
        expected_center_source="operator_selected",
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent = list(executor.map(
            lambda _: campaign_routes.reanalyze_color_target("target-run", request),
            range(2),
        ))
    assert {item["analysis_revision"] for item in concurrent} == {2, 3}
    final = manager.get("target-run")
    assert [item["revision"] for item in final.metadata["color_target_reanalyses"]] == [1, 2, 3]
    first_revision = api_request(
        app, "GET",
        "/api/v1/campaigns/color-target/target-run/analysis-image?revision=1",
    )
    missing_revision = api_request(
        app, "GET",
        "/api/v1/campaigns/color-target/target-run/analysis-image?revision=99",
    )
    assert first_revision.content == b"revised preview 1"
    original_revision = api_request(
        app, "GET",
        "/api/v1/campaigns/color-target/target-run/analysis-image?revision=0",
    )
    original_decoded = cv2.imdecode(
        np.frombuffer(original_revision.content, dtype=np.uint8), cv2.IMREAD_UNCHANGED,
    )
    assert original_decoded is not None and original_decoded.shape == (12, 16, 3)
    assert missing_revision.status_code == 404

    from cubos_api.models.campaigns import ColorCampaignSetup

    setup = ColorCampaignSetup(
        gantry_file="g.yaml",
        deck_file="d.yaml",
        source_protocol_file="source.yaml",
        target_run_id="target-run",
        target_analysis_revision=1,
        target_well="plate.A1",
        expected_center=(0.25, 0.75),
        expected_center_source="operator_selected",
        red_source="stocks.A1",
        yellow_source="stocks.A2",
        blue_source="stocks.A3",
        candidate_wells=[f"plate.A{index}" for index in range(2, 8)],
        fluid_state_id=1,
    )
    accepted = campaign_routes._accepted_target_setup(setup)
    assert accepted.target_lab == (40.0, 1.0, 2.0)

    corrupted_metadata = manager.get("target-run")
    corrupted_metadata.metadata["color_target_reanalyses"][0][
        "source_image_sha256"
    ] = "0" * 64
    manager.store.write(corrupted_metadata)
    with pytest.raises(ValueError, match="does not match the frozen captured image"):
        campaign_routes._accepted_target_setup(setup)
    corrupted_metadata.metadata["color_target_reanalyses"][0][
        "source_image_sha256"
    ] = corrupted_metadata.metadata["color_target_source_sha256"]
    manager.store.write(corrupted_metadata)

    frozen_image.chmod(0o644)
    frozen_image.write_bytes(b"corrupted frozen source")
    with pytest.raises(Exception, match="digest does not match"):
        campaign_routes._accepted_target_setup(setup)


def test_color_target_reanalysis_rejects_out_of_root_saved_path(
    monkeypatch, tmp_path: Path,
):
    image_root = tmp_path / "images"
    image_root.mkdir()
    monkeypatch.setenv("CUBOS_IMAGES_DIR", str(image_root))
    outside = tmp_path / "outside.tiff"
    outside.write_bytes(b"outside")
    manager = get_run_manager()
    record = RunRecord(
        run_id="outside-target",
        state="succeeded",
        created_at=time.time(),
        mock_mode=False,
        metadata={"active_learning_target": "plate.A1"},
        result={"results": [{"image_path": str(outside), "roi_fraction": 0.5}]},
    )
    manager.store.create(record, gantry_yaml="g", deck_yaml="d", protocol_yaml="p")
    manager.store.write_result(record, record.result)
    manager.store.write(record)

    response = api_request(
        create_app(), "POST",
        "/api/v1/campaigns/color-target/outside-target/reanalyze",
        json={"expected_center": [0.5, 0.5], "expected_center_source": "operator_selected"},
    )

    assert response.status_code == 409


def test_color_target_reanalysis_real_analyzer_accepts_frozen_run_source(
    monkeypatch, tmp_path: Path,
):
    import cv2
    import numpy as np
    from cubos.optimization import analyze_color_image
    from cubos_api.routers import campaigns as campaign_routes

    image_root = tmp_path / "images"
    image_root.mkdir()
    monkeypatch.setenv("CUBOS_IMAGES_DIR", str(image_root))
    raw_image = image_root / "real-target.tiff"
    frame = np.full((720, 1280, 3), (48, 52, 58), dtype=np.uint8)
    cv2.circle(frame, (640, 360), 46, (58, 151, 208), -1, lineType=cv2.LINE_AA)
    assert cv2.imwrite(str(raw_image), frame)
    acquisition = {
        "requested_capture_profile": {"fingerprint": "stable-capture"},
        "actual_capture_profile": {"fingerprint": "stable-capture"},
        "image_height": None,
    }
    initial = analyze_color_image(
        raw_image,
        roi_fraction=0.5,
        acquisition_context=acquisition,
    )
    digest = hashlib.sha256(raw_image.read_bytes()).hexdigest()
    initial["frame_metadata"] = {"image_sha256": digest}
    initial["well_identity"] = {
        "expected_well": "plate.A1",
        "source": "protocol_position",
        "verification_status": "not_verified_by_cv",
    }
    manager = get_run_manager()
    record = RunRecord(
        run_id="real-analyzer-target",
        state="succeeded",
        created_at=time.time(),
        mock_mode=False,
        metadata={"active_learning_target": "plate.A1"},
        result={"results": [None, initial]},
    )
    manager.store.create(record, gantry_yaml="g", deck_yaml="d", protocol_yaml="p")
    manager.store.freeze_color_target_source(
        record,
        raw_image,
        allowed_root=image_root,
        capture_sha256=digest,
        initial_analysis=initial,
        annotated_preview=Path(initial["annotated_preview_path"]),
    )
    manager.store.write_result(record, record.result)
    manager.store.write(record)

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/campaigns/color-target/real-analyzer-target/reanalyze",
        json={
            "expected_center": [0.5, 0.5],
            "expected_center_source": "operator_selected",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["measurement_status"] == "accepted"
    assert body["quality"]["flags"] == []
    assert body["analysis_revision"] == 1
    stored_preview = Path(body["annotated_preview_path"])
    assert stored_preview == manager.store.artifact_path(
        "real-analyzer-target", "color-target-analysis-1.png",
    )
    assert stored_preview.is_file()
    staging = manager.store.artifact_path(
        "real-analyzer-target", "color-target-source.tiff",
    ).with_name("color-target-source.analysis.png")
    assert not staging.exists()

    outside = tmp_path / "outside-preview.png"
    outside.write_bytes(b"outside bytes must not change")
    staging.symlink_to(outside)
    blocked = api_request(
        create_app(),
        "POST",
        "/api/v1/campaigns/color-target/real-analyzer-target/reanalyze",
        json={
            "expected_center": [0.5, 0.5],
            "expected_center_source": "operator_selected",
        },
    )
    assert blocked.status_code == 409
    assert outside.read_bytes() == b"outside bytes must not change"
    assert staging.is_symlink()
    staging.unlink()

    frozen_source = manager.store.artifact_path(
        "real-analyzer-target", "color-target-source.tiff",
    )
    frozen_bytes = frozen_source.read_bytes()

    def mutate_source(path, **_kwargs):
        Path(path).write_bytes(b"attempted mutation")
        raise AssertionError("read-only frozen source unexpectedly changed")

    monkeypatch.setattr(campaign_routes, "analyze_color_image", mutate_source)
    mutation = api_request(
        create_app(),
        "POST",
        "/api/v1/campaigns/color-target/real-analyzer-target/reanalyze",
        json={
            "expected_center": [0.5, 0.5],
            "expected_center_source": "operator_selected",
        },
    )
    assert mutation.status_code == 409
    assert frozen_source.read_bytes() == frozen_bytes
    assert hashlib.sha256(frozen_bytes).hexdigest() == digest


def test_real_color_setup_rejects_unprofiled_legacy_target(tmp_path: Path):
    from cubos_api.models.campaigns import ColorCampaignSetup
    from cubos_api.routers.campaigns import _accepted_target_setup

    manager = get_run_manager()
    image_root = tmp_path / "legacy-images"
    image_root.mkdir()
    source = image_root / "old.tiff"
    source.write_bytes(b"old image")
    preview = image_root / "old.analysis.png"
    preview.write_bytes(b"old preview")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    analysis = {
        "measurement_status": "accepted",
        "comparison_status": "not_requested",
        "quality": {"accepted": True},
        "lab": [40.0, 1.0, 2.0],
        "processing_profile": {
            "schema": "cubos.camera-well-cielab.v1",
            "id": "legacy-profile",
            "configuration": {
                "expected_center_normalized": [0.5, 0.5],
                "expected_center_source": "operator_selected",
                "roi_fraction_of_detected_radius": 0.5,
                "acquisition": {"status": "unavailable"},
            },
        },
    }
    record = RunRecord(
        run_id="legacy-target",
        state="succeeded",
        created_at=time.time(),
        mock_mode=False,
        metadata={
            "active_learning_target": "plate.A1",
            "color_target_reanalyses": [{
                "revision": 1,
                "source_image_sha256": digest,
                "analysis": analysis,
            }],
        },
        result={"results": [{
            "image_path": str(source),
            "annotated_preview_path": str(preview),
            "frame_metadata": {"image_sha256": digest},
        }]},
    )
    manager.store.create(record, gantry_yaml="g", deck_yaml="d", protocol_yaml="p")
    manager.store.freeze_color_target_source(
        record,
        source,
        allowed_root=image_root,
        capture_sha256=digest,
        initial_analysis=record.result["results"][0],
        annotated_preview=preview,
    )
    manager.store.write(record)
    setup = ColorCampaignSetup(
        gantry_file="g.yaml",
        deck_file="d.yaml",
        source_protocol_file="source.yaml",
        target_run_id="legacy-target",
        target_analysis_revision=1,
        target_well="plate.A1",
        expected_center=(0.5, 0.5),
        expected_center_source="operator_selected",
        red_source="stocks.A1",
        yellow_source="stocks.A2",
        blue_source="stocks.A3",
        candidate_wells=[f"plate.A{index}" for index in range(2, 8)],
        fluid_state_id=1,
    )

    with pytest.raises(ValueError, match="capture profile is missing"):
        _accepted_target_setup(setup)
