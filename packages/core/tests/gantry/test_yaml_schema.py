"""Tests for gantry YAML schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cubos.gantry.yaml_schema import GantryYamlSchema


def _valid_gantry_dict() -> dict:
    return {
        "serial_port": "/dev/cu.usbserial-2130",
        "gantry_type": "cub_xl",
        "cnc": {"factory_z_travel_mm": 90.0},
        "working_volume": {
            "x_min": 0.0,
            "x_max": 300.0,
            "y_min": 0.0,
            "y_max": 200.0,
            "z_min": 0.0,
            "z_max": 80.0,
        },
    }


class TestGantryYamlSchema:


    def test_origin_policy_defaults_to_deck_origin(self):
        schema = GantryYamlSchema.model_validate(_valid_gantry_dict())

        assert schema.origin_policy == "deck_origin"

    def test_origin_policy_accepts_home_origin(self):
        data = _valid_gantry_dict()
        data["origin_policy"] = "home_origin"
        data["working_volume"] = {
            "x_min": -300.0,
            "x_max": 0.0,
            "y_min": -200.0,
            "y_max": 0.0,
            "z_min": -80.0,
            "z_max": 0.0,
        }

        schema = GantryYamlSchema.model_validate(data)

        assert schema.origin_policy == "home_origin"

    def test_origin_policy_rejects_unknown_value(self):
        data = _valid_gantry_dict()
        data["origin_policy"] = "bogus"

        with pytest.raises(ValidationError, match="origin_policy"):
            GantryYamlSchema.model_validate(data)

    def test_homing_strategy_is_rejected(self):
        data = _valid_gantry_dict()
        data["cnc"]["homing_strategy"] = "standard"
        with pytest.raises(ValidationError, match="homing_strategy"):
            GantryYamlSchema.model_validate(data)

    def test_missing_serial_port_raises(self):
        data = _valid_gantry_dict()
        del data["serial_port"]
        with pytest.raises(ValidationError, match="serial_port"):
            GantryYamlSchema.model_validate(data)

    def test_missing_gantry_type_raises(self):
        data = _valid_gantry_dict()
        del data["gantry_type"]
        with pytest.raises(ValidationError, match="gantry_type"):
            GantryYamlSchema.model_validate(data)


    def test_unknown_gantry_type_rejected(self):
        data = _valid_gantry_dict()
        data["gantry_type"] = "other_machine"
        with pytest.raises(ValidationError, match="gantry_type"):
            GantryYamlSchema.model_validate(data)

    def test_missing_cnc_section_raises(self):
        data = _valid_gantry_dict()
        del data["cnc"]
        with pytest.raises(ValidationError, match="cnc"):
            GantryYamlSchema.model_validate(data)

    def test_missing_working_volume_raises(self):
        data = _valid_gantry_dict()
        del data["working_volume"]
        with pytest.raises(ValidationError, match="working_volume"):
            GantryYamlSchema.model_validate(data)

    @pytest.mark.parametrize("field", ["x_min", "x_max", "y_min", "y_max", "z_min", "z_max"])
    def test_missing_bound_field_raises(self, field: str):
        data = _valid_gantry_dict()
        del data["working_volume"][field]
        with pytest.raises(ValidationError):
            GantryYamlSchema.model_validate(data)

    def test_x_min_ge_x_max_raises(self):
        data = _valid_gantry_dict()
        data["working_volume"]["x_min"] = 300.0
        data["working_volume"]["x_max"] = 0.0
        with pytest.raises(ValidationError, match="x_min"):
            GantryYamlSchema.model_validate(data)

    def test_y_min_ge_y_max_raises(self):
        data = _valid_gantry_dict()
        data["working_volume"]["y_min"] = 200.0
        data["working_volume"]["y_max"] = 0.0
        with pytest.raises(ValidationError, match="y_min"):
            GantryYamlSchema.model_validate(data)

    def test_z_min_ge_z_max_raises(self):
        data = _valid_gantry_dict()
        data["working_volume"]["z_min"] = 80.0
        data["working_volume"]["z_max"] = 0.0
        with pytest.raises(ValidationError, match="z_min"):
            GantryYamlSchema.model_validate(data)

    def test_equal_min_and_max_raises(self):
        data = _valid_gantry_dict()
        data["working_volume"]["x_min"] = 0.0
        data["working_volume"]["x_max"] = 0.0
        with pytest.raises(ValidationError, match="x_min"):
            GantryYamlSchema.model_validate(data)

    def test_missing_factory_z_travel_mm_raises(self):
        data = _valid_gantry_dict()
        del data["cnc"]["factory_z_travel_mm"]
        with pytest.raises(ValidationError, match="factory_z_travel_mm"):
            GantryYamlSchema.model_validate(data)

    def test_factory_z_travel_mm_must_be_positive(self):
        data = _valid_gantry_dict()
        data["cnc"]["factory_z_travel_mm"] = 0.0
        with pytest.raises(ValidationError, match="factory_z_travel_mm"):
            GantryYamlSchema.model_validate(data)

    def test_factory_z_travel_mm_must_cover_working_z_span(self):
        data = _valid_gantry_dict()
        data["cnc"]["factory_z_travel_mm"] = 79.9
        with pytest.raises(ValidationError, match="factory_z_travel_mm"):
            GantryYamlSchema.model_validate(data)

    def test_factory_z_travel_allows_offset_deck_frame_z_max(self):
        data = _valid_gantry_dict()
        data["cnc"]["factory_z_travel_mm"] = 110.0
        data["working_volume"]["z_min"] = 25.0
        data["working_volume"]["z_max"] = 135.0
        schema = GantryYamlSchema.model_validate(data)

        assert schema.working_volume.z_min == 25.0
        assert schema.working_volume.z_max == 135.0


    def test_calibration_block_height_must_be_positive(self):
        data = _valid_gantry_dict()
        data["cnc"]["calibration_block_height_mm"] = 0.0
        with pytest.raises(ValidationError, match="calibration_block_height_mm"):
            GantryYamlSchema.model_validate(data)


    def test_safe_z_defaults_to_z_max_when_omitted(self):
        data = _valid_gantry_dict()
        schema = GantryYamlSchema.model_validate(data)
        assert schema.cnc.safe_z is None
        assert schema.safe_z == schema.working_volume.z_max

    def test_safe_z_above_z_max_rejected(self):
        data = _valid_gantry_dict()
        data["cnc"]["safe_z"] = data["working_volume"]["z_max"] + 1.0
        with pytest.raises(ValidationError, match="safe_z"):
            GantryYamlSchema.model_validate(data)

    def test_safe_z_below_z_min_rejected(self):
        data = _valid_gantry_dict()
        data["cnc"]["safe_z"] = data["working_volume"]["z_min"] - 1.0
        with pytest.raises(ValidationError, match="safe_z"):
            GantryYamlSchema.model_validate(data)

    def test_old_structure_clearance_z_field_rejected(self):
        data = _valid_gantry_dict()
        data["cnc"]["structure_clearance_z"] = 75.0
        with pytest.raises(ValidationError):
            GantryYamlSchema.model_validate(data)

    def test_machine_structures_yaml_is_rejected(self):
        data = _valid_gantry_dict()
        data["machine_structures"] = {
            "right_x_max_rail": {
                "type": "box",
                "x_min": 480.0,
                "x_max": 540.0,
                "y_min": 0.0,
                "y_max": 300.0,
                "z_min": 0.0,
                "z_max": 100.0,
            }
        }

        with pytest.raises(ValidationError, match="machine_structures"):
            GantryYamlSchema.model_validate(data)

    def test_extra_top_level_key_rejected(self):
        data = _valid_gantry_dict()
        data["unknown_field"] = "value"
        with pytest.raises(ValidationError):
            GantryYamlSchema.model_validate(data)

    def test_extra_working_volume_key_rejected(self):
        data = _valid_gantry_dict()
        data["working_volume"]["extra_field"] = 42
        with pytest.raises(ValidationError):
            GantryYamlSchema.model_validate(data)

    def test_extra_cnc_key_rejected(self):
        data = _valid_gantry_dict()
        data["cnc"]["extra_field"] = "value"
        with pytest.raises(ValidationError):
            GantryYamlSchema.model_validate(data)


    def test_instrument_measurement_height_rejected_with_migration_hint(self):
        """A stale `measurement_height:` on the instrument YAML must fail
        loudly. With ``extra="allow"`` it would otherwise slip through
        ``**kwargs`` for drivers that accept them, silently doing nothing."""
        data = _valid_gantry_dict()
        data["instruments"] = {
            "filmetrics": {
                "type": "filmetrics", "vendor": "kla",
                "measurement_height": -1.0,
            },
        }
        with pytest.raises(ValidationError, match="measurement_height"):
            GantryYamlSchema.model_validate(data)

    def test_instrument_interwell_scan_height_rejected_with_migration_hint(self):
        data = _valid_gantry_dict()
        data["instruments"] = {
            "asmi": {
                "type": "asmi", "vendor": "vernier",
                "interwell_scan_height": 8.0,
            },
        }
        with pytest.raises(ValidationError, match="interwell_scan_height"):
            GantryYamlSchema.model_validate(data)


class TestGrblSettingsYaml:


    def test_grbl_settings_parsed(self):
        data = _valid_gantry_dict()
        data["grbl_settings"] = {
            "dir_invert_mask": 2,
            "status_report": 1,
            "max_travel_x": 300.0,
            "max_travel_y": 200.0,
            "max_travel_z": 80.0,
        }
        schema = GantryYamlSchema.model_validate(data)
        assert schema.grbl_settings.dir_invert_mask == 2
        assert schema.grbl_settings.status_report == 1
        assert schema.grbl_settings.max_travel_x == 300.0
        assert schema.grbl_settings.max_travel_y == 200.0


    def test_grbl_settings_extra_field_rejected(self):
        data = _valid_gantry_dict()
        data["grbl_settings"] = {"unknown_setting": 42}
        with pytest.raises(ValidationError):
            GantryYamlSchema.model_validate(data)

    def test_grbl_settings_boolean_fields(self):
        data = _valid_gantry_dict()
        data["grbl_settings"] = {
            "hard_limits": True,
            "soft_limits": False,
            "homing_enable": True,
        }
        schema = GantryYamlSchema.model_validate(data)
        assert schema.grbl_settings.hard_limits is True
        assert schema.grbl_settings.soft_limits is False
        assert schema.grbl_settings.homing_enable is True
