"""Tests for the EmStat (hardpotato) potentiostat driver.

Online tests mock hardpotato at the ``_load_hardpotato`` seam with fake
technique classes whose ``.data`` mimics MethodSCRIPT curves as parsed by
hardpotato/PalmSens: a list of curves, each a list of rows, each a list of
variables carrying ``.value`` — (t, E) columns for OCP, (t, E, i) for CV/CA.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cubos.instruments.potentiostat.exceptions import (
    PotentiostatCommandError,
    PotentiostatConfigError,
    PotentiostatConnectionError,
)
from cubos.instruments.potentiostat.models import (
    CAParams,
    CAResult,
    CPParams,
    CVParams,
    CVResult,
    OCPParams,
    OCPResult,
)
from cubos.instruments.potentiostat.vendors.emstat import EmstatPotentiostat


def _var(value: float) -> SimpleNamespace:
    return SimpleNamespace(value=value)


def _curves(*rows: tuple[float, ...]) -> list[list[list[SimpleNamespace]]]:
    """One curve holding the given rows of column values."""
    return [[[_var(v) for v in row] for row in rows]]


class _FakeTechnique:
    """Stands in for hp.potentiostat.OCP/CV/CA."""

    def __init__(self, kwargs_log: list[dict], data, run_error=None):
        self._kwargs_log = kwargs_log
        self._data = data
        self._run_error = run_error

    def __call__(self, **kwargs):
        self._kwargs_log.append(kwargs)
        return self

    def run(self):
        if self._run_error is not None:
            raise self._run_error
        if self._data is not None:
            self.data = self._data


class _FakeSetup:
    def __init__(self, calls: list, connected: bool = True, error=None):
        self._calls = calls
        self._connected = connected
        self._error = error

    def __call__(self, *args, **kwargs):
        self._calls.append((args, kwargs))
        if self._error is not None:
            raise self._error
        return self

    def check_connection(self):
        return self._connected


def _fake_hp(
    *,
    setup_calls=None,
    connected=True,
    setup_error=None,
    ocp_data=None,
    cv_data=None,
    ca_data=None,
    run_error=None,
    kwargs_log=None,
):
    kwargs_log = kwargs_log if kwargs_log is not None else []
    potentiostat = SimpleNamespace(
        Setup=_FakeSetup(
            setup_calls if setup_calls is not None else [],
            connected=connected,
            error=setup_error,
        ),
        OCP=_FakeTechnique(kwargs_log, ocp_data, run_error),
        CV=_FakeTechnique(kwargs_log, cv_data, run_error),
        CA=_FakeTechnique(kwargs_log, ca_data, run_error),
    )
    return SimpleNamespace(potentiostat=potentiostat)


def _connected_driver(hp, port="/dev/ttyACM1", **kwargs):
    driver = EmstatPotentiostat(port=port, **kwargs)
    with patch(
        "cubos.instruments.potentiostat.vendors.emstat._load_hardpotato",
        return_value=hp,
    ):
        driver.connect()
    return driver


class TestConstructor:

    def test_defaults(self):
        p = EmstatPotentiostat()
        assert p.name == "EmstatPotentiostat"
        assert p._port == ""
        assert p._model == "emstat4_lr"
        assert p._offline is False
        assert p.vendor == "emstat"

    def test_offsets_propagate_to_base(self):
        p = EmstatPotentiostat(offset_x=1.5, offset_y=-2.0, depth=3.0)
        assert p.offset_x == 1.5
        assert p.offset_y == -2.0
        assert p.depth == 3.0

    def test_empty_model_rejected(self):
        with pytest.raises(PotentiostatConfigError, match="model"):
            EmstatPotentiostat(model="")


class TestOfflineLifecycle:

    def test_connect_and_disconnect_are_noops(self):
        p = EmstatPotentiostat(offline=True)
        p.connect()
        p.disconnect()  # must not raise

    def test_health_check_true_in_offline(self):
        p = EmstatPotentiostat(offline=True)
        assert p.health_check() is True

    def test_run_OCP_offline(self):
        p = EmstatPotentiostat(offline=True)
        result = p.run_OCP(OCPParams(duration_s=1.0, sampling_interval_s=0.1))
        assert isinstance(result, OCPResult)
        assert result.is_valid
        assert len(result.voltage_v) == len(result.time_s) == 10
        assert result.vendor == "emstat"
        assert result.metadata["device_id"] == "offline"
        assert result.metadata["aborted"] is False

    def test_run_OCP_offline_is_deterministic(self):
        params = OCPParams(duration_s=1.0, sampling_interval_s=0.1)
        r1 = EmstatPotentiostat(offline=True).run_OCP(params)
        r2 = EmstatPotentiostat(offline=True).run_OCP(params)
        assert r1.voltage_v == r2.voltage_v

    def test_run_CV_offline(self):
        p = EmstatPotentiostat(offline=True)
        result = p.run_CV(
            CVParams(
                start_V=0.0, vertex1_V=0.5, vertex2_V=-0.5, end_V=0.0,
                scan_rate_V_per_s=0.1, cycles=2, sampling_interval_s=0.05,
            )
        )
        assert isinstance(result, CVResult)
        assert result.is_valid
        assert result.vendor == "emstat"

    def test_run_CA_offline(self):
        p = EmstatPotentiostat(offline=True)
        result = p.run_CA(
            CAParams(potential_V=0.6, duration_s=0.5, sampling_interval_s=0.05)
        )
        assert isinstance(result, CAResult)
        assert result.is_valid
        assert all(v == 0.6 for v in result.voltage_v)

    def test_run_CP_raises_not_implemented_offline_too(self):
        p = EmstatPotentiostat(offline=True)
        with pytest.raises(NotImplementedError, match="chronopotentiometry"):
            p.run_CP(CPParams(current_A=1e-3, duration_s=0.5))


class TestConnect:

    def test_missing_port_raises_config_error(self):
        p = EmstatPotentiostat()
        with pytest.raises(PotentiostatConfigError, match="port"):
            p.connect()

    def test_missing_dependency_raises_with_install_hint(self):
        p = EmstatPotentiostat(port="/dev/ttyACM1")
        with patch.dict(sys.modules, {"hardpotato": None}):
            with pytest.raises(
                PotentiostatConnectionError, match="potentiostat-emstat"
            ):
                p.connect()

    def test_connects_and_passes_model_port_folder(self):
        setup_calls: list = []
        hp = _fake_hp(setup_calls=setup_calls)
        p = _connected_driver(hp, data_dir="/tmp/emstat-data")
        assert p.health_check() is True
        (args, kwargs) = setup_calls[0]
        assert args == ("emstat4_lr", ".", "/tmp/emstat-data")
        assert kwargs == {"port": "/dev/ttyACM1", "verbose": 0}

    def test_no_device_raises_connection_error(self):
        hp = _fake_hp(connected=False)
        p = EmstatPotentiostat(port="/dev/ttyACM1")
        with patch(
            "cubos.instruments.potentiostat.vendors.emstat._load_hardpotato",
            return_value=hp,
        ):
            with pytest.raises(PotentiostatConnectionError, match="ttyACM1"):
                p.connect()
        assert p.health_check() is False

    def test_setup_failure_raises_connection_error(self):
        hp = _fake_hp(setup_error=RuntimeError("bad serial"))
        p = EmstatPotentiostat(port="/dev/ttyACM1")
        with patch(
            "cubos.instruments.potentiostat.vendors.emstat._load_hardpotato",
            return_value=hp,
        ):
            with pytest.raises(PotentiostatConnectionError, match="bad serial"):
                p.connect()

    def test_check_connection_exception_wrapped(self):
        hp = _fake_hp()
        p = EmstatPotentiostat(port="/dev/ttyACM1")
        with patch(
            "cubos.instruments.potentiostat.vendors.emstat._load_hardpotato",
            return_value=hp,
        ):
            with patch.object(
                _FakeSetup, "check_connection", side_effect=OSError("port busy")
            ):
                with pytest.raises(PotentiostatConnectionError, match="port busy"):
                    p.connect()

    def test_disconnect_clears_state(self):
        p = _connected_driver(_fake_hp())
        p.disconnect()
        assert p.health_check() is False
        assert p._hp is None
        assert p._setup is None


class TestRunOCPOnline:

    def test_happy_path_returns_shaped_result(self):
        kwargs_log: list = []
        hp = _fake_hp(
            ocp_data=_curves((0.0, 0.31), (0.1, 0.32), (0.2, 0.33)),
            kwargs_log=kwargs_log,
        )
        p = _connected_driver(hp)
        result = p.run_OCP(OCPParams(duration_s=5.0, sampling_interval_s=0.1))
        assert isinstance(result, OCPResult)
        assert result.is_valid
        assert result.time_s == (0.0, 0.1, 0.2)
        assert result.voltage_v == (0.31, 0.32, 0.33)
        assert result.final_voltage_v == 0.33
        assert result.sample_period_s == 0.1
        assert result.duration_s == 5.0
        assert result.vendor == "emstat"
        assert result.metadata["port"] == "/dev/ttyACM1"
        assert result.metadata["model"] == "emstat4_lr"
        assert result.metadata["aborted"] is False
        assert kwargs_log[0]["ttot"] == 5.0
        assert kwargs_log[0]["dt"] == 0.1
        assert kwargs_log[0]["header"] == "OCP"
        # run() needs .port when Setup got an explicit port (hardpotato only
        # sets it itself on the auto-detect path)
        assert hp.potentiostat.OCP.port == "/dev/ttyACM1"

    def test_multi_curve_data_is_concatenated(self):
        two_curves = [
            [[_var(0.0), _var(0.1)], [_var(0.1), _var(0.2)]],
            [[_var(0.2), _var(0.3)]],
        ]
        hp = _fake_hp(ocp_data=two_curves)
        p = _connected_driver(hp)
        result = p.run_OCP(OCPParams(duration_s=1.0, sampling_interval_s=0.1))
        assert result.voltage_v == (0.1, 0.2, 0.3)

    def test_run_without_connect_raises_command_error(self):
        p = EmstatPotentiostat(port="/dev/ttyACM1")
        with pytest.raises(PotentiostatCommandError, match="not connected"):
            p.run_OCP(OCPParams(duration_s=1.0))

    def test_run_failure_wrapped_as_command_error(self):
        hp = _fake_hp(run_error=RuntimeError("serial dropped"))
        p = _connected_driver(hp)
        with pytest.raises(PotentiostatCommandError, match="serial dropped"):
            p.run_OCP(OCPParams(duration_s=1.0))

    def test_empty_data_raises_command_error(self):
        hp = _fake_hp(ocp_data=[])
        p = _connected_driver(hp)
        with pytest.raises(PotentiostatCommandError, match="no data"):
            p.run_OCP(OCPParams(duration_s=1.0))

    def test_malformed_package_raises_command_error(self):
        hp = _fake_hp(ocp_data=[[[_var(0.0)]]])  # row missing the E column
        p = _connected_driver(hp)
        with pytest.raises(PotentiostatCommandError, match="package shape"):
            p.run_OCP(OCPParams(duration_s=1.0))

    def test_file_stems_are_unique_across_runs(self):
        kwargs_log: list = []
        hp = _fake_hp(ocp_data=_curves((0.0, 0.1)), kwargs_log=kwargs_log)
        p = _connected_driver(hp)
        p.run_OCP(OCPParams(duration_s=1.0))
        p.run_OCP(OCPParams(duration_s=1.0))
        stems = [k["fileName"] for k in kwargs_log]
        assert len(set(stems)) == 2


class TestRunCVOnline:

    def test_param_mapping_and_result(self):
        kwargs_log: list = []
        hp = _fake_hp(
            cv_data=_curves((0.0, -0.2, 1e-6), (0.1, 0.0, 2e-6), (0.2, 0.2, 3e-6)),
            kwargs_log=kwargs_log,
        )
        p = _connected_driver(hp)
        result = p.run_CV(
            CVParams(
                start_V=-0.2, vertex1_V=0.2, vertex2_V=-0.2, end_V=-0.2,
                scan_rate_V_per_s=0.1, cycles=3, sampling_interval_s=0.01,
            )
        )
        assert isinstance(result, CVResult)
        assert result.is_valid
        assert result.current_a == (1e-6, 2e-6, 3e-6)
        assert result.cycles == 3
        sent = kwargs_log[0]
        assert sent["Eini"] == -0.2
        assert sent["Ev1"] == 0.2
        assert sent["Ev2"] == -0.2
        assert sent["Efin"] == -0.2
        assert sent["sr"] == 0.1
        assert sent["dE"] == pytest.approx(0.001)
        # hardpotato emits nscans(nSweeps - 1); pass one extra so the device
        # performs the requested three full cycles.
        assert sent["nSweeps"] == 4
        assert result.step_size_v == pytest.approx(0.001)

    @pytest.mark.parametrize(
        "params, message",
        [
            (
                dict(start_V=0.0, vertex1_V=0.5, vertex2_V=-0.5, end_V=0.0,
                     scan_rate_V_per_s=0.05, sampling_interval_s=0.01),
                "step size",
            ),
            (
                dict(start_V=0.0, vertex1_V=0.5, vertex2_V=-0.5, end_V=0.0,
                     scan_rate_V_per_s=0.0005, sampling_interval_s=2.0),
                "scan_rate",
            ),
            (
                dict(start_V=0.0, vertex1_V=0.5, vertex2_V=-0.5, end_V=0.1,
                     scan_rate_V_per_s=0.1, sampling_interval_s=0.01),
                "end_V",
            ),
            (
                dict(start_V=0.0, vertex1_V=0.5004, vertex2_V=0.5008, end_V=0.0,
                     scan_rate_V_per_s=0.1, sampling_interval_s=0.01),
                "quantization",
            ),
        ],
    )
    def test_rejects_emstat_incompatible_settings_before_run(self, params, message):
        kwargs_log: list[dict] = []
        hp = _fake_hp(cv_data=_curves((0.0, 0.0, 1e-6)), kwargs_log=kwargs_log)
        p = _connected_driver(hp)
        with pytest.raises(PotentiostatConfigError, match=message):
            p.run_CV(CVParams(**params))
        assert kwargs_log == []

    @pytest.mark.parametrize("cycles", [10000, 1.5, True])
    def test_rejects_invalid_cycle_count_before_run(self, cycles):
        kwargs_log: list[dict] = []
        hp = _fake_hp(cv_data=_curves((0.0, 0.0, 1e-6)), kwargs_log=kwargs_log)
        p = _connected_driver(hp)
        with pytest.raises(PotentiostatConfigError, match="cycles"):
            p.run_CV(
                CVParams(
                    start_V=0.0, vertex1_V=0.5, vertex2_V=-0.5, end_V=0.0,
                    scan_rate_V_per_s=0.1, cycles=cycles,
                    sampling_interval_s=0.01,
                )
            )
        assert kwargs_log == []


class TestRunCAOnline:

    def test_param_mapping_and_result(self):
        kwargs_log: list = []
        hp = _fake_hp(
            ca_data=_curves((0.0, 0.5, 9e-6), (0.1, 0.5, 5e-6)),
            kwargs_log=kwargs_log,
        )
        p = _connected_driver(hp)
        result = p.run_CA(
            CAParams(potential_V=0.5, duration_s=2.0, sampling_interval_s=0.1)
        )
        assert isinstance(result, CAResult)
        assert result.is_valid
        assert result.step_potential_v == 0.5
        assert result.current_a == (9e-6, 5e-6)
        sent = kwargs_log[0]
        assert sent["Estep"] == 0.5
        assert sent["dt"] == 0.1
        assert sent["ttot"] == 2.0


class TestRunCPOnline:

    def test_raises_not_implemented(self):
        p = _connected_driver(_fake_hp())
        with pytest.raises(NotImplementedError, match="admiral"):
            p.run_CP(CPParams(current_A=1e-3, duration_s=1.0))
