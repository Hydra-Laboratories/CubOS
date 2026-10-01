"""Tests for AdmiralPotentiostat driver: constructor, offline mode, and mocked online mode.

The online-mode tests never touch the real SquidstatPyLibrary. They replace
``_load_qt_bindings`` with a helper that returns a ``_QtBindings`` assembled
from ``MagicMock``s, so the driver's Qt plumbing can be exercised
deterministically.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cubos.instruments.potentiostat.vendors.admiral import AdmiralPotentiostat, _QtBindings
from cubos.instruments.potentiostat.exceptions import (
    PotentiostatCommandError,
    PotentiostatConfigError,
    PotentiostatConnectionError,
    PotentiostatTimeoutError,
)
from cubos.instruments.potentiostat.models import (
    CAParams, CAResult,
    CPParams, CPResult,
    CVParams, CVResult,
    OCPParams, OCPResult,
)


# --- Constructor --------------------------------------------------------------


class TestConstructor:


    def test_offsets_propagate_to_base(self):
        p = AdmiralPotentiostat(offset_x=1.5, offset_y=-2.0, depth=3.0)
        assert p.offset_x == 1.5
        assert p.offset_y == -2.0
        assert p.depth == 3.0

    def test_negative_channel_rejected(self):
        with pytest.raises(PotentiostatConfigError, match="channel"):
            AdmiralPotentiostat(channel=-1)


# --- Offline mode -------------------------------------------------------------


class TestOfflineLifecycle:

    def test_connect_and_disconnect_are_noops(self):
        p = AdmiralPotentiostat(offline=True)
        p.connect()
        p.disconnect()  # must not raise


    def test_run_CV_offline_returns_result_with_aligned_arrays(self):
        p = AdmiralPotentiostat(offline=True)
        p.connect()
        result = p.run_CV(
            CVParams(
                start_V=0.0, vertex1_V=0.5, vertex2_V=-0.5, end_V=0.0,
                scan_rate_V_per_s=0.1, cycles=2, sampling_interval_s=0.05,
            )
        )
        assert isinstance(result, CVResult)
        n = len(result.voltage_v)
        assert len(result.current_a) == n
        assert len(result.time_s) == n
        assert result.cycles == 2
        assert result.vendor == "admiral"
        assert result.metadata["aborted"] is False
        assert result.metadata["device_id"] == "offline"
        assert result.technique == "cv"


    def test_run_OCP_offline(self):
        p = AdmiralPotentiostat(offline=True)
        result = p.run_OCP(OCPParams(duration_s=1.0, sampling_interval_s=0.1))
        assert isinstance(result, OCPResult)
        assert len(result.voltage_v) == len(result.time_s) == 10
        assert result.duration_s == 1.0
        assert result.sample_period_s == 0.1
        assert result.metadata["aborted"] is False
        assert result.technique == "ocp"

    def test_run_CA_offline(self):
        p = AdmiralPotentiostat(offline=True)
        result = p.run_CA(CAParams(potential_V=0.6, duration_s=0.5, sampling_interval_s=0.05))
        assert isinstance(result, CAResult)
        assert all(v == 0.6 for v in result.voltage_v)
        assert len(result.current_a) == 10
        assert result.step_potential_v == 0.6
        assert result.technique == "ca"

    def test_run_CP_offline(self):
        p = AdmiralPotentiostat(offline=True)
        result = p.run_CP(CPParams(current_A=1e-3, duration_s=0.5, sampling_interval_s=0.05))
        assert isinstance(result, CPResult)
        assert all(c == 1e-3 for c in result.current_a)
        assert len(result.voltage_v) == 10
        assert result.step_current_a == 1e-3
        assert result.technique == "cp"


# --- Online mode helpers ------------------------------------------------------


def _make_qt_mock_bindings(
    *,
    schedule_device_connected: bool = True,
    schedule_experiment_stopped: bool = True,
    dc_samples: list[tuple[float, float, float]] | None = None,
):
    """Build a ``_QtBindings`` whose QEventLoop/QTimer fire pre-scripted events.

    Each entry in ``dc_samples`` is ``(timestamp_s, voltage_v, current_a)``.
    """
    squidstat = MagicMock()
    tracker = MagicMock()
    handler = MagicMock()
    squidstat.AisDeviceTracker.Instance.return_value = tracker
    tracker.getInstrumentHandler.return_value = handler

    # Track signal slots for both tracker and handler.
    signal_registry: dict[tuple[str, str], list] = {}

    def _register_signal(owner_name: str, signal_name: str):
        sig = MagicMock()
        key = (owner_name, signal_name)
        signal_registry[key] = []
        sig.connect.side_effect = lambda slot, k=key: signal_registry[k].append(slot)
        sig.disconnect.side_effect = lambda slot, k=key: (
            signal_registry[k].remove(slot)
            if slot in signal_registry[k] else None
        )
        return sig

    tracker.newDeviceConnected = _register_signal("tracker", "newDeviceConnected")
    handler.activeDCDataReady = _register_signal("handler", "activeDCDataReady")
    handler.experimentStopped = _register_signal("handler", "experimentStopped")

    # uploadExperimentToChannel / startUploadedExperiment return falsy on success.
    handler.uploadExperimentToChannel.return_value = 0
    handler.startUploadedExperiment.return_value = 0

    # QEventLoop that runs queued callbacks until quit.
    class _Loop:
        def __init__(self):
            self._queued: list = []
            self._quit = False

        def queue(self, fn):
            self._queued.append(fn)

        def quit(self):
            self._quit = True

        def exec(self):
            # Run queued callbacks until quit flag is set or queue empty.
            while self._queued and not self._quit:
                fn = self._queued.pop(0)
                fn()

    active_loop: dict[str, _Loop] = {}

    class _QEventLoopFactory:
        def __call__(self):
            loop = _Loop()
            active_loop["loop"] = loop
            # Iterate slot lists at call time (lambdas fire during exec()
            # after the driver has wired its slots).
            if schedule_device_connected:
                loop.queue(
                    lambda: [
                        slot("SquidStatMock")
                        for slot in list(
                            signal_registry[("tracker", "newDeviceConnected")]
                        )
                    ]
                )
            if dc_samples is not None:
                for (ts, v, cur) in dc_samples:
                    def _emit(ts=ts, v=v, cur=cur):
                        sample = MagicMock()
                        sample.timestamp = ts
                        sample.workingElectrodeVoltage = v
                        sample.current = cur
                        for slot in list(
                            signal_registry[("handler", "activeDCDataReady")]
                        ):
                            slot(0, sample)
                    loop.queue(_emit)
            if schedule_experiment_stopped:
                loop.queue(
                    lambda: [
                        slot(0, "completed")
                        for slot in list(
                            signal_registry[("handler", "experimentStopped")]
                        )
                    ]
                )
            return loop

    class _QTimer:
        @staticmethod
        def singleShot(_ms: int, callback):
            # For tests that want the timeout to fire, the test will NOT
            # schedule experiment_stopped; the queued callback just runs
            # last and calls quit (simulating timeout).
            loop = active_loop.get("loop")
            if loop is not None:
                loop.queue(callback)

    QCoreApplication = MagicMock()
    QCoreApplication.instance.return_value = None  # force creation
    QCoreApplication.return_value = MagicMock(name="app")

    bindings = _QtBindings(
        squidstat=squidstat,
        QCoreApplication=QCoreApplication,
        QEventLoop=_QEventLoopFactory(),
        QTimer=_QTimer,
    )
    return bindings, tracker, handler


class TestConnectMissingDependency:

    def test_raises_connection_error_with_install_hint(self):
        def _raise(*_a, **_k):
            raise PotentiostatConnectionError(
                "SquidstatPyLibrary is not installed. "
                "Install with: pip install 'cubos[potentiostat]'"
            )

        with patch(
            "cubos.instruments.potentiostat.vendors.admiral._load_qt_bindings",
            side_effect=_raise,
        ):
            p = AdmiralPotentiostat(port="COM3")
            with pytest.raises(PotentiostatConnectionError, match=r"\[potentiostat\]"):
                p.connect()


class TestConnectOnline:

    def test_connects_and_stores_handler(self):
        bindings, tracker, handler = _make_qt_mock_bindings()
        with patch(
            "cubos.instruments.potentiostat.vendors.admiral._load_qt_bindings",
            return_value=bindings,
        ):
            p = AdmiralPotentiostat(port="COM3")
            p.connect()
        assert p._handler is handler
        assert p._tracker is tracker
        assert p._device_id == "SquidStatMock"
        tracker.connectToDeviceOnComPort.assert_called_once_with("COM3")

    def test_no_device_raises_connection_error(self):
        bindings, tracker, _handler = _make_qt_mock_bindings(
            schedule_device_connected=False,
        )
        with patch(
            "cubos.instruments.potentiostat.vendors.admiral._load_qt_bindings",
            return_value=bindings,
        ):
            p = AdmiralPotentiostat(port="COM3", command_timeout=0.01)
            with pytest.raises(PotentiostatConnectionError, match="No SquidStat device"):
                p.connect()


# --- Online run_CV ------------------------------------------------------------


class TestRunCVOnline:

    def test_collects_samples_into_tuple_arrays(self):
        samples = [
            (0.00, 0.0, 1e-6),
            (0.01, 0.1, 2e-6),
            (0.02, 0.2, 3e-6),
            (0.03, 0.3, 4e-6),
        ]
        bindings, _tracker, handler = _make_qt_mock_bindings(dc_samples=samples)
        with patch(
            "cubos.instruments.potentiostat.vendors.admiral._load_qt_bindings",
            return_value=bindings,
        ):
            p = AdmiralPotentiostat(port="COM3")
            p.connect()
            result = p.run_CV(
                CVParams(0.0, 0.5, -0.5, 0.0, 0.05, cycles=2, sampling_interval_s=0.01)
            )

        assert isinstance(result, CVResult)
        assert result.time_s == (0.00, 0.01, 0.02, 0.03)
        assert result.voltage_v == (0.0, 0.1, 0.2, 0.3)
        assert result.current_a == (1e-6, 2e-6, 3e-6, 4e-6)
        assert result.cycles == 2
        assert result.scan_rate_v_s == 0.05
        assert result.step_size_v == pytest.approx(0.05 * 0.01)
        assert result.vendor == "admiral"
        assert result.metadata["aborted"] is False
        assert result.metadata["channel"] == 0
        handler.uploadExperimentToChannel.assert_called_once()
        handler.startUploadedExperiment.assert_called_once_with(0)

    def test_run_without_connect_raises_command_error(self):
        p = AdmiralPotentiostat()
        with pytest.raises(PotentiostatCommandError, match="not connected"):
            p.run_CV(CVParams(0.0, 0.5, -0.5, 0.0, 0.05))


# --- Online run_OCP / run_CA / run_CP ---------------------------------------
#
# These tests guard against argument-order regressions on the vendor element
# classes (AisOpenCircuitElement / AisConstantPotElement /
# AisConstantCurrentElement) — easy to swap since all three are copy-paste
# similar — and verify each result type picks up only the fields it should.


class TestRunOCPOnline:

    def test_collects_voltage_only(self):
        samples = [(0.0, 0.35, 0.0), (0.1, 0.36, 0.0)]
        bindings, _tracker, handler = _make_qt_mock_bindings(dc_samples=samples)
        with patch(
            "cubos.instruments.potentiostat.vendors.admiral._load_qt_bindings",
            return_value=bindings,
        ):
            p = AdmiralPotentiostat(port="COM3")
            p.connect()
            result = p.run_OCP(OCPParams(duration_s=1.0, sampling_interval_s=0.1))

        assert isinstance(result, OCPResult)
        assert result.time_s == (0.0, 0.1)
        assert result.voltage_v == (0.35, 0.36)
        assert result.duration_s == 1.0
        assert result.sample_period_s == 0.1
        assert result.vendor == "admiral"
        # OCP does not expose current.
        assert not hasattr(result, "current_a")
        # Vendor element built with (duration, sample_period) positional order.
        bindings.squidstat.AisOpenCircuitElement.assert_called_once_with(1.0, 0.1)
        handler.startUploadedExperiment.assert_called_once_with(0)


class TestRunCAOnline:

    def test_collects_voltage_and_current_with_step_potential(self):
        samples = [(0.0, 0.5, 1e-6), (0.01, 0.5, 2e-6)]
        bindings, _tracker, handler = _make_qt_mock_bindings(dc_samples=samples)
        with patch(
            "cubos.instruments.potentiostat.vendors.admiral._load_qt_bindings",
            return_value=bindings,
        ):
            p = AdmiralPotentiostat(port="COM3")
            p.connect()
            result = p.run_CA(
                CAParams(potential_V=0.5, duration_s=1.0, sampling_interval_s=0.01)
            )

        assert isinstance(result, CAResult)
        assert result.current_a == (1e-6, 2e-6)
        assert result.voltage_v == (0.5, 0.5)
        assert result.step_potential_v == 0.5
        # Vendor element order: (potential, interval, duration).
        bindings.squidstat.AisConstantPotElement.assert_called_once_with(
            0.5, 0.01, 1.0,
        )


class TestRunCPOnline:

    def test_collects_voltage_and_current_with_step_current(self):
        samples = [(0.0, 0.1, 1e-3), (0.01, 0.11, 1e-3)]
        bindings, _tracker, handler = _make_qt_mock_bindings(dc_samples=samples)
        with patch(
            "cubos.instruments.potentiostat.vendors.admiral._load_qt_bindings",
            return_value=bindings,
        ):
            p = AdmiralPotentiostat(port="COM3")
            p.connect()
            result = p.run_CP(
                CPParams(current_A=1e-3, duration_s=1.0, sampling_interval_s=0.01)
            )

        assert isinstance(result, CPResult)
        assert result.current_a == (1e-3, 1e-3)
        assert result.voltage_v == (0.1, 0.11)
        assert result.step_current_a == 1e-3
        # Vendor element order: (current, interval, duration).
        bindings.squidstat.AisConstantCurrentElement.assert_called_once_with(
            1e-3, 0.01, 1.0,
        )


# --- Upload / start error-code handling -------------------------------------


class TestVendorErrorCodes:

    def test_upload_returning_truthy_raises_command_error(self):
        bindings, _tracker, handler = _make_qt_mock_bindings()
        handler.uploadExperimentToChannel.return_value = "E_BUSY"
        with patch(
            "cubos.instruments.potentiostat.vendors.admiral._load_qt_bindings",
            return_value=bindings,
        ):
            p = AdmiralPotentiostat(port="COM3")
            p.connect()
            with pytest.raises(PotentiostatCommandError, match="E_BUSY"):
                p.run_OCP(OCPParams(duration_s=1.0))
        # When upload fails, start must NOT be called.
        handler.startUploadedExperiment.assert_not_called()

    def test_start_returning_truthy_raises_command_error(self):
        bindings, _tracker, handler = _make_qt_mock_bindings()
        handler.startUploadedExperiment.return_value = "E_NOT_READY"
        with patch(
            "cubos.instruments.potentiostat.vendors.admiral._load_qt_bindings",
            return_value=bindings,
        ):
            p = AdmiralPotentiostat(port="COM3")
            p.connect()
            with pytest.raises(PotentiostatCommandError, match="E_NOT_READY"):
                p.run_OCP(OCPParams(duration_s=1.0))


# --- Timeout -----------------------------------------------------------------


class TestExperimentTimeout:

    def test_timeout_raises_and_stops_experiment(self):
        bindings, _tracker, handler = _make_qt_mock_bindings(
            schedule_experiment_stopped=False,
        )
        with patch(
            "cubos.instruments.potentiostat.vendors.admiral._load_qt_bindings",
            return_value=bindings,
        ):
            p = AdmiralPotentiostat(port="COM3", command_timeout=0.01)
            p.connect()
            with pytest.raises(PotentiostatTimeoutError, match="timeout"):
                p.run_OCP(OCPParams(duration_s=1.0))
        handler.stopExperiment.assert_called_once_with(0)


# --- Disconnect --------------------------------------------------------------


class TestDisconnect:

    def test_clears_state_and_calls_tracker(self):
        bindings, tracker, _handler = _make_qt_mock_bindings()
        with patch(
            "cubos.instruments.potentiostat.vendors.admiral._load_qt_bindings",
            return_value=bindings,
        ):
            p = AdmiralPotentiostat(port="COM3")
            p.connect()
            p.disconnect()
        tracker.disconnectFromDevice.assert_called_once_with("SquidStatMock")
        assert p._handler is None
        assert p._tracker is None
        assert p._device_id is None
