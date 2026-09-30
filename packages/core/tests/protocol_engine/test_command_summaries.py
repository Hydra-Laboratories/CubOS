"""Tests for per-command display summaries.

Summaries are operator-facing display only. The contract that matters is that
``describe`` always returns *something* readable — a formatter must never be
able to fail a caller that is rendering a run.
"""

import pytest

import cubos.protocol_engine.commands  # noqa: F401 - registers every command
from cubos.protocol_engine.commands import _summaries
from cubos.protocol_engine.registry import CommandRegistry, _fallback_summary


@pytest.fixture
def registry():
    return CommandRegistry.instance()


class TestFallback:

    def test_empty_args(self):
        assert _fallback_summary({}) == ""

    def test_key_value_rendering(self):
        assert _fallback_summary({"a": 1, "b": "x"}) == "a=1, b=x"

    def test_truncates_long_renderings(self):
        rendered = _fallback_summary({"k": "y" * 200})
        assert len(rendered) == 80
        assert rendered.endswith("…")


class TestDescribe:

    def test_uses_the_registered_formatter(self, registry):
        summary = registry.get("transfer").describe(
            {
                "source": "stock.A1",
                "destination": "plate.B3",
                "volume_ul": 500.0,
                "speed": 50.0,
            }
        )
        assert "stock.A1" in summary
        assert "plate.B3" in summary
        assert "500 µL" in summary
        # Noise like speed must not reach the operator's one-line view.
        assert "speed" not in summary

    def test_falls_back_when_the_formatter_raises(self, registry):
        # `transfer`'s formatter reads keys that are absent here; describe()
        # must degrade rather than propagate.
        summary = registry.get("transfer").describe({"unexpected": 1})
        assert summary == "unexpected=1"

    def test_every_registered_command_describes_without_raising(self, registry):
        for name in registry.command_names:
            assert isinstance(registry.get(name).describe({}), str)

    def test_command_without_a_formatter_uses_the_fallback(self):
        from pydantic import BaseModel

        from cubos.protocol_engine.registry import RegisteredCommand

        command = RegisteredCommand("x", lambda: None, BaseModel, summary=None)
        assert command.describe({"a": 1}) == "a=1"


class TestFormatters:

    def test_volume_drops_trailing_zeros(self):
        assert _summaries._volume(500.0) == "500 µL"
        assert _summaries._volume(12.5) == "12.5 µL"

    def test_volume_tolerates_non_numeric(self):
        assert _summaries._volume("lots") == "lots µL"

    def test_volume_list_is_empty_for_no_values(self):
        assert _summaries._volumes(None) == ""
        assert _summaries._volumes([]) == ""

    def test_position_renders_a_coordinate_mapping(self):
        assert _summaries._position({"x": 1.0, "y": 2.5}) == "(x=1, y=2.5)"

    def test_pause_with_and_without_a_reason(self):
        assert _summaries.pause({"seconds": 30}) == "30s"
        assert _summaries.pause({"seconds": 30, "reason": "settling"}) == "30s   settling"

    def test_volume_list_truncates(self):
        rendered = _summaries._volumes([1, 2, 3, 4, 5, 6])
        assert rendered.startswith("[1, 2, 3, …]")

    def test_position_renders_coordinates(self):
        assert _summaries._position([1.0, 2.0, 3.5]) == "(1, 2, 3.5)"

    def test_position_renders_names(self):
        assert _summaries._position("plate.A1") == "plate.A1"

    def test_serial_transfer_range(self):
        summary = _summaries.serial_transfer(
            {"source": "s.A1", "plate": "p", "axis": "row", "volume_range": [10.0, 100.0]}
        )
        assert "s.A1 → p ROW" in summary
        assert "10–100 µL" in summary

    def test_serial_transfer_explicit_volumes(self):
        summary = _summaries.serial_transfer(
            {"source": "s.A1", "plate": "p", "axis": "col", "volumes": [10.0, 20.0]}
        )
        assert "[10, 20] µL" in summary

    def test_clear_well_target_versus_explicit(self):
        assert "down to 0 µL" in _summaries.clear_well({"well": "p.A1"})
        assert "50 µL" in _summaries.clear_well({"well": "p.A1", "volume_ul": 50.0})

    def test_cure_without_intensity(self):
        summary = _summaries.cure(
            {"instrument": "uv_curing", "position": "plate.A1", "exposure_time": 2.0}
        )
        assert summary == "uv_curing @ plate.A1   2.0s"

    def test_cure_with_intensity(self):
        summary = _summaries.cure(
            {
                "instrument": "uv_curing",
                "position": "plate.A1",
                "exposure_time": 2.0,
                "intensity": 75.0,
            }
        )
        assert summary == "uv_curing @ plate.A1   2.0s @ 75.0%"

    def test_transfer_notes_a_liquid_class(self):
        summary = _summaries.transfer(
            {
                "source": "a",
                "destination": "b",
                "volume_ul": 10.0,
                "liquid_class": "viscous",
            }
        )
        assert "(viscous)" in summary
