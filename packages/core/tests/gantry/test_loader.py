"""Tests for gantry YAML loader."""

from __future__ import annotations

import os
import tempfile

import pytest

from cubos.gantry.errors import GantryLoaderError
from cubos.gantry.gantry_config import GantryConfig, OriginPolicy
from cubos.gantry.loader import load_gantry_from_yaml, load_gantry_from_yaml_safe


VALID_GANTRY_YAML = """\
serial_port: /dev/cu.usbserial-2130
gantry_type: cub_xl
cnc:
  factory_z_travel_mm: 90.0
  safe_z: 75.0
working_volume:
  x_min: 0.0
  x_max: 300.0
  y_min: 0.0
  y_max: 200.0
  z_min: 0.0
  z_max: 80.0
"""

GANTRY_WITH_INSTRUMENTS_YAML = VALID_GANTRY_YAML + """\
grbl_settings:
  status_report: 0
  homing_enable: true
instruments:
  asmi:
    type: asmi
    vendor: vernier
    sensor_channels: [1]
"""

def _write_temp_yaml(content: str) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write(content)
    f.close()
    return f.name


class TestLoadGantryFromYaml:

    def test_load_valid_gantry_returns_gantry_config(self):
        path = _write_temp_yaml(VALID_GANTRY_YAML)
        try:
            result = load_gantry_from_yaml(path)
            assert isinstance(result, GantryConfig)
        finally:
            os.unlink(path)

    def test_loaded_gantry_has_correct_bounds(self):
        path = _write_temp_yaml(VALID_GANTRY_YAML)
        try:
            config = load_gantry_from_yaml(path)
            vol = config.working_volume
            assert vol.x_min == 0.0
            assert vol.x_max == 300.0
            assert vol.y_min == 0.0
            assert vol.y_max == 200.0
            assert vol.z_min == 0.0
            assert vol.z_max == 80.0
            assert config.factory_z_travel_mm == 90.0
            assert config.safe_z == 75.0
        finally:
            os.unlink(path)

    def test_loaded_gantry_default_feed_rate_defaults_to_none(self):
        path = _write_temp_yaml(VALID_GANTRY_YAML)
        try:
            config = load_gantry_from_yaml(path)
            assert config.default_feed_rate_mm_min is None
        finally:
            os.unlink(path)

    def test_loaded_gantry_reads_default_feed_rate(self):
        path = _write_temp_yaml(
            VALID_GANTRY_YAML.replace(
                "safe_z: 75.0",
                "safe_z: 75.0\n  default_feed_rate_mm_min: 4000.0",
            )
        )
        try:
            config = load_gantry_from_yaml(path)
            assert config.default_feed_rate_mm_min == 4000.0
        finally:
            os.unlink(path)

    def test_loaded_gantry_defaults_to_deck_origin_policy(self):
        path = _write_temp_yaml(VALID_GANTRY_YAML)
        try:
            config = load_gantry_from_yaml(path)
            assert config.origin_policy == OriginPolicy.DECK_ORIGIN
        finally:
            os.unlink(path)

    def test_loaded_gantry_threads_home_origin_policy(self):
        path = _write_temp_yaml(VALID_GANTRY_YAML + "origin_policy: home_origin\n")
        try:
            config = load_gantry_from_yaml(path)
            assert config.origin_policy == OriginPolicy.HOME_ORIGIN
        finally:
            os.unlink(path)

    def test_loaded_gantry_has_serial_port(self):
        path = _write_temp_yaml(VALID_GANTRY_YAML)
        try:
            config = load_gantry_from_yaml(path)
            assert config.serial_port == "/dev/cu.usbserial-2130"
            assert config.gantry_type == "cub_xl"
        finally:
            os.unlink(path)

    def test_loaded_gantry_has_instruments_and_grbl_settings(self):
        path = _write_temp_yaml(GANTRY_WITH_INSTRUMENTS_YAML)
        try:
            config = load_gantry_from_yaml(path)
            assert config.expected_grbl_settings == {"$10": 0.0, "$22": 1.0}
            assert config.instruments["asmi"]["type"] == "asmi"
            assert config.instruments["asmi"]["sensor_channels"] == [1]
        finally:
            os.unlink(path)

    def test_duplicate_yaml_key_names_file_and_key(self, tmp_path):
        path = tmp_path / "duplicate_gantry.yaml"
        path.write_text(
            VALID_GANTRY_YAML + "serial_port: /dev/ttyUSB1\n",
            encoding="utf-8",
        )

        with pytest.raises(Exception) as exc_info:
            load_gantry_from_yaml(path)

        message = str(exc_info.value)
        assert str(path) in message
        assert "duplicate YAML key 'serial_port'" in message

    def test_machine_structures_yaml_is_rejected(self):
        yaml_content = VALID_GANTRY_YAML + """\
machine_structures:
  right_x_max_rail:
    type: box
    x_min: 480.0
    x_max: 540.0
    y_min: 0.0
    y_max: 300.0
    z_min: 0.0
    z_max: 100.0
"""
        path = _write_temp_yaml(yaml_content)
        try:
            with pytest.raises(Exception, match="machine_structures"):
                load_gantry_from_yaml(path)
        finally:
            os.unlink(path)

    def test_missing_file_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_gantry_from_yaml("/nonexistent/path.yaml")

    def test_invalid_yaml_raises(self):
        path = _write_temp_yaml("{{invalid yaml: [")
        try:
            with pytest.raises(Exception):
                load_gantry_from_yaml(path)
        finally:
            os.unlink(path)

    def test_missing_working_volume_raises_validation_error(self):
        yaml_content = """\
serial_port: /dev/ttyUSB0
gantry_type: cub_xl
cnc:
  factory_z_travel_mm: 90.0
"""
        path = _write_temp_yaml(yaml_content)
        try:
            with pytest.raises(Exception, match="working_volume"):
                load_gantry_from_yaml(path)
        finally:
            os.unlink(path)

    def test_reversed_bounds_raises_validation_error(self):
        yaml_content = """\
serial_port: /dev/ttyUSB0
gantry_type: cub_xl
cnc:
  factory_z_travel_mm: 90.0
working_volume:
  x_min: 300.0
  x_max: 0.0
  y_min: 0.0
  y_max: 200.0
  z_min: 0.0
  z_max: 80.0
"""
        path = _write_temp_yaml(yaml_content)
        try:
            with pytest.raises(Exception, match="x_min"):
                load_gantry_from_yaml(path)
        finally:
            os.unlink(path)

    def test_extra_top_level_key_raises(self):
        yaml_content = VALID_GANTRY_YAML + "extra_key: value\n"
        path = _write_temp_yaml(yaml_content)
        try:
            with pytest.raises(Exception):
                load_gantry_from_yaml(path)
        finally:
            os.unlink(path)

    def test_empty_file_raises(self):
        path = _write_temp_yaml("")
        try:
            with pytest.raises(Exception):
                load_gantry_from_yaml(path)
        finally:
            os.unlink(path)


class TestLoadGantryFromYamlSafe:

    def test_safe_returns_gantry_config_on_valid_yaml(self):
        path = _write_temp_yaml(VALID_GANTRY_YAML)
        try:
            result = load_gantry_from_yaml_safe(path)
            assert isinstance(result, GantryConfig)
        finally:
            os.unlink(path)

    def test_safe_wraps_validation_error_in_gantry_loader_error(self):
        yaml_content = """\
serial_port: /dev/ttyUSB0
cnc:
  factory_z_travel_mm: 90.0
"""
        path = _write_temp_yaml(yaml_content)
        try:
            with pytest.raises(GantryLoaderError):
                load_gantry_from_yaml_safe(path)
        finally:
            os.unlink(path)

    def test_safe_wraps_file_not_found_in_gantry_loader_error(self):
        with pytest.raises(GantryLoaderError):
            load_gantry_from_yaml_safe("/nonexistent/path.yaml")

    def test_safe_error_has_how_to_fix_guidance(self):
        yaml_content = """\
serial_port: /dev/ttyUSB0
cnc:
  factory_z_travel_mm: 90.0
"""
        path = _write_temp_yaml(yaml_content)
        try:
            with pytest.raises(GantryLoaderError, match="How to fix"):
                load_gantry_from_yaml_safe(path)
        finally:
            os.unlink(path)

    def test_safe_wraps_yaml_parse_error(self):
        path = _write_temp_yaml("{{bad yaml")
        try:
            with pytest.raises(GantryLoaderError, match="How to fix"):
                load_gantry_from_yaml_safe(path)
        finally:
            os.unlink(path)
