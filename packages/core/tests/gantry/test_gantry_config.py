"""Tests for GantryConfig and WorkingVolume domain models."""

from __future__ import annotations

import pytest

from cubos.gantry.gantry_config import (
    GantryConfig,
    GantryType,
    WorkingVolume,
)


def _make_volume(
    x_min: float = 0.0,
    x_max: float = 300.0,
    y_min: float = 0.0,
    y_max: float = 200.0,
    z_min: float = 0.0,
    z_max: float = 80.0,
) -> WorkingVolume:
    return WorkingVolume(
        x_min=x_min,
        x_max=x_max,
        y_min=y_min,
        y_max=y_max,
        z_min=z_min,
        z_max=z_max,
    )


class TestWorkingVolume:

    def test_contains_interior_point(self):
        vol = _make_volume()
        assert vol.contains(150.0, 100.0, 40.0) is True

    def test_contains_point_on_min_boundary(self):
        vol = _make_volume()
        assert vol.contains(0.0, 0.0, 0.0) is True

    def test_contains_point_on_max_boundary(self):
        vol = _make_volume()
        assert vol.contains(300.0, 200.0, 80.0) is True

    def test_contains_point_on_mixed_boundaries(self):
        vol = _make_volume()
        assert vol.contains(0.0, 200.0, 40.0) is True

    def test_rejects_point_beyond_x_min(self):
        vol = _make_volume()
        assert vol.contains(-0.001, 100.0, 40.0) is False

    def test_rejects_point_beyond_x_max(self):
        vol = _make_volume()
        assert vol.contains(300.001, 100.0, 40.0) is False

    def test_rejects_point_beyond_y_min(self):
        vol = _make_volume()
        assert vol.contains(150.0, -0.001, 40.0) is False

    def test_rejects_point_beyond_y_max(self):
        vol = _make_volume()
        assert vol.contains(150.0, 200.001, 40.0) is False

    def test_rejects_point_beyond_z_min(self):
        vol = _make_volume()
        assert vol.contains(150.0, 100.0, -0.001) is False

    def test_rejects_point_beyond_z_max(self):
        vol = _make_volume()
        assert vol.contains(150.0, 100.0, 80.001) is False

    def test_each_axis_checked_independently(self):
        vol = _make_volume()
        assert vol.contains(150.0, 100.0, -0.001) is False
        assert vol.contains(150.0, -0.001, 40.0) is False
        assert vol.contains(-0.001, 100.0, 40.0) is False

    def test_rejects_reversed_x_bounds(self):
        with pytest.raises(ValueError, match="x_min"):
            _make_volume(x_min=300.0, x_max=0.0)

    def test_rejects_reversed_y_bounds(self):
        with pytest.raises(ValueError, match="y_min"):
            _make_volume(y_min=200.0, y_max=0.0)

    def test_rejects_reversed_z_bounds(self):
        with pytest.raises(ValueError, match="z_min"):
            _make_volume(z_min=80.0, z_max=0.0)

    def test_rejects_equal_bounds(self):
        with pytest.raises(ValueError, match="x_min"):
            _make_volume(x_min=0.0, x_max=0.0)

    def test_frozen_dataclass(self):
        vol = _make_volume()
        try:
            vol.x_min = 0.0
            assert False, "Should have raised"
        except AttributeError:
            pass


class TestGantryConfig:

    def test_stores_all_fields(self):
        vol = _make_volume()
        config = GantryConfig(
            serial_port="/dev/ttyUSB0",
            gantry_type=GantryType.CUB_XL,
            factory_z_travel_mm=90.0,
            working_volume=vol,
            safe_z=75.0,
        )
        assert config.serial_port == "/dev/ttyUSB0"
        assert config.gantry_type == GantryType.CUB_XL
        assert config.factory_z_travel_mm == 90.0
        assert config.working_volume is vol
        assert config.safe_z == 75.0

    def test_gantry_type_string_is_normalized_to_enum(self):
        config = GantryConfig(
            serial_port="/dev/ttyUSB0",
            gantry_type="cub_xl",
            factory_z_travel_mm=90.0,
            working_volume=_make_volume(),
        )
        assert config.gantry_type == GantryType.CUB_XL

    def test_rejects_unknown_gantry_type(self):
        with pytest.raises(ValueError, match="gantry_type"):
            GantryConfig(
                serial_port="/dev/ttyUSB0",
                gantry_type="other_machine",
                factory_z_travel_mm=90.0,
                working_volume=_make_volume(),
            )

    def test_frozen_dataclass(self):
        config = GantryConfig(
            serial_port="/dev/ttyUSB0",
            gantry_type=GantryType.CUB_XL,
            factory_z_travel_mm=90.0,
            working_volume=_make_volume(),
        )
        try:
            config.serial_port = "other"
            assert False, "Should have raised"
        except AttributeError:
            pass

    def test_rejects_negative_factory_z_travel_mm(self):
        with pytest.raises(ValueError, match="factory_z_travel_mm"):
            GantryConfig(
                serial_port="/dev/ttyUSB0",
                gantry_type=GantryType.CUB_XL,
                factory_z_travel_mm=-10.0,
                working_volume=_make_volume(),
            )

    def test_rejects_zero_factory_z_travel_mm(self):
        with pytest.raises(ValueError, match="factory_z_travel_mm"):
            GantryConfig(
                serial_port="/dev/ttyUSB0",
                gantry_type=GantryType.CUB_XL,
                factory_z_travel_mm=0.0,
                working_volume=_make_volume(),
            )

    def test_rejects_structure_clearance_outside_z_bounds(self):
        with pytest.raises(ValueError, match="safe_z"):
            GantryConfig(
                serial_port="/dev/ttyUSB0",
                gantry_type=GantryType.CUB_XL,
                factory_z_travel_mm=90.0,
                working_volume=_make_volume(z_min=0.0, z_max=80.0),
                safe_z=85.0,
            )

    def test_default_feed_rate_mm_min_defaults_to_none(self):
        config = GantryConfig(
            serial_port="/dev/ttyUSB0",
            gantry_type=GantryType.CUB_XL,
            factory_z_travel_mm=90.0,
            working_volume=_make_volume(),
        )
        assert config.default_feed_rate_mm_min is None

    def test_stores_default_feed_rate_mm_min(self):
        config = GantryConfig(
            serial_port="/dev/ttyUSB0",
            gantry_type=GantryType.CUB_XL,
            factory_z_travel_mm=90.0,
            working_volume=_make_volume(),
            default_feed_rate_mm_min=4000.0,
        )
        assert config.default_feed_rate_mm_min == 4000.0

    def test_rejects_zero_default_feed_rate_mm_min(self):
        with pytest.raises(ValueError, match="default_feed_rate_mm_min"):
            GantryConfig(
                serial_port="/dev/ttyUSB0",
                gantry_type=GantryType.CUB_XL,
                factory_z_travel_mm=90.0,
                working_volume=_make_volume(),
                default_feed_rate_mm_min=0.0,
            )

    def test_rejects_negative_default_feed_rate_mm_min(self):
        with pytest.raises(ValueError, match="default_feed_rate_mm_min"):
            GantryConfig(
                serial_port="/dev/ttyUSB0",
                gantry_type=GantryType.CUB_XL,
                factory_z_travel_mm=90.0,
                working_volume=_make_volume(),
                default_feed_rate_mm_min=-100.0,
            )


class TestWorkingVolumeSignedBounds:

    def test_allows_negative_x_min(self):
        vol = _make_volume(x_min=-1.0)
        assert vol.x_min == -1.0

    def test_allows_negative_y_min(self):
        vol = _make_volume(y_min=-1.0)
        assert vol.y_min == -1.0

    def test_allows_negative_z_min(self):
        vol = _make_volume(z_min=-1.0)
        assert vol.z_min == -1.0
