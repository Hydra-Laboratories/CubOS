"""Tests for the manual instrument control endpoints."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.api_client import api_request
from cubos.instruments.camera.vendors.opencv import OpenCVCamera
from cubos_api.app import create_app
from cubos_api.routers import gantry as gantry_router
from cubos_api.routers import instruments as instruments_router
from cubos_api.models.camera_monitor import (
    CameraControlsResponse,
    CameraMonitorStatus,
)
from cubos_api.services.camera_monitor import CameraMonitorNotRunning


@pytest.fixture(autouse=True)
def _reset_manual_instruments():
    instruments_router.reset_manual_instruments()
    yield
    instruments_router.reset_manual_instruments()


@pytest.fixture
def connected_session(monkeypatch):
    """Fake a connected gantry session carrying an imaging config."""
    config = {
        "instruments": {
            "lights": {
                "type": "lighting",
                "vendor": "pawduino",
                "port": "",
                "offline": True,
            },
            "camera": {
                "type": "camera",
                "vendor": "flir",
                "camera_id": 0,
                "offline": True,
            },
            "pipette": {
                "type": "pipette",
                "vendor": "opentrons",
                "offline": True,
            },
        }
    }
    session = SimpleNamespace(connected=True, connected_gantry_config=config)
    monkeypatch.setattr(
        instruments_router, "_require_session", lambda: session,
    )
    return session


@pytest.fixture
def images_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CUBOS_IMAGES_DIR", str(tmp_path / "images"))
    return tmp_path / "images"


class TestLighting:
    def test_list_reports_channels_before_any_connect(self, connected_session):
        response = api_request(create_app(), "GET", "/api/v1/instruments/lighting")
        assert response.status_code == 200
        (entry,) = response.json()
        assert entry["instrument"] == "lights"
        assert entry["connected"] is False
        assert entry["channels"]["white"] == [5, 10, 15, 25, 50, 100]
        assert entry["channels"]["contact"] == [5, 10, 20, 30, 50]
        assert entry["active"] == {"white": 0, "contact": 0}

    def test_set_channel_then_all_off(self, connected_session):
        app = create_app()
        response = api_request(
            app, "POST", "/api/v1/instruments/lighting/set",
            json={"instrument": "lights", "channel": "white", "brightness": 25},
        )
        assert response.status_code == 200
        assert response.json()["active"]["white"] == 25

        response = api_request(
            app, "POST", "/api/v1/instruments/lighting/set",
            json={"instrument": "lights", "all_off": True},
        )
        assert response.status_code == 200
        assert response.json()["active"] == {"white": 0, "contact": 0}

    def test_unsupported_level_400(self, connected_session):
        response = api_request(
            create_app(), "POST", "/api/v1/instruments/lighting/set",
            json={"instrument": "lights", "channel": "white", "brightness": 42},
        )
        assert response.status_code == 400
        assert "does not support" in response.json()["detail"]

    def test_unknown_instrument_404(self, connected_session):
        response = api_request(
            create_app(), "POST", "/api/v1/instruments/lighting/set",
            json={"instrument": "nope", "all_off": True},
        )
        assert response.status_code == 404

    def test_wrong_type_400(self, connected_session):
        response = api_request(
            create_app(), "POST", "/api/v1/instruments/lighting/set",
            json={"instrument": "camera", "all_off": True},
        )
        assert response.status_code == 400
        assert "not a lighting instrument" in response.json()["detail"]

    def test_rejected_while_run_active(self, connected_session):
        gantry_router.begin_run()
        try:
            response = api_request(
                create_app(), "POST", "/api/v1/instruments/lighting/set",
                json={"instrument": "lights", "all_off": True},
            )
        finally:
            gantry_router.end_run()
        assert response.status_code == 409

    def test_requires_connected_gantry(self):
        response = api_request(create_app(), "GET", "/api/v1/instruments/lighting")
        assert response.status_code == 400


class TestCamera:
    def test_capture_and_last_image(self, connected_session, images_dir):
        app = create_app()
        response = api_request(
            app, "POST", "/api/v1/instruments/camera/capture",
            json={"instrument": "camera", "label": "focus-check"},
        )
        assert response.status_code == 200
        image_path = response.json()["image_path"]
        assert "manual" in image_path and image_path.endswith(".png")

        listing = api_request(app, "GET", "/api/v1/instruments/camera")
        (entry,) = listing.json()
        assert entry["last_image"] == image_path

        image = api_request(
            app, "GET", "/api/v1/instruments/camera/last-image",
            params={"instrument": "camera"},
        )
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/png"
        assert image.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_last_image_404_before_any_capture(self, connected_session):
        response = api_request(
            create_app(), "GET", "/api/v1/instruments/camera/last-image",
            params={"instrument": "camera"},
        )
        assert response.status_code == 404

    def test_capture_rejected_while_run_active(self, connected_session, images_dir):
        gantry_router.begin_run()
        try:
            response = api_request(
                create_app(), "POST", "/api/v1/instruments/camera/capture",
                json={"instrument": "camera"},
            )
        finally:
            gantry_router.end_run()
        assert response.status_code == 409

    def test_preview_capture_overwrites_one_fixed_file(self, connected_session, images_dir):
        """Live-preview polling shouldn't pile up a new PNG on disk every tick."""
        app = create_app()
        first = api_request(
            app, "POST", "/api/v1/instruments/camera/capture",
            json={"instrument": "camera", "preview": True},
        )
        second = api_request(
            app, "POST", "/api/v1/instruments/camera/capture",
            json={"instrument": "camera", "preview": True},
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["image_path"] == second.json()["image_path"]
        manual_dir = images_dir / "manual"
        assert list(manual_dir.glob("*.png")) == [manual_dir / "camera_preview.png"]

    def test_preview_and_manual_captures_are_independent_files(self, connected_session, images_dir):
        app = create_app()
        preview = api_request(
            app, "POST", "/api/v1/instruments/camera/capture",
            json={"instrument": "camera", "preview": True},
        )
        manual = api_request(
            app, "POST", "/api/v1/instruments/camera/capture",
            json={"instrument": "camera"},
        )
        assert preview.json()["image_path"] != manual.json()["image_path"]

    def test_preview_does_not_clobber_last_manual_capture(self, connected_session, images_dir):
        app = create_app()
        manual = api_request(
            app, "POST", "/api/v1/instruments/camera/capture",
            json={"instrument": "camera"},
        )
        api_request(
            app, "POST", "/api/v1/instruments/camera/capture",
            json={"instrument": "camera", "preview": True},
        )
        listing = api_request(app, "GET", "/api/v1/instruments/camera")
        (entry,) = listing.json()
        assert entry["last_image"] == manual.json()["image_path"]

    def test_camera_without_capture_support_returns_501(self, monkeypatch, images_dir):
        config = {
            "instruments": {
                "camera": {"type": "camera", "vendor": "mount_only", "offline": True},
            }
        }
        session = SimpleNamespace(connected=True, connected_gantry_config=config)
        monkeypatch.setattr(instruments_router, "_require_session", lambda: session)

        response = api_request(
            create_app(), "POST", "/api/v1/instruments/camera/capture",
            json={"instrument": "camera", "preview": True},
        )
        assert response.status_code == 501
        assert "does not support capture" in response.json()["detail"]


@pytest.fixture
def opencv_session(monkeypatch):
    config = {
        "instruments": {
            "camera": {
                "type": "camera",
                "vendor": "opencv",
                "camera_id": 0,
                "resolution_width": 1280,
                "resolution_height": 720,
                "pixel_format": "MJPG",
            },
            "other_camera": {
                "type": "camera",
                "vendor": "opencv",
                "camera_id": 1,
            },
            "pipette": {
                "type": "pipette",
                "vendor": "opentrons",
                "offline": True,
            },
        }
    }
    session = SimpleNamespace(connected=True, connected_gantry_config=config)
    monkeypatch.setattr(instruments_router, "_require_session", lambda: session)
    return session


class FakeMonitorService:
    def __init__(self):
        self.running = False
        self.started = []
        self.stopped = []
        self.updates = []

    def is_running(self, instrument):
        return self.running

    def status(self, instrument):
        return CameraMonitorStatus(
            instrument=instrument,
            state="running" if self.running else "stopped",
            connected=self.running,
        )

    def start(self, instrument, camera, *, configuration):
        self.running = True
        self.started.append((instrument, camera, configuration))
        status = self.status(instrument)
        status.lease_id = "lease-1"
        status.lease_expires_at = 115.0
        status.subscriber_count = 1
        return status

    def heartbeat(self, instrument, lease_id):
        status = self.status(instrument)
        status.lease_id = lease_id
        status.lease_expires_at = 120.0
        status.subscriber_count = 1
        return status

    def stop(self, instrument, lease_id):
        self.running = False
        self.stopped.append((instrument, lease_id))
        return self.status(instrument)

    def frame(self, instrument, requested_frame_id=None):
        return b"jpeg", 12, 100.5

    def controls(self, instrument):
        if not self.running:
            raise CameraMonitorNotRunning("monitor is not running")
        return CameraControlsResponse(
            instrument=instrument,
            controls={
                "focus": {"supported": False, "value": None, "error": "unsupported"}
            },
            control_fingerprint="profile-1",
        )

    def set_controls(self, instrument, controls):
        self.updates.append((instrument, controls))
        return CameraControlsResponse(
            instrument=instrument,
            controls={
                name: {"supported": True, "value": value, "error": None}
                for name, value in controls.items()
            },
            control_fingerprint="profile-2",
        )

    def stop_all(self):
        self.running = False


class TestCameraMonitor:
    def test_get_status_never_builds_or_connects_camera(
        self, monkeypatch, opencv_session,
    ):
        service = FakeMonitorService()
        monkeypatch.setattr(
            instruments_router,
            "get_camera_monitor_service",
            lambda: service,
        )
        monkeypatch.setattr(
            instruments_router,
            "_build_instrument",
            lambda *args: pytest.fail("GET must not build hardware"),
        )

        response = api_request(
            create_app(),
            "GET",
            "/api/v1/instruments/camera/monitor",
            params={"instrument": "camera"},
        )

        assert response.status_code == 200
        assert response.json()["state"] == "stopped"

    def test_start_builds_only_the_explicitly_requested_camera(
        self, monkeypatch, opencv_session,
    ):
        service = FakeMonitorService()
        built = []
        camera = object.__new__(OpenCVCamera)
        monkeypatch.setattr(
            instruments_router,
            "get_camera_monitor_service",
            lambda: service,
        )
        monkeypatch.setattr(
            instruments_router,
            "_build_instrument",
            lambda name, entry: built.append((name, entry["camera_id"])) or camera,
        )

        response = api_request(
            create_app(),
            "POST",
            "/api/v1/instruments/camera/monitor/start",
            json={"instrument": "other_camera"},
        )

        assert response.status_code == 200
        assert built == [("other_camera", 1)]
        assert service.started == [(
            "other_camera",
            camera,
            ("opencv", 1, 1280, 720, None, ""),
        )]

    def test_frame_get_uses_existing_lease_and_disables_cache(
        self, monkeypatch, opencv_session,
    ):
        service = FakeMonitorService()
        service.running = True
        monkeypatch.setattr(
            instruments_router,
            "get_camera_monitor_service",
            lambda: service,
        )

        response = api_request(
            create_app(),
            "GET",
            "/api/v1/instruments/camera/monitor/frame",
            params={"instrument": "camera", "frame_id": 12},
        )

        assert response.status_code == 200
        assert response.content == b"jpeg"
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-cubos-frame-id"] == "12"
        assert response.headers["x-cubos-received-at"] == "100.5"

    def test_controls_get_does_not_auto_start_camera(
        self, monkeypatch, opencv_session,
    ):
        service = FakeMonitorService()
        monkeypatch.setattr(
            instruments_router,
            "get_camera_monitor_service",
            lambda: service,
        )

        response = api_request(
            create_app(),
            "GET",
            "/api/v1/instruments/camera/controls",
            params={"instrument": "camera"},
        )

        assert response.status_code == 409
        assert service.started == []

    def test_patch_controls_sends_only_explicit_fields(
        self, monkeypatch, opencv_session,
    ):
        service = FakeMonitorService()
        service.running = True
        monkeypatch.setattr(
            instruments_router,
            "get_camera_monitor_service",
            lambda: service,
        )

        response = api_request(
            create_app(),
            "PATCH",
            "/api/v1/instruments/camera/controls",
            json={"instrument": "camera", "controls": {"brightness": 9.0}},
        )

        assert response.status_code == 200
        assert service.updates == [("camera", {"brightness": 9.0})]

    def test_patch_controls_can_enable_auto_white_balance_without_stale_temperature(
        self, monkeypatch, opencv_session,
    ):
        service = FakeMonitorService()
        service.running = True
        monkeypatch.setattr(
            instruments_router,
            "get_camera_monitor_service",
            lambda: service,
        )

        response = api_request(
            create_app(),
            "PATCH",
            "/api/v1/instruments/camera/controls",
            json={"instrument": "camera", "controls": {"auto_white_balance": True}},
        )

        assert response.status_code == 200
        assert service.updates == [("camera", {"auto_white_balance": True})]

    def test_patch_controls_rejects_auto_and_manual_white_balance_together(
        self, monkeypatch, opencv_session,
    ):
        service = FakeMonitorService()
        service.running = True
        monkeypatch.setattr(
            instruments_router,
            "get_camera_monitor_service",
            lambda: service,
        )

        response = api_request(
            create_app(),
            "PATCH",
            "/api/v1/instruments/camera/controls",
            json={
                "instrument": "camera",
                "controls": {
                    "auto_white_balance": True,
                    "white_balance": 4600.0,
                },
            },
        )

        assert response.status_code == 422
        assert service.updates == []

    def test_patch_controls_is_rejected_during_active_run(
        self, monkeypatch, opencv_session,
    ):
        service = FakeMonitorService()
        service.running = True
        monkeypatch.setattr(
            instruments_router,
            "get_camera_monitor_service",
            lambda: service,
        )
        gantry_router.begin_run()
        try:
            response = api_request(
                create_app(),
                "PATCH",
                "/api/v1/instruments/camera/controls",
                json={"instrument": "camera", "controls": {"brightness": 9.0}},
            )
        finally:
            gantry_router.end_run()

        assert response.status_code == 409
        assert service.updates == []

    def test_patch_controls_is_rejected_during_campaign_reservation(
        self, monkeypatch, opencv_session,
    ):
        from cubos_api.services import run_manager

        service = FakeMonitorService()
        service.running = True
        monkeypatch.setattr(run_manager, "active_campaign_owner", lambda: "campaign-1")
        monkeypatch.setattr(
            instruments_router,
            "get_camera_monitor_service",
            lambda: service,
        )

        response = api_request(
            create_app(),
            "PATCH",
            "/api/v1/instruments/camera/controls",
            json={"instrument": "camera", "controls": {"brightness": 9.0}},
        )

        assert response.status_code == 409
        assert service.updates == []

    def test_monitor_start_is_allowed_during_campaign_reservation(
        self, monkeypatch, opencv_session,
    ):
        from cubos_api.services import run_manager

        service = FakeMonitorService()
        camera = object.__new__(OpenCVCamera)
        monkeypatch.setattr(run_manager, "active_campaign_owner", lambda: "campaign-1")
        monkeypatch.setattr(
            instruments_router,
            "get_camera_monitor_service",
            lambda: service,
        )
        monkeypatch.setattr(
            instruments_router,
            "_build_instrument",
            lambda name, entry: camera,
        )

        response = api_request(
            create_app(),
            "POST",
            "/api/v1/instruments/camera/monitor/start",
            json={"instrument": "camera"},
        )

        assert response.status_code == 200
        assert service.started[0][0] == "camera"

    def test_heartbeat_and_stop_are_scoped_to_lease(
        self, monkeypatch, opencv_session,
    ):
        service = FakeMonitorService()
        service.running = True
        monkeypatch.setattr(
            instruments_router,
            "get_camera_monitor_service",
            lambda: service,
        )
        app = create_app()

        heartbeat = api_request(
            app,
            "POST",
            "/api/v1/instruments/camera/monitor/heartbeat",
            json={"instrument": "camera", "lease_id": "lease-a"},
        )
        stopped = api_request(
            app,
            "POST",
            "/api/v1/instruments/camera/monitor/stop",
            json={"instrument": "camera", "lease_id": "lease-a"},
        )

        assert heartbeat.status_code == 200
        assert heartbeat.json()["lease_id"] == "lease-a"
        assert stopped.status_code == 200
        assert service.stopped == [("camera", "lease-a")]

    def test_campaign_reservation_still_blocks_manual_capture(
        self, monkeypatch, opencv_session,
    ):
        from cubos_api.services import run_manager

        monkeypatch.setattr(run_manager, "active_campaign_owner", lambda: "campaign-1")

        response = api_request(
            create_app(),
            "POST",
            "/api/v1/instruments/camera/capture",
            json={"instrument": "camera", "preview": True},
        )

        assert response.status_code == 409

    def test_start_rejects_non_opencv_vendor(self, connected_session):
        response = api_request(
            create_app(),
            "POST",
            "/api/v1/instruments/camera/monitor/start",
            json={"instrument": "camera"},
        )

        assert response.status_code == 501
