from copy import deepcopy

import pytest

from cubos.gantry.offset_calibration import calibrate_instrument_offsets


@pytest.fixture
def config():
    return {
        "serial_port": "/dev/offline", "gantry_type": "cub_xl", "origin_policy": "home_origin",
        "cnc": {"factory_z_travel_mm": 110, "safe_z": -5},
        "working_volume": {"x_min": -300, "x_max": 0, "y_min": -200, "y_max": 0, "z_min": -110, "z_max": 0},
        "grbl_settings": {"max_travel_z": 111, "homing_pull_off": 1, "soft_limits": True},
        "instruments": {
            "pipette": {"type": "pipette", "vendor": "sartorius", "offset_x": 12, "offset_y": -3, "depth": -8, "model": "picus"},
            "probe": {"type": "asmi", "vendor": "vernier", "offset_x": 0, "offset_y": 0},
            "camera": {"type": "camera", "vendor": "usb", "offset_x": 0, "offset_y": 0},
            "lights": {"type": "lighting", "vendor": "pawduino", "offset_x": 0, "offset_y": 0},
        },
    }


@pytest.fixture
def captures():
    return {
        "pipette": {"x": -100, "y": -50, "z": -20, "tip_length_mm": 40},
        "probe": {"x": -120, "y": -45, "z": -70},
        "camera": {"x": -90, "y": -55, "z": -10, "stand_off_mm": 30},
    }


def test_tip_reference_preserves_frame_and_controller_and_lighting(config, captures):
    original = deepcopy(config)
    result = calibrate_instrument_offsets(config, "pipette", captures)
    assert config == original
    for field in ("serial_port", "gantry_type", "origin_policy", "cnc", "working_volume", "grbl_settings"):
        assert result[field] == original[field]
    assert result["instruments"]["pipette"] == original["instruments"]["pipette"]
    assert result["instruments"]["probe"]["offset_x"] == 32
    assert result["instruments"]["probe"]["offset_y"] == -8
    assert result["instruments"]["probe"]["depth"] == -18
    assert result["instruments"]["camera"]["offset_x"] == 2
    assert result["instruments"]["camera"]["offset_y"] == 2
    assert result["instruments"]["camera"]["depth"] == 12
    for field in ("offset_x", "offset_y", "depth"):
        assert result["instruments"]["lights"][field] == result["instruments"]["camera"][field]


def test_secondary_tip_compensation_and_zero_standoff(config, captures):
    config["instruments"]["probe"]["type"] = "pipette"
    captures["pipette"]["tip_length_mm"] = 0
    captures["probe"]["tip_length_mm"] = 25
    captures["camera"]["stand_off_mm"] = 0
    result = calibrate_instrument_offsets(config, "pipette", captures)
    assert result["instruments"]["probe"]["depth"] == -83
    assert result["instruments"]["camera"]["depth"] == 2


@pytest.mark.parametrize("reference", ["missing", "camera", "lights"])
def test_invalid_reference(config, captures, reference):
    with pytest.raises(ValueError, match="calibrated contact"):
        calibrate_instrument_offsets(config, reference, captures)


@pytest.mark.parametrize("value", [None, True, "bad", float("nan"), float("inf")])
def test_invalid_position(config, captures, value):
    captures["probe"]["x"] = value
    with pytest.raises(ValueError, match="finite number"):
        calibrate_instrument_offsets(config, "pipette", captures)


@pytest.mark.parametrize("field,value", [("tip_length_mm", -1), ("tip_length_mm", float("inf")), ("stand_off_mm", -1), ("stand_off_mm", None)])
def test_invalid_compensation(config, captures, field, value):
    name = "camera" if field == "stand_off_mm" else "pipette"
    captures[name][field] = value
    with pytest.raises(ValueError):
        calibrate_instrument_offsets(config, "pipette", captures)


def test_no_tip_on_other_instrument(config, captures):
    captures["probe"]["tip_length_mm"] = 10
    with pytest.raises(ValueError, match="only applies to pipettes"):
        calibrate_instrument_offsets(config, "pipette", captures)


def test_no_standoff_on_contact(config, captures):
    captures["probe"]["stand_off_mm"] = 10
    with pytest.raises(ValueError, match="only applies to cameras"):
        calibrate_instrument_offsets(config, "pipette", captures)


@pytest.mark.parametrize("missing", [True, False])
def test_exact_capture_set(config, captures, missing):
    if missing:
        del captures["probe"]
    else:
        captures["unknown"] = captures["probe"]
    with pytest.raises(ValueError, match="Record every"):
        calibrate_instrument_offsets(config, "pipette", captures)


def test_lighting_requires_camera(config, captures):
    del config["instruments"]["camera"]
    del captures["camera"]
    with pytest.raises(ValueError, match="requires a camera"):
        calibrate_instrument_offsets(config, "pipette", captures)


def test_missing_reference_mount_values_default_to_zero(config, captures):
    for field in ("offset_x", "offset_y", "depth"):
        del config["instruments"]["pipette"][field]
    result = calibrate_instrument_offsets(config, "pipette", captures)
    assert result["instruments"]["probe"]["offset_x"] == 20
    assert result["instruments"]["probe"]["depth"] == -10


def test_arithmetic_overflow_is_rejected(config, captures):
    config["instruments"]["pipette"]["offset_x"] = 1e308
    captures["pipette"]["x"] = 1e308
    captures["probe"]["x"] = -1e308
    with pytest.raises(ValueError, match="finite number"):
        calibrate_instrument_offsets(config, "pipette", captures)
