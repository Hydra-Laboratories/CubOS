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


class TestFormatters:


    def test_serial_transfer_explicit_volumes(self):
        summary = _summaries.serial_transfer(
            {"source": "s.A1", "plate": "p", "axis": "col", "volumes": [10.0, 20.0]}
        )
        assert "[10, 20] µL" in summary


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
