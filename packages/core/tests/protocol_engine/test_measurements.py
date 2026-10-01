"""Tests for protocol-layer measurement normalization."""

from __future__ import annotations

import pytest

from cubos.instruments.filmetrics.models import MeasurementResult
from cubos.instruments.uv_curing.models import CureResult
from cubos.instruments.uvvis_ccs.models import UVVisSpectrum
from cubos.protocol_engine import is_measurement_result as exported_is_measurement_result
from cubos.protocol_engine.measurements import (
    MEASUREMENT_RESULT_TYPES,
    is_measurement_result,
    normalize_measurement,
)


def _make_uvvis_spectrum() -> UVVisSpectrum:
    return UVVisSpectrum(
        wavelengths=(400.0, 500.0, 600.0),
        intensities=(0.1, 0.2, 0.3),
        integration_time_s=0.24,
    )


class TestNormalizeMeasurement:


    def test_unknown_measurement_type_raises_type_error(self):
        with pytest.raises(TypeError, match="Unsupported measurement result"):
            normalize_measurement(
                instrument_name="uvvis",
                method_name="measure",
                raw_result=object(),
            )


    def test_normalize_asmi_indentation_with_surface_detection(self):
        raw_result = {
            "measurements": [
                {"timestamp": 1.0, "z_mm": 6.5, "raw_force_n": 0.10, "corrected_force_n": 0.01, "direction": "down"},
            ],
            "baseline_avg": 0.09,
            "baseline_std": 0.001,
            "force_exceeded": False,
            "data_points": 1,
            "detect_surface": True,
            "surface_z_mm": 7.0,
            "surface_trigger_force_n": 0.02,
            "surface_search_step_mm": 0.5,
            "surface_force_threshold_n": 0.01,
        }

        measurement = normalize_measurement(
            instrument_name="asmi",
            method_name="indentation",
            raw_result=raw_result,
        )

        assert measurement.metadata["detect_surface"] is True
        assert measurement.metadata["surface_z_mm"] == pytest.approx(7.0)
        assert measurement.metadata["surface_trigger_force_n"] == pytest.approx(0.02)
        assert measurement.metadata["surface_search_step_mm"] == pytest.approx(0.5)
        assert measurement.metadata["surface_force_threshold_n"] == pytest.approx(0.01)

    def test_normalize_asmi_without_surface_detection_omits_surface_keys(self):
        raw_result = {
            "measurements": [
                {"timestamp": 1.0, "z_mm": -73.01, "raw_force_n": 0.10, "corrected_force_n": 0.01, "direction": "down"},
            ],
            "baseline_avg": 0.09,
            "baseline_std": 0.001,
            "force_exceeded": False,
            "data_points": 1,
        }

        measurement = normalize_measurement(
            instrument_name="asmi",
            method_name="indentation",
            raw_result=raw_result,
        )

        assert "surface_z_mm" not in measurement.metadata
        assert "detect_surface" not in measurement.metadata

    def test_normalize_asmi_requires_sample_metadata(self):
        raw_result = {
            "measurements": [
                {"z_mm": -73.01, "raw_force_n": 0.10, "corrected_force_n": 0.01, "direction": "down"},
                {"z_mm": -73.02, "raw_force_n": 0.11, "corrected_force_n": 0.02},
                {"z_mm": -73.01, "raw_force_n": 0.09, "corrected_force_n": 0.00, "direction": "up"},
            ],
            "baseline_avg": 0.09,
            "baseline_std": 0.001,
            "force_exceeded": False,
            "data_points": 3,
            "measure_with_return": True,
        }

        with pytest.raises(KeyError, match="timestamp"):
            normalize_measurement(
                instrument_name="asmi",
                method_name="indentation",
                raw_result=raw_result,
            )


class TestIsMeasurementResult:

    def test_true_for_every_concrete_class_normalized_by_measurements(self):
        assert set(MEASUREMENT_RESULT_TYPES) == {
            UVVisSpectrum,
            MeasurementResult,
            CureResult,
        }
        for cls in MEASUREMENT_RESULT_TYPES:
            assert is_measurement_result(cls) is True
            assert exported_is_measurement_result(cls) is True


    def test_false_for_plain_dict_object_and_unknown_class(self):
        assert is_measurement_result({}) is False
        assert is_measurement_result(object) is False
