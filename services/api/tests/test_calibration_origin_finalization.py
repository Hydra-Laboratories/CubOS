from copy import deepcopy
from unittest.mock import Mock

import pytest

from cubos.gantry.gantry import Gantry
from cubos.gantry.gantry_driver.exceptions import MillConnectionError
from cubos.gantry.session import GantrySession
from cubos_api.app import create_app
from cubos_api.config import get_settings
from cubos_api.routers import gantry as gantry_router
from cubos_api.services.yaml_io import read_yaml, write_yaml
from tests.api_client import api_request


PARAMS = {"home_z": 80, "block_touch_z": 35, "block_height": 35, "factory_z_travel": 80}


@pytest.fixture
def setup_runtime(monkeypatch, tmp_path):
    directory = tmp_path / "configs"
    monkeypatch.setattr(get_settings(), "config_dir", directory)
    body = {
        "serial_port": "/dev/offline", "gantry_type": "cub_xl", "origin_policy": "home_origin",
        "cnc": {"factory_z_travel_mm": 80, "safe_z": -5},
        "working_volume": {"x_min": -298, "x_max": 0, "y_min": -198, "y_max": 0, "z_min": -78, "z_max": 0},
        "grbl_settings": {"homing_pull_off": 1},
        "instruments": {"pipette": {"type": "pipette", "vendor": "sartorius", "offset_x": 0, "offset_y": 0, "depth": -42}, "camera": {"type": "camera", "vendor": "usb", "offset_x": 2.5, "offset_y": -28.75, "depth": -75}},
    }
    path = directory / "gantry" / "gantry.yaml"
    write_yaml(path, body)
    session = GantrySession(gantry_factory=lambda config: Gantry(config, offline=True))
    session.connect(path, filename="gantry.yaml")
    session._gantry.set_work_coordinates(x=258, y=145, z=80)
    monkeypatch.setattr(gantry_router, "_session", session)
    return create_app(), session, path, body


@pytest.mark.parametrize("policy", [None, "home_origin", "deck_origin"])
@pytest.mark.parametrize("flow", ["single", "multi"])
def test_endpoint_returns_verified_frame_and_blocks_motion_until_matching_save(setup_runtime, policy, flow):
    app, session, path, original = setup_runtime
    if flow == "single":
        del session._connected_gantry_config["instruments"]["camera"]
    request = dict(PARAMS)
    if policy is not None:
        request["origin_policy"] = policy
    response = api_request(app, "POST", "/api/v1/gantry/calibration/finalize-origin", json=request)
    assert response.status_code == 200
    result = response.json()
    selected = policy or "home_origin"
    assert result["origin_policy"] == selected
    assert result["measured_volume"] == {"x": 258, "y": 145, "z": 80}
    assert result["safe_z"] == (-5 if selected == "home_origin" else 75)
    assert result["position"] == ({"x": 0, "y": 0, "z": 0} if selected == "home_origin" else {"x": 258, "y": 145, "z": 80})
    assert read_yaml(path) == original
    assert api_request(app, "POST", "/api/v1/gantry/jog", json={"x": -1}).status_code == 400
    saved = session.connected_gantry_config
    mounting = deepcopy(saved["instruments"])
    assert api_request(app, "PUT", "/api/v1/gantry/gantry.yaml", json=saved).status_code == 200
    assert not session.calibration_active
    assert session.connected_gantry_config["instruments"] == mounting
    assert api_request(app, "POST", "/api/v1/gantry/jog", json={"x": -1}).status_code == 200


def test_invalid_requested_policy_is_rejected_before_finalization(setup_runtime):
    app, session, _, _ = setup_runtime
    controller = Mock()
    session._gantry.finalize_deck_origin_calibration = controller
    response = api_request(app, "POST", "/api/v1/gantry/calibration/finalize-origin", json={**PARAMS, "origin_policy": "wrong"})
    assert response.status_code == 422
    controller.assert_not_called()


def test_failed_origin_readback_never_saves_and_retains_motion_block(setup_runtime):
    app, session, path, original = setup_runtime
    session._gantry.soft_limits_enabled = Mock(return_value=True)
    failure = MillConnectionError("Calibrated WPos did not verify")
    session._gantry.finalize_deck_origin_calibration = Mock(side_effect=failure)
    response = api_request(app, "POST", "/api/v1/gantry/calibration/finalize-origin", json=PARAMS)
    assert response.status_code == 500
    assert "did not verify" in response.json()["detail"]
    assert read_yaml(path) == original
    assert api_request(app, "POST", "/api/v1/gantry/calibration/restore-soft-limits").status_code == 200
    assert api_request(app, "POST", "/api/v1/gantry/jog", json={"x": -1}).status_code == 400
    assert session.calibration_active


def test_pending_frame_requires_a_matching_saved_profile_after_disconnect(setup_runtime):
    app, session, _, _ = setup_runtime
    assert api_request(app, "POST", "/api/v1/gantry/calibration/finalize-origin", json={**PARAMS, "origin_policy": "deck_origin"}).status_code == 200
    finalized = session.connected_gantry_config
    assert api_request(app, "POST", "/api/v1/gantry/disconnect").status_code == 200
    blocked = api_request(app, "POST", "/api/v1/gantry/connect", json={"filename": "gantry.yaml"})
    assert blocked.status_code == 400
    assert "Save the finalized calibration" in blocked.json()["detail"]
    assert api_request(app, "PUT", "/api/v1/gantry/saved.yaml", json=finalized).status_code == 200
    assert api_request(app, "POST", "/api/v1/gantry/connect", json={"filename": "saved.yaml"}).status_code == 200
    assert not session.calibration_active
