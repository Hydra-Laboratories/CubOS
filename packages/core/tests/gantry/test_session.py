"""Tests for the persistent CubOS gantry session."""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path

import pytest

from cubos.gantry.session import (
    CalibrationBlockedError,
    GantryAlarmError,
    GantryNotConnectedError,
    GantrySession,
    GantrySessionError,
    GantrySessionHealthCheckError,
    InterruptFeedHoldTimeoutError,
    MovementOutOfBoundsError,
)
from cubos.gantry.errors import MillConnectionError


GANTRY_YAML = """\
serial_port: /dev/test
gantry_type: cub_xl
cnc:
  factory_z_travel_mm: 90.0
working_volume:
  x_min: 0.0
  x_max: 300.0
  y_min: 0.0
  y_max: 200.0
  z_min: 0.0
  z_max: 80.0
grbl_settings:
  status_report: 0
  soft_limits: true
  hard_limits: false
  homing_enable: true
  homing_pull_off: 10.0
  max_travel_x: 310.0
  max_travel_y: 210.0
  max_travel_z: 90.0
instruments: {}
"""


class FakeGantry:
    instances: list["FakeGantry"] = []

    def __init__(self, config=None):
        self.config = config or {}
        self.connected = False
        self.coords = {"x": 10.0, "y": 20.0, "z": 30.0}
        self.status = "Idle"
        self.calls: list[tuple[str, object]] = []
        self.disconnect_calls = 0
        self.connect_should_fail = False
        self.grbl_settings = {
            "$10": "0",
            "$20": "1",
            "$21": "0",
            "$22": "1",
            "$27": "10",
            "$130": "310",
            "$131": "210",
            "$132": "90",
        }
        FakeGantry.instances.append(self)

    def connect(self, port=None):
        self.calls.append(("connect", port))
        if self.connect_should_fail:
            raise RuntimeError("connect failed")
        self.connected = True

    def disconnect(self):
        self.disconnect_calls += 1
        self.calls.append(("disconnect", None))
        self.connected = False

    def get_position_info(self):
        return {"coords": self.coords, "work_pos": self.coords, "status": self.status}

    def _extract_status(self):
        return self.status

    def read_grbl_settings(self):
        self.calls.append(("read_grbl_settings", None))
        return dict(self.grbl_settings)

    def is_healthy(self):
        self.calls.append(("is_healthy", None))
        return True

    def prepare_for_protocol_run(self):
        self.calls.append(("prepare_for_protocol_run", None))

    def connect_instruments(self):
        self.calls.append(("connect_instruments", None))

    def disconnect_instruments(self):
        self.calls.append(("disconnect_instruments", None))

    def home(self):
        self.calls.append(("home", None))

    def unlock(self):
        self.calls.append(("unlock", None))

    def reset_and_unlock(self):
        self.calls.append(("reset_and_unlock", None))

    def resume(self):
        self.calls.append(("resume", None))

    def enforce_work_position_reporting(self):
        self.calls.append(("enforce_work_position_reporting", None))

    def activate_work_coordinate_system(self, system):
        self.calls.append(("activate_work_coordinate_system", system))

    def clear_g92_offsets(self):
        self.calls.append(("clear_g92_offsets", None))

    def soft_limits_enabled(self):
        self.calls.append(("soft_limits_enabled", None))
        return True

    def set_soft_limits_enabled(self, enabled):
        self.calls.append(("set_soft_limits_enabled", enabled))

    def hard_limits_enabled(self):
        self.calls.append(("hard_limits_enabled", None))
        return float(self.grbl_settings.get("$21", "0")) != 0.0

    def set_hard_limits_enabled(self, enabled):
        self.calls.append(("set_hard_limits_enabled", enabled))
        self.grbl_settings["$21"] = "1" if enabled else "0"

    def configure_soft_limits_from_spans(self, **kwargs):
        self.calls.append(("configure_soft_limits_from_spans", kwargs))

    def set_grbl_setting(self, setting, value):
        self.calls.append(("set_grbl_setting", (setting, value)))

    def move_to(self, *args, **kwargs):
        self.calls.append(("move_to", args or kwargs))
        if args:
            x, y, z = args
        else:
            x, y, z = kwargs["x"], kwargs["y"], kwargs["z"]
        self.coords = {"x": float(x), "y": float(y), "z": float(z)}

    def jog(self, **kwargs):
        self.calls.append(("jog", kwargs))
        self.coords = {
            "x": self.coords["x"] + float(kwargs.get("x", 0.0)),
            "y": self.coords["y"] + float(kwargs.get("y", 0.0)),
            "z": self.coords["z"] + float(kwargs.get("z", 0.0)),
        }

    def get_status(self):
        self.calls.append(("get_status", None))
        return self.status

    def stop(self):
        self.calls.append(("stop", None))

    def feed_hold_realtime(self):
        self.calls.append(("feed_hold_realtime", None))

    def jog_cancel(self):
        self.calls.append(("jog_cancel", None))

    def set_work_coordinates(self, **kwargs):
        self.calls.append(("set_work_coordinates", kwargs))

    def finalize_deck_origin_calibration(self, **kwargs):
        self.calls.append(("finalize_deck_origin_calibration", kwargs))
        return {
            "measured_volume": {"x": 300.0, "y": 200.0, "z": 80.0},
            "z_calibration": {"block_height": 10.0},
            "max_travel": {"x": 310.0, "y": 210.0, "z": 90.0},
            "position": {"x": 300.0, "y": 200.0, "z": 80.0},
            "homing_pull_off_mm": 10.0,
        }


def _write_gantry(path: Path) -> Path:
    gantry_path = path / "gantry.yaml"
    gantry_path.write_text(GANTRY_YAML, encoding="utf-8")
    return gantry_path


@pytest.fixture(autouse=True)
def _clear_fake_gantry_instances():
    FakeGantry.instances.clear()
    yield
    FakeGantry.instances.clear()


def test_connect_stages_and_publishes_after_success(tmp_path):
    observations: list[tuple[str, bool]] = []
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)

    class ObservingGantry(FakeGantry):
        def connect(self, port=None):
            observations.append(("session_connected_during_connect", session.connected))
            observations.append(("lock_held", session.operation_lock.locked()))
            super().connect(port=port)

    session = GantrySession(gantry_factory=ObservingGantry, sleep=lambda _seconds: None)
    snapshot = session.connect(_write_gantry(tmp_path), filename="gantry.yaml")

    assert snapshot.connected is True
    assert observations == [
        ("session_connected_during_connect", False),
        ("lock_held", True),
    ]
    assert session.connected is True
    assert ObservingGantry.instances[-1].calls[0] == ("connect", "/dev/test")


def test_connect_failure_preserves_existing_session(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    existing = FakeGantry.instances[-1]

    class FailingGantry(FakeGantry):
        def connect(self, port=None):
            raise RuntimeError("serial failure")

    session._gantry_factory = FailingGantry

    with pytest.raises(RuntimeError, match="serial failure"):
        session.connect(_write_gantry(tmp_path), filename="gantry.yaml")

    assert existing.disconnect_calls == 1
    assert session.connected is False
    assert session.operation_lock.locked() is False


def test_double_connect_disconnects_previous_gantry(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    first = FakeGantry.instances[-1]

    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    second = FakeGantry.instances[-1]

    assert first.disconnect_calls == 1
    assert second is not first
    assert session._gantry is second


def test_disconnect_without_connection_returns_disconnected_snapshot():
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)

    snapshot = session.disconnect()

    assert snapshot.connected is False
    assert snapshot.status == "Disconnected"


def test_disconnect_reports_restore_and_disconnect_errors(tmp_path):
    class BadDisconnectGantry(FakeGantry):
        def soft_limits_enabled(self):
            return False

        def disconnect(self):
            super().disconnect()
            raise RuntimeError("close failed")

    session = GantrySession(gantry_factory=BadDisconnectGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    session._calibration_restore_soft_limits = True

    with pytest.raises(GantrySessionError, match="Soft-limit restore and disconnect both failed"):
        session.disconnect()

    assert session.connected is False


def test_refresh_connected_config_only_for_matching_filename(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    fake = FakeGantry.instances[-1]

    session.refresh_connected_config("other.yaml", {"serial_port": "/dev/ignored"})
    assert fake.config["serial_port"] == "/dev/test"

    session.refresh_connected_config(
        "gantry.yaml",
        {"serial_port": "/dev/new", "grbl_settings": {"status_report": 0}},
    )

    assert session._connected_gantry_config["serial_port"] == "/dev/new"
    assert fake.config == {"serial_port": "/dev/new"}


def test_connect_handshake_failure_disconnects_staged_gantry(tmp_path):
    class PositionFailingGantry(FakeGantry):
        def get_position_info(self):
            raise RuntimeError("position read failed")

    session = GantrySession(
        gantry_factory=PositionFailingGantry,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(RuntimeError, match="position read failed"):
        session.connect(_write_gantry(tmp_path), filename="gantry.yaml")

    staged = PositionFailingGantry.instances[-1]
    assert staged.disconnect_calls == 1
    assert session.connected is False
    assert session.operation_lock.locked() is False


def test_position_returns_cached_status_while_operation_lock_is_held(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    FakeGantry.instances[-1].status = "Run"
    session.operation_lock.acquire()
    try:
        snapshot = session.position()
    finally:
        session.operation_lock.release()

    assert snapshot.x == 10.0
    assert snapshot.status == "Run"


def test_position_uses_controller_observed_cache_while_operation_lock_is_held(tmp_path):
    class ObservedCacheGantry(FakeGantry):
        def get_cached_position_info(self):
            return {
                "coords": {"x": 42.5, "y": 18.0, "z": 7.25},
                "work_pos": {"x": 42.5, "y": 18.0, "z": 7.25},
                "status": "Run",
            }

    session = GantrySession(gantry_factory=ObservedCacheGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    session.operation_lock.acquire()
    try:
        snapshot = session.position()
    finally:
        session.operation_lock.release()

    assert (snapshot.x, snapshot.y, snapshot.z) == (42.5, 18.0, 7.25)
    assert snapshot.status == "Run"


def test_position_falls_back_when_controller_cache_read_fails(tmp_path):
    class CacheFailingGantry(FakeGantry):
        def get_cached_position_info(self):
            raise RuntimeError("cache unavailable")

    session = GantrySession(gantry_factory=CacheFailingGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    fake = CacheFailingGantry.instances[-1]
    fake.status = "Run"
    session.operation_lock.acquire()
    try:
        snapshot = session.position()
    finally:
        session.operation_lock.release()

    assert snapshot.x == 10.0
    assert snapshot.status == "Run"


def test_position_propagates_connection_error_from_controller_cache(tmp_path):
    error = MillConnectionError("serial port disappeared")

    class CacheDisconnectingGantry(FakeGantry):
        def get_cached_position_info(self):
            raise error

    session = GantrySession(
        gantry_factory=CacheDisconnectingGantry,
        sleep=lambda _seconds: None,
    )
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    session.operation_lock.acquire()
    try:
        with pytest.raises(MillConnectionError) as exc_info:
            session.position()
    finally:
        session.operation_lock.release()

    assert exc_info.value is error


def test_position_uses_alarm_status_when_read_raises_alarm(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    FakeGantry.instances[-1].get_position_info = lambda: (_ for _ in ()).throw(
        RuntimeError("ALARM:1 hard limit")
    )

    snapshot = session.position()

    assert snapshot.status == "ALARM:1"


def test_simple_locked_wrappers_call_gantry_and_return_position(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    fake = FakeGantry.instances[-1]

    assert session.home().connected
    assert session.unlock().connected
    assert session.reset_and_unlock().connected
    assert session.resume().connected
    assert session.feed_hold().connected
    assert session.jog_cancel().connected
    assert session.set_work_coordinates(x=1.0, y=None, z=2.0).connected

    assert ("home", None) in fake.calls
    assert ("unlock", None) in fake.calls
    assert ("reset_and_unlock", None) in fake.calls
    assert ("resume", None) in fake.calls
    assert ("stop", None) in fake.calls
    assert ("jog_cancel", None) in fake.calls
    assert ("set_work_coordinates", {"x": 1.0, "y": None, "z": 2.0}) in fake.calls


def test_grbl_setting_helpers_normalize_and_parse_values(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    fake = FakeGantry.instances[-1]

    settings = session.set_grbl_setting("20", "0")

    assert ("set_grbl_setting", ("$20", 0.0)) in fake.calls
    assert settings["$20"] == "1"
    assert session.read_grbl_settings()["$10"] == "0"

    for bad_setting in ("abc", "$x", "20.5"):
        with pytest.raises(ValueError, match="numeric code"):
            session.set_grbl_setting(bad_setting, "1")
    for bad_value in ("", "1\n2", "abc"):
        with pytest.raises(ValueError):
            session.set_grbl_setting("$20", bad_value)


def test_move_and_jog_reject_invalid_targets(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")

    with pytest.raises(MovementOutOfBoundsError, match="outside"):
        session.move_to_blocking(x=301.0, y=20.0, z=30.0)

    with pytest.raises(ValueError, match="finite"):
        session.jog(x=0.0, y=0.0, z=math.nan)


def test_movement_error_marks_reconnect_required_for_missing_working_volume(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    session._connected_gantry_config.pop("working_volume")

    with pytest.raises(MovementOutOfBoundsError) as exc_info:
        session.move_to_blocking(x=10.0, y=20.0, z=30.0)

    assert exc_info.value.requires_reconnect is True


def test_movement_error_marks_reconnect_required_for_missing_current_position(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    FakeGantry.instances[-1].get_position_info = lambda: {
        "coords": None,
        "work_pos": None,
        "status": "Idle",
    }

    with pytest.raises(MovementOutOfBoundsError) as exc_info:
        session.jog(x=1.0)

    assert exc_info.value.requires_reconnect is True


def test_movement_error_does_not_mark_reconnect_required_for_bounds_violation(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")

    with pytest.raises(MovementOutOfBoundsError) as exc_info:
        session.jog(x=500.0)

    assert exc_info.value.requires_reconnect is False


def test_move_worker_publishes_and_resets_errors(tmp_path):
    class OneFailureGantry(FakeGantry):
        def __init__(self, config=None):
            super().__init__(config=config)
            self.fail_next = True

        def move_to(self, *args, **kwargs):
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("out of bounds")
            return super().move_to(*args, **kwargs)

    session = GantrySession(gantry_factory=OneFailureGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")

    session._move_worker(1.0, 2.0, 3.0)
    assert session.position().move_error == "out of bounds"

    session._move_worker(4.0, 5.0, 6.0)
    snapshot = session.position()
    assert snapshot.move_error is None
    assert (snapshot.x, snapshot.y, snapshot.z) == (4.0, 5.0, 6.0)


def test_move_to_background_rejects_out_of_bounds_before_thread(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")

    with pytest.raises(MovementOutOfBoundsError):
        session.move_to(x=-1.0, y=0.0, z=0.0)


def test_calibration_active_reflects_calibration_flags(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    assert session.calibration_active is False

    session.prepare_calibration_origin()
    assert session.calibration_active is True

    session.restore_calibration_soft_limits()
    assert session.calibration_active is False


def test_calibration_prepare_disables_and_restore_reenables_soft_limits(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    fake = FakeGantry.instances[-1]

    session.prepare_calibration_origin()
    session.restore_calibration_soft_limits()

    assert ("set_grbl_setting", ("$10", 0.0)) in fake.calls
    assert ("set_grbl_setting", ("$27", 10.0)) in fake.calls
    assert ("home", None) in fake.calls
    assert ("set_soft_limits_enabled", False) in fake.calls
    assert ("set_soft_limits_enabled", True) in fake.calls


def test_calibration_prepare_enforces_hard_limits_and_restore_reverts(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    fake = FakeGantry.instances[-1]
    assert fake.grbl_settings["$21"] == "0"

    session.prepare_calibration_origin()
    assert ("set_hard_limits_enabled", True) in fake.calls
    assert fake.grbl_settings["$21"] == "1"
    assert session.calibration_active

    session.restore_calibration_soft_limits()
    assert ("set_hard_limits_enabled", False) in fake.calls
    assert fake.grbl_settings["$21"] == "0"
    assert not session.calibration_active


def test_calibration_prepare_leaves_hard_limits_alone_when_already_on(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    fake = FakeGantry.instances[-1]
    fake.grbl_settings["$21"] = "1"

    session.prepare_calibration_origin()
    session.restore_calibration_soft_limits()

    # $21 was already enabled: never written, and never reverted.
    assert not any(name == "set_hard_limits_enabled" for name, _ in fake.calls)
    assert fake.grbl_settings["$21"] == "1"


def test_configure_soft_limits_reverts_calibration_hard_limits(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    fake = FakeGantry.instances[-1]

    session.prepare_calibration_origin()
    assert fake.grbl_settings["$21"] == "1"
    session.configure_soft_limits(
        max_travel_x=310.0,
        max_travel_y=210.0,
        max_travel_z=90.0,
    )

    # The fixture YAML sets hard_limits: false, so the manual span-programming
    # path also reverts the calibration-window enforcement.
    assert ("set_hard_limits_enabled", False) in fake.calls
    assert fake.grbl_settings["$21"] == "0"


def test_hard_limit_restore_failure_warns_and_clears_flag(tmp_path):
    class FailingHardRestoreGantry(FakeGantry):
        def set_hard_limits_enabled(self, enabled):
            super().set_hard_limits_enabled(enabled)
            if not enabled:
                raise RuntimeError("serial died mid-restore")

    session = GantrySession(
        gantry_factory=FailingHardRestoreGantry, sleep=lambda _seconds: None
    )
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")

    session.prepare_calibration_origin()
    # A failed $21 revert must not raise — the machine is left in the safer
    # state — and must not leave the flag set (no retry loop).
    session.restore_calibration_soft_limits()
    assert session._calibration_restore_hard_limits is False
    assert not session.calibration_active


def test_finalize_reverts_calibration_hard_limits_unless_configured(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    fake = FakeGantry.instances[-1]

    session.prepare_calibration_origin()
    assert fake.grbl_settings["$21"] == "1"
    session.finalize_calibration_origin(
        home_z=80.0,
        block_touch_z=10.0,
        block_height=5.0,
        factory_z_travel=90.0,
    )

    # The fixture YAML sets hard_limits: false, so the calibration-window
    # enforcement is reverted once calibrated soft limits are in place.
    assert ("set_hard_limits_enabled", False) in fake.calls
    assert fake.grbl_settings["$21"] == "0"
    assert not session.calibration_active


def test_finalize_keeps_hard_limits_when_yaml_configures_them(tmp_path):
    gantry_path = tmp_path / "gantry.yaml"
    gantry_path.write_text(
        GANTRY_YAML.replace("hard_limits: false", "hard_limits: true"),
        encoding="utf-8",
    )
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(gantry_path, filename="gantry.yaml")
    fake = FakeGantry.instances[-1]

    session.prepare_calibration_origin()
    session.finalize_calibration_origin(
        home_z=80.0,
        block_touch_z=10.0,
        block_height=5.0,
        factory_z_travel=90.0,
    )

    assert ("set_hard_limits_enabled", True) in fake.calls
    assert ("set_hard_limits_enabled", False) not in fake.calls
    assert fake.grbl_settings["$21"] == "1"
    assert not session.calibration_active


def test_feed_hold_interrupt_does_not_wait_for_operation_lock(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    fake = FakeGantry.instances[-1]
    session.operation_lock.acquire()
    try:
        session.feed_hold_interrupt()
    finally:
        session.operation_lock.release()

    assert ("feed_hold_realtime", None) in fake.calls
    assert ("stop", None) not in fake.calls


def test_feed_hold_interrupt_reports_timeout(tmp_path):
    class TimeoutGantry(FakeGantry):
        def feed_hold_realtime(self):
            raise RuntimeError("Error executing command !: Command execution timed out")

    session = GantrySession(gantry_factory=TimeoutGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")

    with pytest.raises(InterruptFeedHoldTimeoutError):
        session.feed_hold_interrupt()


def test_interrupt_helpers_require_connection():
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)

    with pytest.raises(GantryNotConnectedError):
        session.feed_hold_interrupt()
    with pytest.raises(GantryNotConnectedError):
        session.jog_cancel_interrupt()


def test_jog_queued_behind_cancel_is_dropped(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    fake = FakeGantry.instances[-1]

    jog_done = threading.Event()

    def queued_jog():
        session.jog(x=1.0)
        jog_done.set()

    # Hold the operation lock so the jog request blocks after snapshotting
    # its cancel generation — the same interleaving as a jog request queued
    # behind a held-jog burst on the API server when the cancel arrives.
    session._lock.acquire()
    try:
        worker = threading.Thread(target=queued_jog, daemon=True)
        worker.start()
        time.sleep(0.1)
        session.jog_cancel_interrupt()
    finally:
        session._lock.release()
    assert jog_done.wait(timeout=2.0)

    assert ("jog_cancel", None) in fake.calls
    assert not any(name == "jog" for name, _ in fake.calls)

    # A fresh jog issued after the cancel proceeds normally.
    session.jog(x=1.0)
    assert any(name == "jog" for name, _ in fake.calls)


def test_locked_jog_cancel_also_drops_queued_jogs(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    fake = FakeGantry.instances[-1]

    generation = session._jog_cancel_generation
    session.jog_cancel()
    assert session._jog_cancel_generation == generation + 1
    assert ("jog_cancel", None) in fake.calls


def test_jog_zero_delta_is_noop_and_alarm_is_wrapped(tmp_path):
    class AlarmJogGantry(FakeGantry):
        def jog(self, **kwargs):
            raise RuntimeError("ALARM:1")

    session = GantrySession(gantry_factory=AlarmJogGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")

    session.jog()
    with pytest.raises(GantryAlarmError):
        session.jog(x=1.0)


def test_jog_blocking_idle_alarm_and_timeout_paths(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    fake = FakeGantry.instances[-1]

    assert session.jog_blocking(x=1.0, timeout_s=0.1).connected

    fake.status = "<Alarm|WPos:0,0,0>"
    with pytest.raises(GantryAlarmError):
        session.jog_blocking(x=1.0, timeout_s=0.1)

    fake.status = "<Run|WPos:0,0,0>"
    with pytest.raises(GantrySessionError, match="Timed out"):
        session._wait_until_idle_locked(timeout_s=0.0)


def test_finalize_calibration_failure_restores_soft_limits_and_keeps_flag_if_unverified(tmp_path):
    class FailingFinalizeGantry(FakeGantry):
        def __init__(self, config=None):
            super().__init__(config=config)
            self.soft_limits_verified = False
            self.soft_limit_reads = 0

        def finalize_deck_origin_calibration(self, **kwargs):
            self.calls.append(("finalize_deck_origin_calibration", kwargs))
            raise RuntimeError("calibration failed")

        def soft_limits_enabled(self):
            self.calls.append(("soft_limits_enabled", None))
            self.soft_limit_reads += 1
            if self.soft_limit_reads == 1:
                return True
            return self.soft_limits_verified

    session = GantrySession(gantry_factory=FailingFinalizeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    fake = FailingFinalizeGantry.instances[-1]
    session.prepare_calibration_origin()

    with pytest.raises(RuntimeError, match="calibration failed"):
        session.finalize_calibration_origin(
            home_z=80.0,
            block_touch_z=10.0,
            block_height=5.0,
            factory_z_travel=90.0,
        )

    assert ("set_soft_limits_enabled", True) in fake.calls
    assert ("set_soft_limits_enabled", False) in fake.calls
    assert session._calibration_restore_soft_limits is True


def test_run_protocol_uses_existing_gantry_and_preserves_connection(monkeypatch, tmp_path):
    import cubos.gantry.session as session_module

    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    fake = FakeGantry.instances[-1]
    events: list[str] = []

    class FakeStore:
        def __init__(self, db_path=None):
            self.db_path = db_path
            events.append(f"store:{db_path}")

        def close(self):
            events.append("store.close")

    class FakeInstrumented:
        def connect_instruments(self):
            events.append("cubos.instruments.connect")

        def disconnect_instruments(self):
            events.append("cubos.instruments.disconnect")

    class FakeProtocol:
        def execute(self, context):
            events.append("protocol.execute")
            assert context.gantry is instrumented
            return ["a", "b"]

    instrumented = FakeInstrumented()
    context = type("Context", (), {"gantry": instrumented})()

    def fake_create_campaign(data_store, **kwargs):
        events.append("campaign.create")
        assert data_store.db_path == tmp_path / "data.db"
        return 77

    def fake_setup_protocol(*args, **kwargs):
        events.append("setup_protocol")
        assert kwargs["gantry"] is fake
        assert kwargs["campaign_id"] == 77
        return FakeProtocol(), context

    monkeypatch.setattr(session_module, "DataStore", FakeStore)
    monkeypatch.setattr(session_module, "create_campaign_for_protocol_run", fake_create_campaign)
    monkeypatch.setattr(session_module, "setup_protocol", fake_setup_protocol)

    result = session.run_protocol(
        gantry_path=tmp_path / "gantry.yaml",
        deck_path=tmp_path / "deck.yaml",
        protocol_path=tmp_path / "protocol.yaml",
        gantry_file="gantry.yaml",
        deck_file="deck.yaml",
        protocol_file="protocol.yaml",
        db_path=tmp_path / "data.db",
    )

    assert result.campaign_id == 77
    assert result.steps_executed == 2
    assert ("prepare_for_protocol_run", None) in fake.calls
    assert fake.disconnect_calls == 0
    assert events == [
        f"store:{tmp_path / 'data.db'}",
        "campaign.create",
        "setup_protocol",
        "cubos.instruments.connect",
        "protocol.execute",
        "cubos.instruments.disconnect",
        "store.close",
    ]


def test_run_protocol_links_fluid_state_to_campaign_and_context(monkeypatch, tmp_path):
    """The fluid state must reach BOTH the campaign record (tip/cap state
    journals resolve it through the campaign) and setup_protocol (context
    tracking) — missing either silently breaks durable tracking."""
    import cubos.gantry.session as session_module

    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")

    seen = {}

    class FakeStore:
        def __init__(self, db_path=None):
            self.db_path = db_path

        def close(self):
            pass

    class FakeInstrumented:
        def connect_instruments(self):
            pass

        def disconnect_instruments(self):
            pass

    class FakeProtocol:
        def execute(self, context):
            return []

    context = type("Context", (), {"gantry": FakeInstrumented()})()

    def fake_create_campaign(data_store, **kwargs):
        seen["campaign_fluid_state_id"] = kwargs.get("fluid_state_id")
        return 78

    def fake_setup_protocol(*args, **kwargs):
        seen["setup_fluid_state_id"] = kwargs.get("fluid_state_id")
        return FakeProtocol(), context

    monkeypatch.setattr(session_module, "DataStore", FakeStore)
    monkeypatch.setattr(session_module, "create_campaign_for_protocol_run", fake_create_campaign)
    monkeypatch.setattr(session_module, "setup_protocol", fake_setup_protocol)

    session.run_protocol(
        gantry_path=tmp_path / "gantry.yaml",
        deck_path=tmp_path / "deck.yaml",
        protocol_path=tmp_path / "protocol.yaml",
        gantry_file="gantry.yaml",
        deck_file="deck.yaml",
        protocol_file="protocol.yaml",
        db_path=tmp_path / "data.db",
        fluid_state_id=3,
    )

    assert seen["campaign_fluid_state_id"] == 3
    assert seen["setup_fluid_state_id"] == 3


def test_run_protocol_does_not_block_on_calibration_warning(tmp_path):
    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    session._calibration_warning = "settings differ"

    # A GRBL-settings mismatch is advisory, not blocking: run_protocol must get
    # past the calibration gate. It still fails here on the missing deck/protocol
    # fixtures, but never with CalibrationBlockedError.
    with pytest.raises(Exception) as excinfo:
        session.run_protocol(
            gantry_path=tmp_path / "gantry.yaml",
            deck_path=tmp_path / "deck.yaml",
            protocol_path=tmp_path / "protocol.yaml",
            gantry_file="gantry.yaml",
            deck_file="deck.yaml",
            protocol_file="protocol.yaml",
        )
    assert not isinstance(excinfo.value, CalibrationBlockedError)


def test_run_protocol_blocks_initial_unhealthy_gantry(tmp_path):
    class UnhealthyGantry(FakeGantry):
        def is_healthy(self):
            return False

    session = GantrySession(gantry_factory=UnhealthyGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")

    with pytest.raises(GantrySessionHealthCheckError, match="not connected"):
        session.run_protocol(
            gantry_path=tmp_path / "gantry.yaml",
            deck_path=tmp_path / "deck.yaml",
            protocol_path=tmp_path / "protocol.yaml",
            gantry_file="gantry.yaml",
            deck_file="deck.yaml",
            protocol_file="protocol.yaml",
        )


def test_run_protocol_disconnects_instruments_on_failure(monkeypatch, tmp_path):
    import cubos.gantry.session as session_module

    session = GantrySession(gantry_factory=FakeGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    events: list[str] = []

    class FakeStore:
        def __init__(self, db_path=None):
            pass

        def close(self):
            events.append("store.close")

    class FakeInstrumented:
        def connect_instruments(self):
            events.append("cubos.instruments.connect")

        def disconnect_instruments(self):
            events.append("cubos.instruments.disconnect")

    class FailingProtocol:
        def execute(self, context):
            raise RuntimeError("boom")

    context = type("Context", (), {"gantry": FakeInstrumented()})()
    monkeypatch.setattr(session_module, "DataStore", FakeStore)
    monkeypatch.setattr(
        session_module,
        "create_campaign_for_protocol_run",
        lambda *_args, **_kwargs: 88,
    )
    monkeypatch.setattr(
        session_module,
        "setup_protocol",
        lambda *_args, **_kwargs: (FailingProtocol(), context),
    )

    with pytest.raises(RuntimeError, match="boom"):
        session.run_protocol(
            gantry_path=tmp_path / "gantry.yaml",
            deck_path=tmp_path / "deck.yaml",
            protocol_path=tmp_path / "protocol.yaml",
            gantry_file="gantry.yaml",
            deck_file="deck.yaml",
            protocol_file="protocol.yaml",
            db_path=tmp_path / "data.db",
        )

    assert events == ["cubos.instruments.connect", "cubos.instruments.disconnect", "store.close"]


def test_run_protocol_health_failure_after_instruments_uses_session_error(
    monkeypatch,
    tmp_path,
):
    import cubos.gantry.session as session_module

    class HealthDropGantry(FakeGantry):
        def __init__(self, config=None):
            super().__init__(config=config)
            self.health_checks = 0

        def is_healthy(self):
            self.health_checks += 1
            return self.health_checks == 1

    session = GantrySession(gantry_factory=HealthDropGantry, sleep=lambda _seconds: None)
    session.connect(_write_gantry(tmp_path), filename="gantry.yaml")
    events: list[str] = []

    class FakeStore:
        def __init__(self, db_path=None):
            pass

        def close(self):
            events.append("store.close")

    class FakeInstrumented:
        def connect_instruments(self):
            events.append("cubos.instruments.connect")

        def disconnect_instruments(self):
            events.append("cubos.instruments.disconnect")

    class UnreachedProtocol:
        def execute(self, context):
            raise AssertionError("protocol should not execute")

    context = type("Context", (), {"gantry": FakeInstrumented()})()
    monkeypatch.setattr(session_module, "DataStore", FakeStore)
    monkeypatch.setattr(
        session_module,
        "create_campaign_for_protocol_run",
        lambda *_args, **_kwargs: 88,
    )
    monkeypatch.setattr(
        session_module,
        "setup_protocol",
        lambda *_args, **_kwargs: (UnreachedProtocol(), context),
    )

    with pytest.raises(GantrySessionHealthCheckError, match="health check failed"):
        session.run_protocol(
            gantry_path=tmp_path / "gantry.yaml",
            deck_path=tmp_path / "deck.yaml",
            protocol_path=tmp_path / "protocol.yaml",
            gantry_file="gantry.yaml",
            deck_file="deck.yaml",
            protocol_file="protocol.yaml",
            db_path=tmp_path / "data.db",
        )

    assert events == ["cubos.instruments.connect", "cubos.instruments.disconnect", "store.close"]
