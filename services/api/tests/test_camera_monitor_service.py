from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from cubos.instruments.camera.exceptions import CameraCaptureError
from cubos.instruments.camera.frame_broker import CameraFrame
from cubos.instruments.camera.vendors.opencv import OpenCVCamera
from cubos_api.services.camera_monitor import (
    CameraMonitorError,
    CameraMonitorFrameExpired,
    CameraMonitorNotRunning,
    CameraMonitorService,
    CameraMonitorUnsupported,
    _active_step,
    _active_execution_context,
    _expected_well,
)


class FakeMonitorCamera(OpenCVCamera):
    def __init__(self, *, frame: CameraFrame | None = None) -> None:
        self.camera_id = 0
        self.resolution = (1280, 720)
        self.pixel_format = "MJPG"
        self.connected = False
        self.connect_count = 0
        self.disconnect_count = 0
        self.controls = {
            "exposure": {"supported": True, "value": 156.0, "error": None},
            "white_balance": {"supported": True, "value": 4600.0, "error": None},
            "focus": {"supported": False, "value": None, "error": "unsupported"},
            "brightness": {"supported": True, "value": 0.0, "error": None},
        }
        self.frame = frame or CameraFrame(
            frame_id=7,
            received_at=100.0,
            data="FRAME",
            width=800,
            height=600,
            configuration_revision=0,
            capture_metadata={},
        )

    def connect(self) -> None:
        self.connect_count += 1
        self.connected = True

    def disconnect(self) -> None:
        self.disconnect_count += 1
        self.connected = False

    def health_check(self) -> bool:
        return self.connected

    def latest_frame(self, **kwargs):
        if self.frame is None:
            raise CameraCaptureError("no frame")
        return self.frame

    def encode_jpeg(self, frame):
        return b"jpeg"

    def control_status(self):
        return self.controls

    def set_controls(self, values):
        for name, value in values.items():
            self.controls[name] = {
                "supported": True,
                "value": float(value),
                "error": None,
            }
        return self.controls

    def control_fingerprint(self):
        return {
            "fingerprint": "profile-1",
            "actual_pixel_format": "YUYV",
            "configuration_revision": 0,
        }


def test_start_status_frame_and_stop_have_bounded_monitor_lifecycle():
    camera = FakeMonitorCamera()
    context = {
        "run_id": "run-1",
        "campaign_id": "campaign-1",
        "trial_number": 2,
        "step_index": 11,
        "step_command": "measure_color",
        "expected_well": "plate.A3",
        "expected_center": {"x": 0.5, "y": 0.5},
        "expected_center_source": "frame_center",
        "analysis": {
            "image_path": "/images/prior.tiff",
            "roi": {"center_x_px": 455.0, "center_y_px": 317.0},
            "quality": {"status": "accepted", "glare_fraction": 0.01},
            "processing_profile": {"id": "color-profile"},
        },
    }
    service = CameraMonitorService(
        context_provider=lambda: context,
        clock=lambda: 101.0,
    )

    status = service.start("camera", camera, configuration=(0, 1280, 720, "MJPG"))

    assert camera.connect_count == 1
    assert status.state == "running"
    assert status.frame_id == 7
    assert status.received_at == 100.0
    assert status.frame_age_seconds == 1.0
    assert status.requested_resolution.model_dump() == {"width": 1280, "height": 720}
    assert status.actual_resolution.model_dump() == {"width": 800, "height": 600}
    assert status.actual_pixel_format == "YUYV"
    assert status.expected_well == "plate.A3"
    assert status.expected_center.model_dump() == {"x": 0.5, "y": 0.5}
    assert status.expected_center_source == "frame_center"
    assert status.well_identity_verification == "not_verified_by_cv"
    assert status.roi is None
    assert status.latest_analysis["roi"] == context["analysis"]["roi"]
    assert status.analysis_is_current_frame is False
    assert status.lease_id
    assert status.subscriber_count == 1
    assert status.image_url.endswith("instrument=camera&frame_id=7")
    assert any("configured resolution" in warning for warning in status.warnings)
    assert service.frame("camera") == (b"jpeg", 7, 100.0)

    stopped = service.stop("camera", status.lease_id)
    assert stopped.state == "stopped"
    assert camera.disconnect_count == 1
    with pytest.raises(CameraMonitorNotRunning):
        service.frame("camera")


def test_start_is_idempotent_for_same_running_monitor():
    camera = FakeMonitorCamera()
    service = CameraMonitorService(context_provider=dict)

    first = service.start("camera", camera, configuration="same")
    second = service.start("camera", FakeMonitorCamera(), configuration="same")

    assert camera.connect_count == 1
    assert second.subscriber_count == 2
    service.stop("camera", first.lease_id)
    assert camera.disconnect_count == 0
    assert service.status("camera").subscriber_count == 1
    service.stop("camera", second.lease_id)
    assert camera.disconnect_count == 1


def test_non_opencv_monitor_is_explicitly_unsupported():
    service = CameraMonitorService(context_provider=dict)

    with pytest.raises(CameraMonitorUnsupported, match="OpenCV"):
        service.start("camera", object(), configuration="config")


def test_initial_frame_failure_releases_device_and_records_failure():
    camera = FakeMonitorCamera()
    camera.frame = None
    service = CameraMonitorService(context_provider=dict)

    with pytest.raises(CameraMonitorError, match="no frame"):
        service.start("camera", camera, configuration="config")

    assert camera.disconnect_count == 1
    status = service.status("camera")
    assert status.state == "failed"
    assert "CameraCaptureError" in status.error


def test_controls_apply_only_supplied_values():
    camera = FakeMonitorCamera()
    service = CameraMonitorService(context_provider=dict)
    lease = service.start("camera", camera, configuration="config")

    response = service.set_controls("camera", {"brightness": 12.0})

    assert response.controls["brightness"].value == 12.0
    assert response.controls["exposure"].value == 156.0
    assert response.controls["focus"].supported is False
    service.stop("camera", lease.lease_id)


def test_stale_frame_is_not_served():
    camera = FakeMonitorCamera()
    service = CameraMonitorService(
        context_provider=dict,
        clock=lambda: 110.0,
        maximum_frame_age_s=3.0,
    )
    lease = service.start("camera", camera, configuration="config")

    with pytest.raises(CameraMonitorError, match="stale"):
        service.frame("camera")
    service.stop("camera", lease.lease_id)


def test_monitor_lease_expires_and_releases_camera():
    camera = FakeMonitorCamera()
    service = CameraMonitorService(
        context_provider=dict,
        lease_ttl_s=0.03,
    )
    service.start("camera", camera, configuration="config")

    deadline = time.monotonic() + 0.5
    while camera.disconnect_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert camera.disconnect_count == 1
    assert service.status("camera").state == "stopped"


def test_heartbeat_renews_only_the_matching_lease():
    camera = FakeMonitorCamera()
    service = CameraMonitorService(context_provider=dict, lease_ttl_s=1.0)
    lease = service.start("camera", camera, configuration="config")

    renewed = service.heartbeat("camera", lease.lease_id)

    assert renewed.lease_id == lease.lease_id
    assert renewed.lease_expires_at >= lease.lease_expires_at
    with pytest.raises(CameraMonitorNotRunning, match="missing or expired"):
        service.heartbeat("camera", "not-this-client")
    service.stop("camera", lease.lease_id)


def test_running_monitor_rejects_conflicting_capture_configuration():
    camera = FakeMonitorCamera()
    service = CameraMonitorService(context_provider=dict)
    lease = service.start("camera", camera, configuration="first")

    with pytest.raises(CameraMonitorError, match="different capture configuration"):
        service.start("camera", FakeMonitorCamera(), configuration="second")

    service.stop("camera", lease.lease_id)


def test_frame_endpoint_returns_exact_cached_status_frame():
    camera = FakeMonitorCamera()
    service = CameraMonitorService(context_provider=dict, clock=lambda: 101.0)
    lease = service.start("camera", camera, configuration="config")
    camera.frame = CameraFrame(
        frame_id=8,
        received_at=101.0,
        data="NEW",
        width=800,
        height=600,
        configuration_revision=0,
        capture_metadata={},
    )

    _, frame_id, _ = service.frame("camera", requested_frame_id=7)

    assert frame_id == 7
    with pytest.raises(CameraMonitorFrameExpired, match="no longer available"):
        service.frame("camera", requested_frame_id=6)
    service.stop("camera", lease.lease_id)


def test_analysis_overlay_requires_exact_run_and_frame_provenance():
    camera = FakeMonitorCamera()
    analysis = {
        "_source_run_id": "run-1",
        "frame_metadata": {"frame_id": 7, "received_at": 100.0},
        "well_identity": {"expected_well": "plate.A3"},
        "roi": {"center_x_px": 455.0, "center_y_px": 317.0},
        "quality": {"status": "accepted"},
        "processing_profile": {
            "configuration": {
                "acquisition": {
                    "actual_capture_profile": {"fingerprint": "older-profile"},
                },
            },
        },
    }
    service = CameraMonitorService(
        context_provider=lambda: {"run_id": "run-1", "analysis": analysis},
        clock=lambda: 101.0,
    )
    lease = service.start("camera", camera, configuration="config")

    assert lease.analysis_is_current_frame is True
    assert lease.roi == analysis["roi"]
    assert lease.analysis_source_run_id == "run-1"
    assert lease.analysis_source_well == "plate.A3"
    assert lease.analysis_source_frame_id == 7
    assert "_source_run_id" not in lease.latest_analysis
    assert any("capture a new reference" in warning for warning in lease.warnings)
    service.stop("camera", lease.lease_id)


def test_analysis_image_path_accepts_only_real_paths_inside_image_root(
    tmp_path, monkeypatch,
):
    root = tmp_path / "images"
    root.mkdir()
    image = root / "annotated.png"
    image.write_bytes(b"png")
    monkeypatch.setenv("CUBOS_IMAGES_DIR", str(root))
    context = {
        "analysis": {
            "annotated_preview_path": str(image),
            "_source_instrument": "camera",
        },
    }
    service = CameraMonitorService(context_provider=lambda: context)

    assert service.analysis_image_path("camera") == image.resolve()

    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png")
    context["analysis"]["annotated_preview_path"] = str(outside)
    with pytest.raises(CameraMonitorError, match="outside"):
        service.analysis_image_path("camera")

    escape = root / "escape.png"
    escape.symlink_to(outside)
    context["analysis"]["annotated_preview_path"] = str(escape)
    with pytest.raises(CameraMonitorError, match="outside"):
        service.analysis_image_path("camera")


def test_analysis_image_rejects_different_camera_source(tmp_path, monkeypatch):
    root = tmp_path / "images"
    root.mkdir()
    image = root / "annotated.png"
    image.write_bytes(b"png")
    monkeypatch.setenv("CUBOS_IMAGES_DIR", str(root))
    service = CameraMonitorService(context_provider=lambda: {
        "analysis": {
            "annotated_preview_path": str(image),
            "_source_instrument": "camera-2",
        },
    })

    with pytest.raises(CameraMonitorError, match="different camera"):
        service.analysis_image_path("camera-1")


def test_active_step_tracks_nested_scopes_without_claiming_completed_step():
    events = [
        SimpleNamespace(kind="step", data={"index": 3, "command": "transfer", "substep": None, "outcome": "started"}),
        SimpleNamespace(kind="step", data={"index": 3, "command": "transfer", "substep": "stroke0:aspirate", "outcome": "started"}),
        SimpleNamespace(kind="step", data={"index": 3, "command": "transfer", "substep": "stroke0:aspirate", "outcome": "completed"}),
    ]
    assert _active_step(events) == {
        "step_index": 3,
        "step_command": "transfer",
        "step_substep": None,
    }
    events.append(SimpleNamespace(kind="step", data={"index": 3, "command": "transfer", "substep": None, "outcome": "completed"}))
    assert _active_step(events) is None


def test_expected_well_comes_only_from_stored_protocol_step(tmp_path):
    path = tmp_path / "protocol.yaml"
    path.write_text(
        "protocol:\n"
        "  - transfer:\n"
        "      source: stocks.A1\n"
        "      destination: plate.B4\n"
        "      volume_ul: 50\n",
        encoding="utf-8",
    )

    assert _expected_well(path, 0, "stroke0:aspirate") == "stocks.A1"
    assert _expected_well(path, 0, "stroke0:dispense") == "plate.B4"
    assert _expected_well(path, 0, None) == "plate.B4"


def test_active_context_uses_native_run_without_campaign_imports(
    tmp_path, monkeypatch,
):
    from cubos_api.services import run_manager

    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "protocol.yaml").write_text(
        "protocol:\n"
        "  - measure_color:\n"
        "      instrument: camera\n"
        "      position: plate.A3\n",
        encoding="utf-8",
    )
    event = SimpleNamespace(
        kind="step",
        data={
            "index": 0,
            "command": "measure_color",
            "substep": None,
            "outcome": "started",
        },
    )
    fake_runs = SimpleNamespace(
        active_run_id="run-1",
        reservation_owner="campaign-1",
        get=lambda run_id: SimpleNamespace(
            metadata={
                "active_learning_campaign_id": "campaign-1",
                "trial": 2,
            },
        ),
        store=SimpleNamespace(
            events=lambda run_id: [event],
            run_dir=lambda run_id: tmp_path / "runs" / run_id,
        ),
    )
    monkeypatch.setattr(run_manager, "get_run_manager", lambda: fake_runs)
    context = _active_execution_context()
    assert context["expected_well"] == "plate.A3"
    assert context["run_id"] == "run-1"
    assert "analysis" not in context
