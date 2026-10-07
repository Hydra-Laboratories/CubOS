"""Disconnected API operations must not prevent a later reconnect."""

import pytest

from cubos.gantry.session import GantrySession
from cubos_api.app import create_app
from cubos_api.config import get_settings
from cubos_api.routers import gantry as gantry_router
from cubos_api.services.yaml_io import write_yaml
from tests.api_client import api_request


class FakeGantry:
    def __init__(self, config=None):
        self.config = config or {}
        self.home_calls = 0

    def connect(self, port=None):
        pass

    def disconnect(self):
        pass

    def get_position_info(self):
        coords = {"x": 10.0, "y": 20.0, "z": 30.0}
        return {"coords": coords, "work_pos": coords, "status": "Idle"}

    def home(self):
        self.home_calls += 1

    def read_grbl_settings(self):
        return {"$10": "0"}


@pytest.mark.parametrize("method,route", [("POST", "home"), ("GET", "grbl-settings")])
def test_disconnection_before_locked_api_operation_does_not_block_reconnect(monkeypatch, tmp_path, method, route):
    config_dir = tmp_path / "configs"
    write_yaml(config_dir / "gantry" / "fake.yaml", {
        "serial_port": "/dev/fake", "gantry_type": "cub_xl",
        "cnc": {"factory_z_travel_mm": 110},
        "working_volume": {"x_min": 0, "x_max": 300, "y_min": 0, "y_max": 200, "z_min": 0, "z_max": 80},
        "instruments": {},
    })
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    monkeypatch.setattr(gantry_router, "_session", session)
    app = create_app()

    disconnected = api_request(app, method, f"/api/v1/gantry/{route}")
    assert disconnected.status_code == 400
    assert disconnected.json()["detail"] == "Gantry not connected"
    assert not session.operation_lock.locked()
    assert api_request(app, "POST", "/api/v1/gantry/connect", json={"filename": "fake.yaml"}).status_code == 200

    require_session = gantry_router._require_session

    def disconnect_after_preflight():
        current = require_session()
        current.disconnect()
        return current

    # A connection can disappear between the route's check and the locked operation.
    with monkeypatch.context() as patch:
        patch.setattr(gantry_router, "_require_session", disconnect_after_preflight)
        failed = api_request(app, method, f"/api/v1/gantry/{route}")
    assert failed.status_code == 400
    assert failed.json()["detail"] == "Gantry not connected"
    acquired = session.operation_lock.acquire(blocking=False)
    assert acquired, "Failed context entry must release the lock before retrying connect."
    session.operation_lock.release()

    reconnected = api_request(app, "POST", "/api/v1/gantry/connect", json={"filename": "fake.yaml"})
    assert reconnected.status_code == 200
    assert reconnected.json()["connected"] is True
    home = api_request(app, "POST", "/api/v1/gantry/home")
    assert home.status_code == 200
    assert home.json()["connected"] is True
    assert session._gantry.home_calls == 1
