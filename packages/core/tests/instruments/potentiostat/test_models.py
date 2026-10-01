"""Unit tests for potentiostat params/results dataclasses and exceptions."""

import pytest

from cubos.instruments.potentiostat.exceptions import (
    PotentiostatCommandError,
    PotentiostatConfigError,
    PotentiostatConnectionError,
    PotentiostatError,
    PotentiostatTimeoutError,
)
from cubos.instruments.potentiostat.models import (
    CAParams,
    CAResult,
    CPParams,
    CPResult,
    CVParams,
    OCPParams,
)


# --- Exception hierarchy ------------------------------------------------------


class TestExceptionHierarchy:

    @pytest.mark.parametrize(
        "cls",
        [
            PotentiostatConnectionError,
            PotentiostatCommandError,
            PotentiostatTimeoutError,
            PotentiostatConfigError,
        ],
    )
    def test_subclasses_inherit_from_potentiostat_error(self, cls):
        assert issubclass(cls, PotentiostatError)


# --- CVParams -----------------------------------------------------------------


class TestCVParams:


    def test_zero_scan_rate_rejected(self):
        with pytest.raises(PotentiostatConfigError, match="scan_rate_V_per_s"):
            CVParams(0.0, 0.5, -0.5, 0.0, 0.0)

    def test_negative_scan_rate_rejected(self):
        with pytest.raises(PotentiostatConfigError):
            CVParams(0.0, 0.5, -0.5, 0.0, -0.1)

    def test_zero_cycles_rejected(self):
        with pytest.raises(PotentiostatConfigError, match="cycles"):
            CVParams(0.0, 0.5, -0.5, 0.0, 0.05, cycles=0)

    def test_zero_sampling_interval_rejected(self):
        with pytest.raises(PotentiostatConfigError, match="sampling_interval_s"):
            CVParams(0.0, 0.5, -0.5, 0.0, 0.05, sampling_interval_s=0.0)

    def test_identical_vertices_rejected(self):
        with pytest.raises(PotentiostatConfigError, match="vertex"):
            CVParams(0.0, 0.5, 0.5, 0.0, 0.05)


# --- OCPParams / CAParams / CPParams -----------------------------------------


class TestDurationParams:

    @pytest.mark.parametrize(
        "cls,kwargs",
        [
            (OCPParams, {"duration_s": 10.0}),
            (CAParams, {"potential_V": 0.5, "duration_s": 10.0}),
            (CPParams, {"current_A": 1e-3, "duration_s": 10.0}),
        ],
    )
    def test_valid(self, cls, kwargs):
        p = cls(**kwargs)
        assert p.duration_s == 10.0

    @pytest.mark.parametrize(
        "cls,kwargs",
        [
            (OCPParams, {"duration_s": 0.0}),
            (CAParams, {"potential_V": 0.5, "duration_s": 0.0}),
            (CPParams, {"current_A": 1e-3, "duration_s": 0.0}),
        ],
    )
    def test_zero_duration_rejected(self, cls, kwargs):
        with pytest.raises(PotentiostatConfigError, match="duration_s"):
            cls(**kwargs)

    @pytest.mark.parametrize(
        "cls,kwargs",
        [
            (OCPParams, {"duration_s": 1.0, "sampling_interval_s": 0.0}),
            (CAParams, {"potential_V": 0.5, "duration_s": 1.0, "sampling_interval_s": 0.0}),
            (CPParams, {"current_A": 1e-3, "duration_s": 1.0, "sampling_interval_s": 0.0}),
        ],
    )
    def test_zero_sampling_interval_rejected(self, cls, kwargs):
        with pytest.raises(PotentiostatConfigError, match="sampling_interval_s"):
            cls(**kwargs)

    def test_sampling_interval_larger_than_duration_rejected(self):
        with pytest.raises(PotentiostatConfigError, match="sampling_interval_s"):
            OCPParams(duration_s=0.5, sampling_interval_s=1.0)


# --- Result dataclasses -------------------------------------------------------


class TestCAResult:

    def test_fields_and_technique(self):
        r = CAResult(
            time_s=(0.0, 0.01),
            voltage_v=(0.5, 0.5),
            current_a=(1e-6, 9e-7),
            sample_period_s=0.01,
            duration_s=0.02,
            step_potential_v=0.5,
            vendor="admiral",
        )
        assert r.technique == "ca"
        assert r.is_valid


class TestCPResult:

    def test_fields_and_technique(self):
        r = CPResult(
            time_s=(0.0, 0.01),
            voltage_v=(0.1, 0.11),
            current_a=(1e-3, 1e-3),
            sample_period_s=0.01,
            duration_s=0.02,
            step_current_a=1e-3,
            vendor="admiral",
        )
        assert r.technique == "cp"
        assert r.is_valid
        assert r.step_current_a == 1e-3
