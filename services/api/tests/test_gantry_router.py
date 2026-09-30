"""Test gantry API endpoints delegate to CubOS ``GantrySession``."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cubos.gantry.session import (
    CalibrationBlockedError,
    GantryAlarmError,
    GantryNotConnectedError,
    GantryPositionSnapshot,
    GantrySessionError,
    GantrySessionHealthCheckError,
    InterruptFeedHoldTimeoutError,
    MovementOutOfBoundsError,
)

from tests.api_client import api_request
from cubos_api.app import create_app
from cubos_api.config import get_settings
from cubos_api.routers import gantry as gantry_router
from cubos_api.services.yaml_io import read_yaml, write_yaml


VALID_GANTRY = {
    "serial_port": "",
    "gantry_type": "cub_xl",
    "cnc": {
        "factory_z_travel_mm": 110.0,
        "calibration_block_height_mm": 35.0,
    },
    "working_volume": {
        "x_min": 0.0,
        "x_max": 300.0,
        "y_min": 0.0,
        "y_max": 200.0,
        "z_min": 0.0,
        "z_max": 80.0,
    },
    "grbl_settings": {
        "soft_limits": True,
        "homing_enable": True,
        "max_travel_x": 310.0,
        "max_travel_y": 210.0,
        "max_travel_z": 90.0,
    },
    "instruments": {},
}


class FakeSession:
    def __init__(
        self,
        snapshot: GantryPositionSnapshot | None = None,
        *,
        connected: bool = True,
    ):
        # Defaults to True because most tests assign a FakeSession directly
        # to gantry_router._session to simulate an *already connected*
        # session. Tests that go through the connect() flow (via a
        # GantrySession factory) should pass connected=False, mirroring
        # CubOS's real GantrySession where `.connected` reflects whether
        # `_gantry` has been set by a successful connect() call.
        self.connected = connected
        self.snapshot = snapshot or GantryPositionSnapshot(
            x=1.0,
            y=2.0,
            z=3.0,
            work_x=1.0,
            work_y=2.0,
            work_z=3.0,
            status="Idle",
            connected=True,
        )
        self.calls: list[tuple[str, object]] = []
        self._calibration_active = False

    @property
    def calibration_active(self) -> bool:
        return self._calibration_active

    def position(self):
        self.calls.append(("position", None))
        return self.snapshot

    def home(self):
        self.calls.append(("home", None))
        return self.snapshot

    def resume(self):
        self.calls.append(("resume", None))
        return self.snapshot

    def connect(self, path=None, *, filename=None):
        self.calls.append(("connect", (path, filename)))
        self.connected = True
        return replace(self.snapshot, calibration_warning=None)

    def disconnect(self):
        self.connected = False
        self.calls.append(("disconnect", None))
        return GantryPositionSnapshot(connected=False, status="Disconnected")

    def jog(self, **kwargs):
        self.calls.append(("jog", kwargs))

    def move_to(self, **kwargs):
        self.calls.append(("move_to", kwargs))

    def move_to_blocking(self, **kwargs):
        self.calls.append(("move_to_blocking", kwargs))
        return self.snapshot

    def jog_blocking(self, **kwargs):
        self.calls.append(("jog_blocking", kwargs))
        return self.snapshot

    def set_work_coordinates(self, **kwargs):
        self.calls.append(("set_work_coordinates", kwargs))
        return self.snapshot

    def configure_soft_limits(self, **kwargs):
        self.calls.append(("configure_soft_limits", kwargs))

    def prepare_calibration_origin(self):
        self.calls.append(("prepare_calibration_origin", None))
        return self.snapshot

    def calibration_home_and_center(self):
        self.calls.append(("calibration_home_and_center", None))
        return type(
            "Result",
            (),
            {
                "xy_bounds": {"x": 300.0, "y": 200.0, "z": 80.0},
                "position": {"x": 150.0, "y": 100.0, "z": 80.0},
            },
        )()

    def restore_calibration_soft_limits(self):
        self.calls.append(("restore_calibration_soft_limits", None))
        return self.snapshot

    def finalize_calibration_origin(self, **kwargs):
        self.calls.append(("finalize_calibration_origin", kwargs))
        return type(
            "Result",
            (),
            {
                "measured_volume": {"x": 300.0, "y": 200.0, "z": 80.0},
                "z_calibration": {"z_max": 80.0},
                "max_travel": {"x": 310.0, "y": 210.0, "z": 90.0},
                "position": {"x": 300.0, "y": 200.0, "z": 80.0},
                "homing_pull_off_mm": 10.0,
            },
        )()

    def recover_calibration_limit(self, **kwargs):
        self.calls.append(("recover_calibration_limit", kwargs))
        result = type(
            "Result",
            (),
            {
                "attempts": 2,
                "pull_off_delta": {"x": 0.0, "y": 0.0, "z": 5.0},
            },
        )()
        return result, ["recovered by CubOS"]

    def unlock(self):
        self.calls.append(("unlock", None))
        return self.snapshot

    def reset_and_unlock(self):
        self.calls.append(("reset_and_unlock", None))
        return self.snapshot

    def feed_hold(self):
        self.calls.append(("feed_hold", None))
        return self.snapshot

    def jog_cancel(self):
        self.calls.append(("jog_cancel", None))
        return self.snapshot

    def read_grbl_settings(self):
        self.calls.append(("read_grbl_settings", None))
        return {"$20": "1", "$130": "300.0"}

    def set_grbl_setting(self, setting, value):
        self.calls.append(("set_grbl_setting", (setting, value)))
        return {"$20": "0"}

    def refresh_connected_config(self, filename, config):
        self.calls.append(("refresh_connected_config", (filename, config)))

    def feed_hold_interrupt(self):
        self.calls.append(("feed_hold_interrupt", None))

    def jog_cancel_interrupt(self):
        self.calls.append(("jog_cancel_interrupt", None))

    def run_protocol(self, **kwargs):
        self.calls.append(("run_protocol", kwargs))
        return type(
            "RunResult",
            (),
            {"status": "ok", "steps_executed": 2, "campaign_id": 123},
        )()


@pytest.fixture(autouse=True)
def reset_gantry_router_state(monkeypatch):
    monkeypatch.setattr(gantry_router, "_session", None)
    yield


def test_position_returns_not_connected_without_session():
    response = api_request(create_app(), "GET", "/api/v1/gantry/position")

    assert response.status_code == 200
    assert response.json()["connected"] is False
    assert response.json()["status"] == "Not connected"
    assert response.json()["calibration_active"] is False


def test_list_configs_and_schema_metadata(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    gantry_dir = config_dir / "gantry"
    gantry_dir.mkdir(parents=True)
    write_yaml(gantry_dir / "test.yaml", VALID_GANTRY)
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)

    configs = api_request(create_app(), "GET", "/api/v1/gantry/configs")
    pipettes = api_request(create_app(), "GET", "/api/v1/gantry/pipette-models")
    schemas = api_request(create_app(), "GET", "/api/v1/gantry/instrument-schemas")

    assert configs.status_code == 200
    assert configs.json() == ["test.yaml"]
    assert pipettes.status_code == 200
    assert any(item["channels"] >= 1 for item in pipettes.json())
    assert schemas.status_code == 200
    assert "asmi" in schemas.json()
    assert "vernier" in schemas.json()["asmi"]


def test_home_endpoint_delegates_to_gantry_session(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(gantry_router, "_session", session)

    response = api_request(create_app(), "POST", "/api/v1/gantry/home")

    assert response.status_code == 200
    assert ("home", None) in session.calls


def test_connected_position_delegates_to_session(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(gantry_router, "_session", session)

    response = api_request(create_app(), "GET", "/api/v1/gantry/position")

    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert response.json()["calibration_active"] is False
    assert ("position", None) in session.calls


def test_connected_position_reports_active_calibration_flags(monkeypatch):
    session = FakeSession()
    session._calibration_active = True
    monkeypatch.setattr(gantry_router, "_session", session)

    response = api_request(create_app(), "GET", "/api/v1/gantry/position")

    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert response.json()["calibration_active"] is True


def test_protected_endpoint_requires_connected_session(monkeypatch):
    monkeypatch.setattr(gantry_router, "_session", None)

    response = api_request(create_app(), "POST", "/api/v1/gantry/home")

    assert response.status_code == 400
    assert "Gantry not connected" in response.text


def test_connect_uses_selected_gantry_config(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    gantry_dir = config_dir / "gantry"
    gantry_dir.mkdir(parents=True)
    write_yaml(gantry_dir / "aaa_first.yaml", {**VALID_GANTRY, "serial_port": "/dev/wrong"})
    write_yaml(gantry_dir / "selected.yaml", {**VALID_GANTRY, "serial_port": "/dev/right"})
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)

    created: list[FakeSession] = []

    class FakeSessionFactory(FakeSession):
        def __init__(self):
            super().__init__(connected=False)
            created.append(self)

    monkeypatch.setattr(gantry_router, "GantrySession", FakeSessionFactory)

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/gantry/connect",
        json={"filename": "selected.yaml"},
    )

    assert response.status_code == 200
    path, filename = created[0].calls[0][1]
    assert filename == "selected.yaml"
    assert path == gantry_dir / "selected.yaml"


def test_connect_uses_first_config_when_filename_is_omitted(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    gantry_dir = config_dir / "gantry"
    gantry_dir.mkdir(parents=True)
    write_yaml(gantry_dir / "aaa_first.yaml", VALID_GANTRY)
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)
    created: list[FakeSession] = []

    class FakeSessionFactory(FakeSession):
        def __init__(self):
            super().__init__(connected=False)
            created.append(self)

    monkeypatch.setattr(gantry_router, "GantrySession", FakeSessionFactory)

    response = api_request(create_app(), "POST", "/api/v1/gantry/connect", json={})

    assert response.status_code == 200
    assert created[0].calls[0][1] == (gantry_dir / "aaa_first.yaml", "aaa_first.yaml")


def test_connect_without_any_config_calls_session_with_none(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    (config_dir / "gantry").mkdir(parents=True)
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)
    created: list[FakeSession] = []

    class FakeSessionFactory(FakeSession):
        def __init__(self):
            super().__init__(connected=False)
            created.append(self)

    monkeypatch.setattr(gantry_router, "GantrySession", FakeSessionFactory)

    response = api_request(create_app(), "POST", "/api/v1/gantry/connect", json={})

    assert response.status_code == 200
    assert created[0].calls[0][1] == (None, None)


def test_connect_missing_named_config_returns_404(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    (config_dir / "gantry").mkdir(parents=True)
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/gantry/connect",
        json={"filename": "missing.yaml"},
    )

    assert response.status_code == 404
    assert "Config not found" in response.text


def test_connect_surfaces_session_validation_errors(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    gantry_dir = config_dir / "gantry"
    gantry_dir.mkdir(parents=True)
    write_yaml(gantry_dir / "selected.yaml", VALID_GANTRY)
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)

    class BadSession(FakeSession):
        def __init__(self):
            super().__init__(connected=False)

        def connect(self, path=None, *, filename=None):
            raise ValueError("bad current schema")

    monkeypatch.setattr(gantry_router, "GantrySession", BadSession)

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/gantry/connect",
        json={"filename": "selected.yaml"},
    )

    assert response.status_code == 400
    assert "Invalid gantry config" in response.text


def test_connect_surfaces_unexpected_session_errors(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    gantry_dir = config_dir / "gantry"
    gantry_dir.mkdir(parents=True)
    write_yaml(gantry_dir / "selected.yaml", VALID_GANTRY)
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)

    class BadSession(FakeSession):
        def __init__(self):
            super().__init__(connected=False)

        def connect(self, path=None, *, filename=None):
            raise RuntimeError("serial failed")

    monkeypatch.setattr(gantry_router, "GantrySession", BadSession)

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/gantry/connect",
        json={"filename": "selected.yaml"},
    )

    assert response.status_code == 500
    assert "Failed to connect" in response.text


def test_get_gantry_requires_current_cubos_schema(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    gantry_dir = config_dir / "gantry"
    gantry_dir.mkdir(parents=True)
    write_yaml(
        gantry_dir / "legacy.yaml",
        {
            "serial_port": "",
            "cnc": {},
            "working_volume": {
                "x_min": 0.0,
                "x_max": 300.0,
                "y_min": 0.0,
                "y_max": 200.0,
                "z_min": 0.0,
                "z_max": 80.0,
            },
        },
    )
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)

    response = api_request(create_app(), "GET", "/api/v1/gantry/legacy.yaml")

    assert response.status_code == 400
    assert "Field required" in response.text


def test_get_gantry_returns_current_schema(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    gantry_dir = config_dir / "gantry"
    gantry_dir.mkdir(parents=True)
    write_yaml(gantry_dir / "valid.yaml", VALID_GANTRY)
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)

    response = api_request(create_app(), "GET", "/api/v1/gantry/valid.yaml")

    assert response.status_code == 200
    assert response.json()["config"]["gantry_type"] == "cub_xl"


def test_get_gantry_returns_404_for_missing_config(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    (config_dir / "gantry").mkdir(parents=True)
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)

    response = api_request(create_app(), "GET", "/api/v1/gantry/missing.yaml")

    assert response.status_code == 404


def test_put_gantry_persists_current_schema_and_refreshes_session(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    gantry_dir = config_dir / "gantry"
    gantry_dir.mkdir(parents=True)
    write_yaml(gantry_dir / "test.yaml", VALID_GANTRY)
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)
    session = FakeSession()
    monkeypatch.setattr(gantry_router, "_session", session)

    updated = {
        **VALID_GANTRY,
        "cnc": {
            **VALID_GANTRY["cnc"],
            "calibration_block_height_mm": 36.25,
        },
    }
    response = api_request(
        create_app(),
        "PUT",
        "/api/v1/gantry/test.yaml",
        json=updated,
    )

    assert response.status_code == 200
    saved = read_yaml(gantry_dir / "test.yaml")
    assert saved["cnc"]["calibration_block_height_mm"] == 36.25
    assert session.calls[-1][0] == "refresh_connected_config"


def test_raw_put_valid_gantry_refreshes_connected_session(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    gantry_dir = config_dir / "gantry"
    gantry_dir.mkdir(parents=True)
    write_yaml(gantry_dir / "test.yaml", VALID_GANTRY)
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)
    session = FakeSession()
    monkeypatch.setattr(gantry_router, "_session", session)

    updated = {
        **VALID_GANTRY,
        "cnc": {
            **VALID_GANTRY["cnc"],
            "calibration_block_height_mm": 36.25,
        },
    }
    raw_path = tmp_path / "updated_gantry.yaml"
    write_yaml(raw_path, updated)

    response = api_request(
        create_app(),
        "PUT",
        "/api/v1/raw/test.yaml",
        json={"content": raw_path.read_text(encoding="utf-8")},
    )

    assert response.status_code == 200
    assert session.calls[-1][0] == "refresh_connected_config"
    _, (filename, config) = session.calls[-1]
    assert filename == "test.yaml"
    assert config["cnc"]["calibration_block_height_mm"] == 36.25


def test_put_gantry_rejects_invalid_current_schema(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    (config_dir / "gantry").mkdir(parents=True)
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)

    response = api_request(
        create_app(),
        "PUT",
        "/api/v1/gantry/test.yaml",
        json={"serial_port": ""},
    )

    assert response.status_code == 400


def test_motion_endpoints_delegate_to_session(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(gantry_router, "_session", session)

    responses = [
        api_request(create_app(), "POST", "/api/v1/gantry/jog", json={"x": 1, "y": 0, "z": 0}),
        api_request(
            create_app(),
            "POST",
            "/api/v1/gantry/move-to",
            json={"x": 1, "y": 2, "z": 3},
        ),
        api_request(
            create_app(),
            "POST",
            "/api/v1/gantry/move-to-blocking",
            json={"x": 1, "y": 2, "z": 3},
        ),
        api_request(
            create_app(),
            "POST",
            "/api/v1/gantry/jog-blocking",
            json={"x": 0, "y": 1, "z": 0, "timeout_s": 1},
        ),
        api_request(
            create_app(),
            "POST",
            "/api/v1/gantry/work-coordinates",
            json={"x": 1},
        ),
        api_request(
            create_app(),
            "POST",
            "/api/v1/gantry/soft-limits",
            json={
                "max_travel_x": 300,
                "max_travel_y": 200,
                "max_travel_z": 90,
                "hard_limits": False,
            },
        ),
    ]

    assert [response.status_code for response in responses] == [200] * len(responses)
    called = [name for name, _ in session.calls]
    assert "jog" in called
    assert "move_to" in called
    assert "move_to_blocking" in called
    assert "jog_blocking" in called
    assert "set_work_coordinates" in called
    assert "configure_soft_limits" in called


def test_move_to_maps_session_bounds_errors(monkeypatch):
    session = FakeSession()

    def reject(**_kwargs):
        raise MovementOutOfBoundsError(
            "Manual move target outside configured gantry working volume: X=301 outside [0, 300]"
        )

    session.move_to = reject
    monkeypatch.setattr(gantry_router, "_session", session)

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/gantry/move-to",
        json={"x": 301, "y": 100, "z": 40},
    )

    assert response.status_code == 400
    assert "outside configured gantry working volume" in response.text


def test_session_error_mapping_branches(monkeypatch):
    cases = [
        (GantryNotConnectedError("missing"), 400, "Gantry not connected"),
        (CalibrationBlockedError("cal blocked"), 400, "cal blocked"),
        (GantrySessionHealthCheckError("not healthy"), 400, "not healthy"),
        (GantryAlarmError("alarm"), 409, "alarm"),
        (ValueError("bad value"), 400, "bad value"),
        (GantrySessionError("session failed"), 500, "session failed"),
        (RuntimeError("plain failed"), 500, "Homing failed"),
    ]
    for exc, status, text in cases:
        session = FakeSession()

        def fail():
            raise exc

        session.home = fail
        monkeypatch.setattr(gantry_router, "_session", session)
        response = api_request(create_app(), "POST", "/api/v1/gantry/home")
        assert response.status_code == status
        assert text in response.text


def test_movement_reconnect_mapping_uses_structured_error_attribute(monkeypatch):
    session = FakeSession()

    def reject(**_kwargs):
        raise MovementOutOfBoundsError(
            "Manual absolute moves require a loaded gantry working_volume.",
            requires_reconnect=True,
        )

    session.move_to_blocking = reject
    monkeypatch.setattr(gantry_router, "_session", session)

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/gantry/move-to-blocking",
        json={"x": 10, "y": 10, "z": 10},
    )

    assert response.status_code == 409


@pytest.mark.parametrize(
    ("method", "path", "session_method", "json_body"),
    [
        ("POST", "/api/v1/gantry/feed-hold", "feed_hold", None),
        ("POST", "/api/v1/gantry/resume", "resume", None),
        ("POST", "/api/v1/gantry/jog-cancel", "jog_cancel_interrupt", None),
        ("POST", "/api/v1/gantry/unlock", "unlock", None),
        ("POST", "/api/v1/gantry/reset-unlock", "reset_and_unlock", None),
        (
            "POST",
            "/api/v1/gantry/calibration/prepare-origin",
            "prepare_calibration_origin",
            None,
        ),
        (
            "POST",
            "/api/v1/gantry/calibration/home-and-center",
            "calibration_home_and_center",
            None,
        ),
        (
            "POST",
            "/api/v1/gantry/calibration/restore-soft-limits",
            "restore_calibration_soft_limits",
            None,
        ),
        ("GET", "/api/v1/gantry/grbl-settings", "read_grbl_settings", None),
        (
            "POST",
            "/api/v1/gantry/grbl-settings",
            "set_grbl_setting",
            {"setting": "$20", "value": "0"},
        ),
        ("POST", "/api/v1/gantry/jog", "jog", {"x": 1, "y": 0, "z": 0}),
        (
            "POST",
            "/api/v1/gantry/jog-blocking",
            "jog_blocking",
            {"x": 0, "y": 1, "z": 0, "timeout_s": 1},
        ),
        (
            "POST",
            "/api/v1/gantry/work-coordinates",
            "set_work_coordinates",
            {"x": 1},
        ),
        (
            "POST",
            "/api/v1/gantry/soft-limits",
            "configure_soft_limits",
            {
                "max_travel_x": 300,
                "max_travel_y": 200,
                "max_travel_z": 90,
            },
        ),
        ("POST", "/api/v1/gantry/disconnect", "disconnect", None),
    ],
)
@pytest.mark.parametrize(
    ("exc", "status_code", "detail"),
    [
        (GantryAlarmError("alarm"), 409, "alarm"),
        (GantrySessionError("boom"), 500, "boom"),
    ],
)
def test_hardware_endpoint_error_paths(
    monkeypatch,
    method,
    path,
    session_method,
    json_body,
    exc,
    status_code,
    detail,
):
    def fail(self, *_args, **_kwargs):
        raise exc

    monkeypatch.setattr(FakeSession, session_method, fail)
    monkeypatch.setattr(gantry_router, "_session", FakeSession())

    response = api_request(create_app(), method, path, json=json_body)

    assert response.status_code == status_code
    assert detail in response.text


def test_missing_working_volume_maps_to_conflict(monkeypatch):
    session = FakeSession()

    def reject(**_kwargs):
        raise MovementOutOfBoundsError(
            "Manual absolute moves require a loaded gantry working_volume.",
            requires_reconnect=True,
        )

    session.move_to_blocking = reject
    monkeypatch.setattr(gantry_router, "_session", session)

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/gantry/move-to-blocking",
        json={"x": 10, "y": 10, "z": 10},
    )

    assert response.status_code == 409


def test_calibration_and_recovery_endpoints_delegate_to_session(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(gantry_router, "_session", session)

    responses = [
        api_request(create_app(), "POST", "/api/v1/gantry/calibration/prepare-origin"),
        api_request(create_app(), "POST", "/api/v1/gantry/calibration/home-and-center"),
        api_request(create_app(), "POST", "/api/v1/gantry/calibration/restore-soft-limits"),
        api_request(
            create_app(),
            "POST",
            "/api/v1/gantry/calibration/finalize-origin",
            json={
                "home_z": 80,
                "block_touch_z": 10,
                "block_height": 10,
                "factory_z_travel": 90,
            },
        ),
        api_request(
            create_app(),
            "POST",
            "/api/v1/gantry/calibration/recover-limit",
            json={"x": 0, "y": 0, "z": -1, "pull_off_mm": 3, "feed_rate": 900},
        ),
    ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200, 200]
    called = [name for name, _ in session.calls]
    assert "prepare_calibration_origin" in called
    assert "calibration_home_and_center" in called
    assert "restore_calibration_soft_limits" in called
    assert "finalize_calibration_origin" in called
    assert "recover_calibration_limit" in called


def test_limit_recovery_connection_loss_maps_to_503(monkeypatch):
    from cubos.gantry.gantry_driver.exceptions import MillConnectionError

    session = FakeSession()

    def fail(**_kwargs):
        raise MillConnectionError(
            "Controller connection lost and serial reconnect failed after 4 attempts"
        )

    session.recover_calibration_limit = fail
    monkeypatch.setattr(gantry_router, "_session", session)

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/gantry/calibration/recover-limit",
        json={"x": 1, "y": 0, "z": 0},
    )

    assert response.status_code == 503
    assert "Controller connection lost" in response.text
    assert "Power-cycle" in response.text


def test_limit_recovery_alarm_error_maps_to_409(monkeypatch):
    session = FakeSession()

    def fail(**_kwargs):
        raise RuntimeError("limit alarm still active")

    session.recover_calibration_limit = fail
    monkeypatch.setattr(gantry_router, "_session", session)

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/gantry/calibration/recover-limit",
        json={"x": 0, "y": 0, "z": -1},
    )

    assert response.status_code == 409
    assert "Limit recovery did not clear" in response.text


def test_limit_recovery_generic_runtime_error_maps_to_500(monkeypatch):
    session = FakeSession()

    def fail(**_kwargs):
        raise RuntimeError("serial died")

    session.recover_calibration_limit = fail
    monkeypatch.setattr(gantry_router, "_session", session)

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/gantry/calibration/recover-limit",
        json={"x": 0, "y": 0, "z": -1},
    )

    assert response.status_code == 500
    assert "Limit recovery failed" in response.text


def test_limit_recovery_missing_delta_maps_to_400(monkeypatch):
    session = FakeSession()

    def fail(**_kwargs):
        raise ValueError("Limit recovery requires the failed jog delta.")

    session.recover_calibration_limit = fail
    monkeypatch.setattr(gantry_router, "_session", session)

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/gantry/calibration/recover-limit",
        json={},
    )

    assert response.status_code == 400
    assert "Limit recovery requires the failed jog delta." in response.text
    assert "E-stop" not in response.text


def test_finalize_origin_accepts_dataclass_result(monkeypatch):
    from cubos.gantry.session import FinalizeOriginResult

    session = FakeSession()

    def finalize(**kwargs):
        return FinalizeOriginResult(
            measured_volume={"x": 1.0, "y": 2.0, "z": 3.0},
            z_calibration={"z_max": 3.0},
            max_travel={"x": 1.0, "y": 2.0, "z": 4.0},
            position={"x": 0.0, "y": 0.0, "z": 3.0},
            homing_pull_off_mm=1.0,
        )

    session.finalize_calibration_origin = finalize
    monkeypatch.setattr(gantry_router, "_session", session)

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/gantry/calibration/finalize-origin",
        json={
            "home_z": 3,
            "block_touch_z": 1,
            "block_height": 1,
            "factory_z_travel": 4,
        },
    )

    assert response.status_code == 200
    assert response.json()["homing_pull_off_mm"] == 1.0


def test_finalize_origin_calibration_blocked_maps_to_400(monkeypatch):
    session = FakeSession()

    def finalize(**_kwargs):
        raise CalibrationBlockedError("calibration warning is active")

    session.finalize_calibration_origin = finalize
    monkeypatch.setattr(gantry_router, "_session", session)

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/gantry/calibration/finalize-origin",
        json={
            "home_z": 80,
            "block_touch_z": 10,
            "block_height": 10,
            "factory_z_travel": 90,
        },
    )

    assert response.status_code == 400
    assert "calibration warning is active" in response.text


def test_advanced_gantry_actions_delegate_to_session(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(gantry_router, "_session", session)

    endpoints = [
        ("POST", "/api/v1/gantry/unlock", None),
        ("POST", "/api/v1/gantry/reset-unlock", None),
        ("POST", "/api/v1/gantry/feed-hold", None),
        ("POST", "/api/v1/gantry/resume", None),
        ("POST", "/api/v1/gantry/jog-cancel", None),
        ("GET", "/api/v1/gantry/grbl-settings", None),
        ("POST", "/api/v1/gantry/grbl-settings", {"setting": "$20", "value": "0"}),
    ]

    for method, path, json_body in endpoints:
        response = api_request(create_app(), method, path, json=json_body)
        assert response.status_code == 200

    called = [name for name, _ in session.calls]
    assert "unlock" in called
    assert "reset_and_unlock" in called
    assert "feed_hold" in called
    assert "resume" in called
    assert "jog_cancel_interrupt" in called
    assert "read_grbl_settings" in called
    assert ("set_grbl_setting", ("$20", "0")) in session.calls


def test_disconnect_without_session_returns_disconnected():
    response = api_request(create_app(), "POST", "/api/v1/gantry/disconnect")

    assert response.status_code == 200
    assert response.json()["connected"] is False


def test_disconnect_delegates_to_session(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(gantry_router, "_session", session)

    response = api_request(create_app(), "POST", "/api/v1/gantry/disconnect")

    assert response.status_code == 200
    assert ("disconnect", None) in session.calls


def test_protocol_cancel_helper_uses_interrupt_path(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(gantry_router, "_session", session)

    gantry_router.request_feed_hold_interrupt()

    assert ("feed_hold_interrupt", None) in session.calls


def test_helper_paths_delegate_to_connected_session(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(gantry_router, "_session", session)

    gantry_router.request_jog_cancel_interrupt()
    result = gantry_router.run_protocol_on_session(example=True)

    assert ("jog_cancel_interrupt", None) in session.calls
    assert result.campaign_id == 123
    assert session.calls[-1] == ("run_protocol", {"example": True})


def test_interrupt_timeout_translation_keeps_cancel_shape():
    response = gantry_router.translate_interrupt_timeout(
        InterruptFeedHoldTimeoutError("sent but not acknowledged")
    )

    assert response == {
        "status": "cancel_requested",
        "warning": "sent but not acknowledged",
    }


def test_connect_returns_409_when_already_connected(monkeypatch):
    session = FakeSession()
    assert session.connected is True
    monkeypatch.setattr(gantry_router, "_session", session)

    response = api_request(create_app(), "POST", "/api/v1/gantry/connect")

    assert response.status_code == 409
    assert "already connected" in response.text.lower()
    assert not any(name == "connect" for name, _ in session.calls)


def test_get_or_create_session_creation_is_race_safe(monkeypatch):
    import threading

    created: list[FakeSession] = []

    class CountingSession(FakeSession):
        def __init__(self):
            super().__init__()
            created.append(self)

    monkeypatch.setattr(gantry_router, "GantrySession", CountingSession)

    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        gantry_router._get_or_create_session()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(created) == 1


def test_reset_session_clears_module_global(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(gantry_router, "_session", session)

    gantry_router.reset_session()

    assert gantry_router.current_session() is None


def test_motion_and_calibration_endpoints_reject_while_run_active(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(gantry_router, "_session", session)
    gantry_router.begin_run(protocol_file="running.yaml")
    try:
        endpoints = [
            ("POST", "/api/v1/gantry/home", None),
            ("POST", "/api/v1/gantry/jog", {"x": 1, "y": 0, "z": 0}),
            ("POST", "/api/v1/gantry/move-to", {"x": 1, "y": 2, "z": 3}),
            ("POST", "/api/v1/gantry/move-to-blocking", {"x": 1, "y": 2, "z": 3}),
            (
                "POST",
                "/api/v1/gantry/jog-blocking",
                {"x": 0, "y": 1, "z": 0, "timeout_s": 1},
            ),
            ("POST", "/api/v1/gantry/calibration/prepare-origin", None),
            ("POST", "/api/v1/gantry/calibration/home-and-center", None),
            ("POST", "/api/v1/gantry/calibration/restore-soft-limits", None),
            (
                "POST",
                "/api/v1/gantry/calibration/finalize-origin",
                {
                    "home_z": 80,
                    "block_touch_z": 10,
                    "block_height": 10,
                    "factory_z_travel": 90,
                },
            ),
        ]
        for method, path, json_body in endpoints:
            response = api_request(create_app(), method, path, json=json_body)
            assert response.status_code == 409, f"{path} should be gated"
            assert "busy running a protocol" in response.text
    finally:
        gantry_router.end_run()

    assert session.calls == []


def test_stop_recovery_and_observability_endpoints_stay_available_during_run(
    monkeypatch,
):
    session = FakeSession()
    monkeypatch.setattr(gantry_router, "_session", session)
    gantry_router.begin_run(protocol_file="running.yaml")
    try:
        endpoints = [
            ("POST", "/api/v1/gantry/feed-hold", None),
            ("POST", "/api/v1/gantry/jog-cancel", None),
            ("GET", "/api/v1/gantry/position", None),
            ("POST", "/api/v1/gantry/unlock", None),
            ("POST", "/api/v1/gantry/reset-unlock", None),
            ("POST", "/api/v1/gantry/disconnect", None),
        ]
        for method, path, json_body in endpoints:
            response = api_request(create_app(), method, path, json=json_body)
            assert response.status_code == 200, f"{path} should not be gated"
    finally:
        gantry_router.end_run()


def test_gantry_exposes_instrument_registry_endpoints():
    response = api_request(create_app(), "GET", "/api/v1/gantry/instrument-types")

    assert response.status_code == 200
    types = {entry["type"]: entry for entry in response.json()}
    assert "asmi" in types
    assert "vernier" in types["asmi"]["vendors"]


def test_gantry_exposes_protocol_measurement_methods_from_cubos_registry():
    response = api_request(create_app(), "GET", "/api/v1/gantry/instrument-methods")

    assert response.status_code == 200
    methods = response.json()
    assert methods["asmi"] == ["indentation"]
    assert "measure" in methods["filmetrics"]
    assert "measure" in methods["uvvis_ccs"]
    assert methods["pipette"] == []


def test_instrument_schemas_use_public_config_fields(monkeypatch):
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(gantry_router, "get_supported_types", lambda: ["demo"])
    monkeypatch.setattr(gantry_router, "get_supported_vendors", lambda _type: ["acme"])

    def fake_config_fields(type_key: str, vendor: str):
        calls.append((type_key, vendor))
        return [
            SimpleNamespace(
                name="gain",
                type="float",
                required=False,
                default=1.0,
                choices=(0.5, 1.0),
            )
        ]

    monkeypatch.setattr(gantry_router, "config_fields", fake_config_fields)

    response = api_request(create_app(), "GET", "/api/v1/gantry/instrument-schemas")

    assert response.status_code == 200
    assert calls == [("demo", "acme")]
    assert response.json() == {
        "demo": {
            "acme": [
                {
                    "name": "gain",
                    "type": "float",
                    "required": False,
                    "default": 1.0,
                    "choices": [0.5, 1.0],
                }
            ]
        }
    }


def test_instrument_methods_use_public_measurement_method_api(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(gantry_router, "get_supported_types", lambda: ["demo"])

    def fake_list_measurement_methods(type_key: str) -> list[str]:
        calls.append(type_key)
        return ["measure"]

    monkeypatch.setattr(
        gantry_router,
        "list_measurement_methods",
        fake_list_measurement_methods,
    )

    response = api_request(create_app(), "GET", "/api/v1/gantry/instrument-methods")

    assert response.status_code == 200
    assert response.json() == {"demo": ["measure"]}
    assert calls == ["demo"]


def test_delete_gantry_removes_file_and_404s_when_missing(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    gantry_dir = config_dir / "gantry"
    gantry_dir.mkdir(parents=True)
    (gantry_dir / "old.yaml").write_text("working_volume: {}\n", encoding="utf-8")
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)
    monkeypatch.setattr(gantry_router, "_session", None)
    app = create_app()

    response = api_request(app, "DELETE", "/api/v1/gantry/old.yaml")
    assert response.status_code == 200
    assert response.json() == {"status": "deleted", "filename": "old.yaml"}
    assert not (gantry_dir / "old.yaml").exists()
    assert api_request(app, "DELETE", "/api/v1/gantry/old.yaml").status_code == 404


def test_delete_gantry_refused_for_connected_config(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    gantry_dir = config_dir / "gantry"
    gantry_dir.mkdir(parents=True)
    (gantry_dir / "live.yaml").write_text("working_volume: {}\n", encoding="utf-8")
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)
    monkeypatch.setattr(
        gantry_router,
        "_session",
        SimpleNamespace(connected_gantry_filename="live.yaml"),
    )

    response = api_request(create_app(), "DELETE", "/api/v1/gantry/live.yaml")
    assert response.status_code == 409
    assert "disconnect" in response.json()["detail"]
    assert (gantry_dir / "live.yaml").exists()
