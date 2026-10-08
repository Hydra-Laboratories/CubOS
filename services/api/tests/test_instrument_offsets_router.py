from unittest.mock import Mock

import pytest

from tests.api_client import api_request
from cubos_api.app import create_app
from cubos_api.routers import gantry as gantry_router


@pytest.fixture
def request_body():
    return {
        "config": {
            "serial_port": "/dev/offline", "gantry_type": "cub_xl", "origin_policy": "home_origin",
            "cnc": {"factory_z_travel_mm": 110},
            "working_volume": {"x_min": -300, "x_max": 0, "y_min": -200, "y_max": 0, "z_min": -110, "z_max": 0},
            "instruments": {
                "pipette": {"type": "pipette", "vendor": "sartorius", "offset_x": 12, "offset_y": -3, "depth": -8},
                "camera": {"type": "camera", "vendor": "usb", "offset_x": 0, "offset_y": 0},
            },
        },
        "reference_instrument": "pipette",
        "captures": {
            "pipette": {"x": -100, "y": -50, "z": -20, "tip_length_mm": 40},
            "camera": {"x": -90, "y": -55, "z": -10, "stand_off_mm": 30},
        },
    }


def test_offline_preview_no_session_or_file_write(monkeypatch, request_body):
    session = Mock(side_effect=AssertionError("No controller access allowed"))
    write = Mock(side_effect=AssertionError("Preview must not save"))
    monkeypatch.setattr(gantry_router, "_require_session", session)
    monkeypatch.setattr(gantry_router, "write_yaml", write)
    response = api_request(create_app(), "POST", "/api/v1/gantry/calibration/instrument-offsets", json=request_body)
    assert response.status_code == 200
    result = response.json()
    assert result["instruments"]["pipette"] == request_body["config"]["instruments"]["pipette"]
    assert result["instruments"]["camera"]["depth"] == 12
    assert result["working_volume"] == request_body["config"]["working_volume"]
    session.assert_not_called()
    write.assert_not_called()


def test_invalid_capture_returns_actionable_validation_error(request_body):
    request_body["captures"]["camera"]["stand_off_mm"] = -1
    response = api_request(create_app(), "POST", "/api/v1/gantry/calibration/instrument-offsets", json=request_body)
    assert response.status_code == 422
    assert "stand-off must be nonnegative" in response.json()["detail"]


def test_invalid_config_rejected_before_calculation(request_body):
    request_body["config"]["cnc"]["factory_z_travel_mm"] = 10
    response = api_request(create_app(), "POST", "/api/v1/gantry/calibration/instrument-offsets", json=request_body)
    assert response.status_code == 422
    assert "factory_z_travel_mm" in response.json()["detail"]


@pytest.mark.parametrize("capture", [None, 1, "invalid", []])
def test_malformed_capture_is_rejected(request_body, capture):
    request_body["captures"]["pipette"] = capture
    response = api_request(create_app(), "POST", "/api/v1/gantry/calibration/instrument-offsets", json=request_body)
    assert response.status_code == 422
