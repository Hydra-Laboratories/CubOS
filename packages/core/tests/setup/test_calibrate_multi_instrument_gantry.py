"""Offline tests for multi-instrument calibration helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cubos.tools.calibration.multi_instrument_calibration import (
    MultiInstrumentCalibrationResult,
    _retract_up_after_contact,
    _updated_yaml_text,
    compute_non_contact_block_calibration,
    compute_relative_instrument_calibrations,
    run_multi_instrument_calibration,
)


def _write_multi_gantry(path: Path) -> Path:
    path.write_text(
        """\
serial_port: /dev/ttyUSB0
gantry_type: cub_xl
cnc:
  factory_z_travel_mm: 100.0
  calibration_block_height_mm: 12.5
  y_axis_motion: head
working_volume:
  x_min: 0.0
  x_max: 400.0
  y_min: 0.0
  y_max: 300.0
  z_min: 0.0
  z_max: 100.0
grbl_settings:
  status_report: 0
  soft_limits: true
  homing_enable: true
  homing_pull_off: 10.0
  max_travel_x: 400.0
  max_travel_y: 300.0
  max_travel_z: 100.0
instruments:
  left_probe:
    type: asmi
    vendor: vernier
    offset_x: 99.0
    offset_y: 99.0
    depth: 99.0
    offline: true
  camera:
    type: uv_curing
    vendor: excelitas
    offset_x: 1.0
    offset_y: 2.0
    depth: 3.0
    offline: true
""",
        encoding="utf-8",
    )
    return path


def _write_home_origin_multi_gantry(path: Path) -> Path:
    path.write_text(
        """\
serial_port: /dev/ttyUSB0
gantry_type: cub_xl
origin_policy: home_origin
cnc:
  factory_z_travel_mm: 100.0
  calibration_block_height_mm: 12.5
  y_axis_motion: head
working_volume:
  x_min: -400.0
  x_max: 0.0
  y_min: -300.0
  y_max: 0.0
  z_min: -100.0
  z_max: 0.0
grbl_settings:
  status_report: 0
  soft_limits: true
  homing_enable: true
  homing_pull_off: 10.0
  max_travel_x: 400.0
  max_travel_y: 300.0
  max_travel_z: 100.0
instruments:
  left_probe:
    type: asmi
    vendor: vernier
    offset_x: 99.0
    offset_y: 99.0
    depth: 99.0
    offline: true
  camera:
    type: uv_curing
    vendor: excelitas
    offset_x: 1.0
    offset_y: 2.0
    depth: 3.0
    offline: true
""",
        encoding="utf-8",
    )
    return path


def _write_rpi_camera_gantry(path: Path) -> Path:
    path.write_text(
        """\
serial_port: /dev/ttyUSB0
gantry_type: cub_xl
cnc:
  factory_z_travel_mm: 100.0
  calibration_block_height_mm: 12.5
  y_axis_motion: head
working_volume:
  x_min: 0.0
  x_max: 400.0
  y_min: 0.0
  y_max: 300.0
  z_min: 0.0
  z_max: 100.0
grbl_settings:
  status_report: 0
  soft_limits: true
  homing_enable: true
  homing_pull_off: 10.0
  max_travel_x: 400.0
  max_travel_y: 300.0
  max_travel_z: 100.0
instruments:
  left_probe:
    type: asmi
    vendor: vernier
    offset_x: 99.0
    offset_y: 99.0
    depth: 99.0
    offline: true
  pi_camera:
    type: camera
    vendor: raspberry_pi
    offset_x: 1.0
    offset_y: 2.0
    depth: 3.0
    offline: true
""",
        encoding="utf-8",
    )
    return path


def _write_lighting_gantry(path: Path) -> Path:
    path.write_text(
        """\
serial_port: /dev/ttyUSB0
gantry_type: cub_xl
cnc:
  factory_z_travel_mm: 100.0
  calibration_block_height_mm: 12.5
  y_axis_motion: head
working_volume:
  x_min: 0.0
  x_max: 400.0
  y_min: 0.0
  y_max: 300.0
  z_min: 0.0
  z_max: 100.0
grbl_settings:
  status_report: 0
  soft_limits: true
  homing_enable: true
  homing_pull_off: 10.0
  max_travel_x: 400.0
  max_travel_y: 300.0
  max_travel_z: 100.0
instruments:
  left_probe:
    type: asmi
    vendor: vernier
    offset_x: 99.0
    offset_y: 99.0
    depth: 99.0
    offline: true
  pi_camera:
    type: camera
    vendor: raspberry_pi
    offset_x: 1.0
    offset_y: 2.0
    depth: 3.0
    offline: true
  lights:
    type: lighting
    vendor: pawduino
    port: ""
    offset_x: 5.0
    offset_y: 6.0
    depth: 7.0
    offline: true
""",
        encoding="utf-8",
    )
    return path


class _FakeGantry:
    instance: "_FakeGantry"

    def __init__(self, config: dict):
        self.config = config
        self.calls: list[tuple] = []
        self.coords = {"x": 0.0, "y": 0.0, "z": 0.0}
        self.home_count = 0
        self.homing_pull_off = 0.0
        _FakeGantry.instance = self

    def connect(self) -> None:
        self.calls.append(("connect",))

    def disconnect(self) -> None:
        self.calls.append(("disconnect",))

    def home(self) -> None:
        self.calls.append(("home",))
        self.home_count += 1
        if self.home_count == 2:
            self.coords = {"x": 398.0, "y": 299.0, "z": 88.0}
        elif self.home_count >= 3:
            self.coords = {"x": 398.0, "y": 299.0, "z": 96.0}

    def move_to(self, x: float, y: float, z: float, travel_z=None) -> None:
        self.calls.append(("move_to", x, y, z, travel_z))
        self.coords = {"x": x, "y": y, "z": z}

    def clear_g92_offsets(self) -> None:
        self.calls.append(("clear_g92_offsets",))

    def enforce_work_position_reporting(self) -> None:
        self.calls.append(("enforce_work_position_reporting",))

    def activate_work_coordinate_system(self, system: str = "G54") -> None:
        self.calls.append(("activate_work_coordinate_system", system))

    def set_work_coordinates(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
    ) -> None:
        self.calls.append(("set_work_coordinates", x, y, z))
        if x is not None:
            self.coords["x"] = x
        if y is not None:
            self.coords["y"] = y
        if z is not None:
            self.coords["z"] = z

    def get_coordinates(self) -> dict[str, float]:
        self.calls.append(("get_coordinates",))
        return dict(self.coords)

    def jog(
        self,
        x: float = 0,
        y: float = 0,
        z: float = 0,
        feed_rate: float = 2000,
    ) -> None:
        self.calls.append(("jog", x, y, z, feed_rate))
        self.coords = {
            "x": self.coords["x"] + x,
            "y": self.coords["y"] + y,
            "z": self.coords["z"] + z,
        }

    def jog_cancel(self) -> None:
        self.calls.append(("jog_cancel",))

    def stop(self) -> None:
        self.calls.append(("stop",))

    def unlock(self) -> None:
        self.calls.append(("unlock",))

    def set_serial_timeout(self, timeout: float) -> None:
        self.calls.append(("set_serial_timeout", timeout))

    def set_grbl_setting(self, setting: str, value: float) -> None:
        self.calls.append(("set_grbl_setting", setting, value))
        if setting in {"$27", "27"}:
            self.homing_pull_off = float(value)

    def homing_pull_off_mm(self) -> float:
        self.calls.append(("homing_pull_off_mm",))
        return self.homing_pull_off

    def soft_limits_enabled(self) -> bool | None:
        self.calls.append(("soft_limits_enabled",))
        return False

    def set_soft_limits_enabled(self, enabled: bool) -> None:
        self.calls.append(("set_soft_limits_enabled", enabled))

    def configure_soft_limits_from_spans(
        self,
        *,
        max_travel_x: float,
        max_travel_y: float,
        max_travel_z: float,
        status_report: float | int | None = None,
        homing_pull_off: float | None = None,
        hard_limits: float | int | bool | None = None,
        tolerance_mm: float = 0.001,
    ) -> None:
        self.calls.append(
            (
                "configure_soft_limits_from_spans",
                max_travel_x,
                max_travel_y,
                max_travel_z,
                status_report,
                homing_pull_off,
                hard_limits,
                tolerance_mm,
            )
        )


class _SerialDropOnFirstHomeFakeGantry(_FakeGantry):
    def __init__(self, config: dict):
        super().__init__(config)
        self.failed_first_home = False

    def home(self) -> None:
        if not self.failed_first_home:
            self.failed_first_home = True
            self.calls.append(("home_failed_device_not_configured",))
            raise OSError("Could not configure port: (6, 'Device not configured')")
        super().home()


class _SoftLimitEnabledFakeGantry(_FakeGantry):
    def __init__(self, config: dict):
        super().__init__(config)
        self.soft_limits_are_enabled = True
        self.hard_limits_are_enabled = False

    def soft_limits_enabled(self) -> bool | None:
        self.calls.append(("soft_limits_enabled",))
        return self.soft_limits_are_enabled

    def set_soft_limits_enabled(self, enabled: bool) -> None:
        self.calls.append(("set_soft_limits_enabled", enabled))
        self.soft_limits_are_enabled = enabled

    def hard_limits_enabled(self) -> bool | None:
        self.calls.append(("hard_limits_enabled",))
        return self.hard_limits_are_enabled

    def set_hard_limits_enabled(self, enabled: bool) -> None:
        self.calls.append(("set_hard_limits_enabled", enabled))
        self.hard_limits_are_enabled = enabled


class _SoftLimitProgrammingFailsFakeGantry(_FakeGantry):
    def configure_soft_limits_from_spans(self, **kwargs) -> None:
        self.calls.append(("configure_soft_limits_from_spans_failed", kwargs))
        raise RuntimeError("GRBL soft-limit settings did not verify")


class _IdleTrackingFakeGantry(_FakeGantry):
    def get_status(self) -> str:
        self.calls.append(("get_status",))
        return "Idle"


def _key_reader(keys):
    iterator = iter(keys)

    def read():
        return next(iterator)

    return read


def test_retract_waits_for_idle_before_returning():
    gantry = _FakeGantry(config={})
    statuses = iter(["Jog", "Run", "Idle"])
    gantry.get_status = lambda: next(statuses)  # type: ignore[attr-defined]

    _retract_up_after_contact(
        gantry,
        retract_z_mm=15.0,
        feed_rate=2000.0,
        output=lambda _message: None,
    )

    assert gantry.calls == [("jog", 0, 0, 15.0, 2000.0)]


def test_compute_relative_instrument_calibrations_uses_shared_block_point():
    calibrations = compute_relative_instrument_calibrations(
        block_coordinates={
            "left_probe": {"x": 90.0, "y": 120.0, "z": 30.0},
            "camera": {"x": 82.0, "y": 117.0, "z": 36.0},
        },
        reference_instrument="left_probe",
        lowest_instrument="left_probe",
    )

    assert calibrations["left_probe"] == {
        "offset_x": 0.0,
        "offset_y": 0.0,
        "depth": 0.0,
    }
    assert calibrations["camera"] == {
        "offset_x": 8.0,
        "offset_y": 3.0,
        "depth": 6.0,
    }


def test_compute_non_contact_block_calibration_uses_centered_camera_pose():
    calibration = compute_non_contact_block_calibration(
        block_reference_coordinates={"x": 199.0, "y": 149.5, "z": 12.5},
        non_contact_coordinates={"x": 211.0, "y": 153.5, "z": 35.5},
        block_height_mm=12.5,
        height_above_block_mm=20.0,
    )

    assert calibration == {
        "offset_x": -12.0,
        "offset_y": -4.0,
        "depth": 3.0,
    }


def test_compute_non_contact_block_calibration_rejects_negative_distance():
    with pytest.raises(ValueError, match="must be >= 0"):
        compute_non_contact_block_calibration(
            block_reference_coordinates={"x": 199.0, "y": 149.5, "z": 12.5},
            non_contact_coordinates={"x": 211.0, "y": 153.5, "z": 35.5},
            block_height_mm=12.5,
            height_above_block_mm=-1.0,
        )


def test_dry_run_prompts_for_only_operator_choices(tmp_path):
    path = _write_multi_gantry(tmp_path / "gantry.yaml")
    inputs = iter(["", "1", "y"])
    prompts: list[str] = []
    messages: list[str] = []

    def input_reader(prompt: str) -> str:
        prompts.append(prompt)
        return next(inputs)

    result = run_multi_instrument_calibration(
        path,
        dry_run=True,
        output=messages.append,
        input_reader=input_reader,
    )

    assert result is None
    assert prompts == [
        "Pick the number for the first/left-most tool for front-left origin: ",
        "Pick the number for the first/left-most tool for front-left origin: ",
        "You selected #1 left_probe. Continue? [y/N]: ",
    ]
    assert any("Pick which numbered tool" in message for message in messages)
    assert any("Available instruments" in message for message in messages)
    assert any("  1. left_probe (asmi)" in message for message in messages)
    assert any("  2. camera (uv_curing)" in message for message in messages)
    assert any("Dry run only" in message for message in messages)


def test_dry_run_home_origin_prompt_label_matches_preflight_wording(tmp_path):
    path = _write_home_origin_multi_gantry(tmp_path / "gantry.yaml")
    inputs = iter(["1", "y"])
    prompts: list[str] = []
    messages: list[str] = []

    def input_reader(prompt: str) -> str:
        prompts.append(prompt)
        return next(inputs)

    result = run_multi_instrument_calibration(
        path,
        dry_run=True,
        output=messages.append,
        input_reader=input_reader,
    )

    assert result is None
    assert prompts == [
        "Pick the number for the first/left-most tool for back-right-top origin: ",
        "You selected #1 left_probe. Continue? [y/N]: ",
    ]
    assert any(
        "place the origin block/artifact at the back-right-top corner" in message
        for message in messages
    )
    assert any(
        "place an origin block/artifact at the back-right-top corner" in message
        for message in messages
    )
    assert not any("front-left" in message for message in messages)


def test_multi_instrument_calibration_reconnects_once_if_serial_drops_during_home(tmp_path):
    path = _write_multi_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []

    result = run_multi_instrument_calibration(
        path,
        reference_instrument="left_probe",
        lowest_instrument="left_probe",
        instruments_to_calibrate=("left_probe",),
        skip_soft_limit_config=True,
        output=messages.append,
        input_reader=lambda _prompt: "12.5",
        gantry_factory=_SerialDropOnFirstHomeFakeGantry,
            key_reader=_key_reader(
                [
                    ("\r", 1),
                    ("Z", 1),
                    ("\r", 1),
                    ("\r", 1),
                ]
        ),
        stdin_flusher=lambda: None,
    )

    assert isinstance(result, MultiInstrumentCalibrationResult)
    calls = _SerialDropOnFirstHomeFakeGantry.instance.calls
    assert ("home_failed_device_not_configured",) in calls
    assert calls.count(("connect",)) == 2
    assert ("disconnect",) in calls
    assert any("Reconnecting once" in message for message in messages)


def test_multi_instrument_calibration_disables_stale_soft_limits_during_jogs(tmp_path):
    path = _write_multi_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []

    result = run_multi_instrument_calibration(
        path,
        reference_instrument="left_probe",
        lowest_instrument="left_probe",
        instruments_to_calibrate=("left_probe",),
        skip_soft_limit_config=True,
        output=messages.append,
        input_reader=lambda _prompt: "12.5",
        gantry_factory=_SoftLimitEnabledFakeGantry,
            key_reader=_key_reader(
                [
                    ("\r", 1),
                    ("Z", 1),
                    ("\r", 1),
                    ("\r", 1),
                ]
        ),
        stdin_flusher=lambda: None,
    )

    assert isinstance(result, MultiInstrumentCalibrationResult)
    calls = _SoftLimitEnabledFakeGantry.instance.calls
    disable_call = ("set_soft_limits_enabled", False)
    restore_call = ("set_soft_limits_enabled", True)
    assert disable_call in calls
    assert restore_call in calls
    assert calls.index(disable_call) < calls.index(
        ("set_work_coordinates", 0.0, 0.0, None)
    )
    assert calls.index(restore_call) < calls.index(
        ("home",),
        calls.index(("set_work_coordinates", 0.0, 0.0, None)),
    )
    assert any("Temporarily disabling GRBL soft limits" in m for m in messages)
    # Hard limits are enforced for the same window and reverted after the
    # soft limits come back on.
    enable_hard = ("set_hard_limits_enabled", True)
    revert_hard = ("set_hard_limits_enabled", False)
    assert enable_hard in calls
    assert revert_hard in calls
    assert calls.index(enable_hard) < calls.index(revert_hard)
    assert calls.index(restore_call) < calls.index(revert_hard)
    assert any("Enabling GRBL hard limits" in m for m in messages)


def test_multi_outer_finally_reverts_hard_limits_when_soft_restore_fails(tmp_path):
    from cubos.gantry.gantry_driver.exceptions import MillConnectionError

    path = _write_multi_gantry(tmp_path / "gantry.yaml")

    class SoftRestoreFailsFakeGantry(_SoftLimitEnabledFakeGantry):
        def set_soft_limits_enabled(self, enabled: bool) -> None:
            super().set_soft_limits_enabled(enabled)
            if enabled:
                raise MillConnectionError("link died during soft-limit restore")

    with pytest.raises(MillConnectionError):
        run_multi_instrument_calibration(
            path,
            reference_instrument="left_probe",
            lowest_instrument="left_probe",
            instruments_to_calibrate=("left_probe",),
            skip_soft_limit_config=True,
            output=lambda _message: None,
            input_reader=lambda _prompt: "12.5",
            gantry_factory=SoftRestoreFailsFakeGantry,
            key_reader=_key_reader(
                [
                    ("\r", 1),
                    ("Z", 1),
                    ("\r", 1),
                    ("\r", 1),
                ]
            ),
            stdin_flusher=lambda: None,
        )

    # The inline cleanup died at the soft-limit restore; the outer safety
    # net must still drop $21 back to its prior state.
    calls = SoftRestoreFailsFakeGantry.instance.calls
    assert ("set_hard_limits_enabled", False) in calls


def test_multi_instrument_calibration_accepts_block_height_for_z_reference(tmp_path):
    path = _write_multi_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []
    inputs = iter(["12.5"])

    result = run_multi_instrument_calibration(
        path,
        reference_instrument="left_probe",
        lowest_instrument="left_probe",
        instruments_to_calibrate=("left_probe",),
        skip_soft_limit_config=True,
        output=messages.append,
        input_reader=lambda _prompt: next(inputs),
        gantry_factory=_FakeGantry,
        key_reader=_key_reader(
            [
                ("\r", 1),
                ("Z", 1),
                ("\r", 1),
                ("RIGHT", 1),
                ("\r", 1),
            ]
        ),
        stdin_flusher=lambda: None,
    )

    assert isinstance(result, MultiInstrumentCalibrationResult)
    assert result.z_origin_verification == (199.0, 149.5, 12.5)
    assert ("set_work_coordinates", None, None, 12.5) in _FakeGantry.instance.calls
    assert any("calibration block" in message.lower() for message in messages)


def test_multi_instrument_calibration_prompts_for_lowest_instrument_when_omitted(tmp_path):
    path = _write_multi_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []
    inputs = iter(["12.5", "1", "y"])

    result = run_multi_instrument_calibration(
        path,
        reference_instrument="left_probe",
        instruments_to_calibrate=("left_probe",),
        skip_soft_limit_config=True,
        output=messages.append,
        input_reader=lambda _prompt: next(inputs),
        gantry_factory=_FakeGantry,
        key_reader=_key_reader(
            [
                ("\r", 1),
                ("Z", 1),
                ("\r", 1),
                ("RIGHT", 1),
                ("\r", 1),
            ]
        ),
        stdin_flusher=lambda: None,
    )

    assert isinstance(result, MultiInstrumentCalibrationResult)
    assert result.lowest_instrument == "left_probe"
    assert any(
        "Lowest mounted tool will be selected after all instruments are attached." in message
        for message in messages
    )


def test_multi_instrument_calibration_sets_xy_before_z_and_updates_yaml(tmp_path):
    path = _write_multi_gantry(tmp_path / "gantry.yaml")
    out_path = tmp_path / "calibrated.yaml"
    messages: list[str] = []
    inputs = iter(["12.5", "y"])

    result = run_multi_instrument_calibration(
        path,
        reference_instrument="left_probe",
        lowest_instrument="left_probe",
        instruments_to_calibrate=("left_probe", "camera"),
        skip_soft_limit_config=False,
        output_gantry_path=out_path,
        write_gantry_yaml=True,
        output=messages.append,
        input_reader=lambda _prompt: next(inputs),
        gantry_factory=_FakeGantry,
        key_reader=_key_reader(
            [
                ("LEFT", 2),
                ("DOWN", 1),
                ("\r", 1),  # confirm XY origin: only X/Y are zeroed
                ("Z", 3),
                ("\r", 1),  # confirm lowest instrument shared Z/block point
                ("RIGHT", 15),
                ("UP", 7),
                ("Z", 9),
                ("\r", 1),  # camera shared block point
            ]
        ),
        stdin_flusher=lambda: None,
    )

    assert isinstance(result, MultiInstrumentCalibrationResult)
    assert result.xy_origin_verification == (0.0, 0.0, 0.0)
    assert result.z_origin_verification == (199.0, 149.5, 12.5)
    assert result.measured_working_volume == (398.0, 299.0, 96.0)
    assert result.grbl_max_travel == (408.0, 309.0, 106.0)
    assert result.instrument_calibrations["left_probe"] == {
        "offset_x": 0.0,
        "offset_y": 0.0,
        "depth": 0.0,
    }
    assert result.instrument_calibrations["camera"] == {
        "offset_x": -15.0,
        "offset_y": -7.0,
        "depth": 6.0,
    }

    set_wpos_calls = [
        call for call in _FakeGantry.instance.calls if call[0] == "set_work_coordinates"
    ]
    assert set_wpos_calls[0] == ("set_work_coordinates", 0.0, 0.0, None)
    assert set_wpos_calls[1] == ("set_work_coordinates", None, None, 12.5)

    move_calls = [call for call in _FakeGantry.instance.calls if call[0] == "move_to"]
    assert move_calls == [("move_to", 199.0, 149.5, 88.0, None)]
    retract_calls = [
        call for call in _FakeGantry.instance.calls
        if call == ("jog", 0, 0, 15.0, 2000.0)
    ]
    assert len(retract_calls) == 2

    written = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert written["working_volume"] == {
        "x_min": 0.0,
        "x_max": 398.0,
        "y_min": 0.0,
        "y_max": 299.0,
        "z_min": 0.0,
        "z_max": 96.0,
    }
    assert written["cnc"]["factory_z_travel_mm"] == 100.0
    assert written["cnc"]["calibration_block_height_mm"] == 12.5
    assert written["grbl_settings"]["homing_pull_off"] == 10.0
    assert written["grbl_settings"]["max_travel_x"] == 408.0
    assert written["grbl_settings"]["max_travel_y"] == 309.0
    assert written["grbl_settings"]["max_travel_z"] == 106.0
    assert "measurement_height" not in written["instruments"]["camera"]
    assert written["instruments"]["camera"]["offset_x"] == -15.0
    assert written["instruments"]["camera"]["offset_y"] == -7.0
    assert written["instruments"]["camera"]["depth"] == 6.0


def test_multi_instrument_calibration_writes_yaml_before_soft_limit_programming_failure(tmp_path):
    path = _write_multi_gantry(tmp_path / "gantry.yaml")
    out_path = tmp_path / "calibrated.yaml"
    messages: list[str] = []

    with pytest.raises(RuntimeError, match="soft-limit settings did not verify"):
        run_multi_instrument_calibration(
            path,
            reference_instrument="left_probe",
            lowest_instrument="left_probe",
            instruments_to_calibrate=("left_probe", "camera"),
            output_gantry_path=out_path,
            write_gantry_yaml=True,
            output=messages.append,
            input_reader=lambda _prompt: "12.5",
            gantry_factory=_SoftLimitProgrammingFailsFakeGantry,
            key_reader=_key_reader(
                [
                    ("LEFT", 2),
                    ("DOWN", 1),
                    ("\r", 1),
                    ("Z", 3),
                    ("\r", 1),
                    ("RIGHT", 15),
                    ("UP", 7),
                    ("Z", 9),
                    ("\r", 1),
                ]
            ),
            stdin_flusher=lambda: None,
        )

    assert out_path.exists()
    written = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert written["grbl_settings"]["max_travel_z"] == 106.0
    assert written["instruments"]["camera"]["offset_x"] == -15.0
    output_text = "\n".join(messages)
    assert "Full calibrated multi-instrument gantry YAML to copy/paste:" in output_text
    assert f"was written to: {out_path}" in output_text
    assert any(
        call[0] == "configure_soft_limits_from_spans_failed"
        for call in _SoftLimitProgrammingFailsFakeGantry.instance.calls
    )


def test_multi_instrument_calibration_records_rpi_camera_over_block(tmp_path):
    path = _write_rpi_camera_gantry(tmp_path / "gantry.yaml")
    out_path = tmp_path / "calibrated.yaml"
    messages: list[str] = []
    inputs = iter(["12.5", "20.0", "y"])

    def read_input(prompt: str) -> str:
        if "Distance from calibration block top" in prompt:
            assert any(
                "Non-contact pose recorded for pi_camera" in message
                for message in messages
            )
        return next(inputs)

    result = run_multi_instrument_calibration(
        path,
        reference_instrument="left_probe",
        lowest_instrument="left_probe",
        instruments_to_calibrate=("left_probe", "pi_camera"),
        skip_soft_limit_config=False,
        output_gantry_path=out_path,
        write_gantry_yaml=True,
        output=messages.append,
        input_reader=read_input,
        gantry_factory=_FakeGantry,
        key_reader=_key_reader(
            [
                ("\r", 1),  # XY origin
                ("Z", 3),
                ("\r", 1),  # lowest instrument shared block point
                ("RIGHT", 12),
                ("UP", 4),
                ("Z", 8),
                ("\r", 1),  # camera centered over block
            ]
        ),
        stdin_flusher=lambda: None,
    )

    assert isinstance(result, MultiInstrumentCalibrationResult)
    assert result.instrument_calibrations["pi_camera"] == {
        "offset_x": -12.0,
        "offset_y": -4.0,
        "depth": -13.0,
    }
    assert any("non-contact instrument" in message for message in messages)
    assert any(
        "Measure the height from the calibration block top to pi_camera" in message
        for message in messages
    )
    assert any("distance from block=20.000 mm" in message for message in messages)

    written = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert written["instruments"]["pi_camera"]["offset_x"] == -12.0
    assert written["instruments"]["pi_camera"]["offset_y"] == -4.0
    assert written["instruments"]["pi_camera"]["depth"] == -13.0


def test_multi_instrument_calibration_rejects_rpi_camera_as_lowest(tmp_path):
    path = _write_rpi_camera_gantry(tmp_path / "gantry.yaml")

    with pytest.raises(ValueError, match="cannot be non-contact"):
        run_multi_instrument_calibration(
            path,
            reference_instrument="left_probe",
            lowest_instrument="pi_camera",
            instruments_to_calibrate=("left_probe", "pi_camera"),
            dry_run=True,
            output=lambda _message: None,
            input_reader=lambda _prompt: "y",
        )


def test_multi_instrument_calibration_lighting_reuses_camera_calibration(tmp_path):
    path = _write_lighting_gantry(tmp_path / "gantry.yaml")
    out_path = tmp_path / "calibrated.yaml"
    messages: list[str] = []
    inputs = iter(["12.5", "20.0", "y"])

    result = run_multi_instrument_calibration(
        path,
        reference_instrument="left_probe",
        lowest_instrument="left_probe",
        instruments_to_calibrate=("left_probe", "pi_camera", "lights"),
        skip_soft_limit_config=False,
        output_gantry_path=out_path,
        write_gantry_yaml=True,
        output=messages.append,
        input_reader=lambda _prompt: next(inputs),
        gantry_factory=_FakeGantry,
        key_reader=_key_reader(
            [
                ("\r", 1),  # XY origin
                ("Z", 3),
                ("\r", 1),  # lowest instrument shared block point
                ("RIGHT", 12),
                ("UP", 4),
                ("Z", 8),
                ("\r", 1),  # camera centered over block; no lights step follows
            ]
        ),
        stdin_flusher=lambda: None,
    )

    assert isinstance(result, MultiInstrumentCalibrationResult)
    assert result.instrument_calibrations["lights"] == (
        result.instrument_calibrations["pi_camera"]
    )
    assert "lights" not in result.block_reference_coordinates
    output_text = "\n".join(messages)
    assert "lights reuses the camera calibration from pi_camera." in output_text
    assert "calibrate lights" not in output_text
    assert "calibrate non-contact instrument 'lights'" not in output_text

    written = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    camera = written["instruments"]["pi_camera"]
    lights = written["instruments"]["lights"]
    assert lights["offset_x"] == camera["offset_x"]
    assert lights["offset_y"] == camera["offset_y"]
    assert lights["depth"] == camera["depth"]


def test_multi_instrument_calibration_lighting_without_camera_keeps_offsets(tmp_path):
    path = _write_lighting_gantry(tmp_path / "gantry.yaml")
    raw = path.read_text(encoding="utf-8")
    start = raw.index("  pi_camera:")
    end = raw.index("  lights:")
    path.write_text(raw[:start] + raw[end:], encoding="utf-8")
    out_path = tmp_path / "calibrated.yaml"
    messages: list[str] = []
    inputs = iter(["12.5", "y"])

    result = run_multi_instrument_calibration(
        path,
        reference_instrument="left_probe",
        lowest_instrument="left_probe",
        instruments_to_calibrate=("left_probe", "lights"),
        skip_soft_limit_config=False,
        output_gantry_path=out_path,
        write_gantry_yaml=True,
        output=messages.append,
        input_reader=lambda _prompt: next(inputs),
        gantry_factory=_FakeGantry,
        key_reader=_key_reader(
            [
                ("\r", 1),  # XY origin
                ("Z", 3),
                ("\r", 1),  # lowest instrument shared block point; no more steps
            ]
        ),
        stdin_flusher=lambda: None,
    )

    assert isinstance(result, MultiInstrumentCalibrationResult)
    assert "lights" not in result.instrument_calibrations
    assert any(
        "Skipping lights: no calibrated camera to copy from" in message
        for message in messages
    )
    written = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert written["instruments"]["lights"]["offset_x"] == 5.0
    assert written["instruments"]["lights"]["offset_y"] == 6.0
    assert written["instruments"]["lights"]["depth"] == 7.0


def test_multi_instrument_calibration_rejects_lighting_as_reference(tmp_path):
    path = _write_lighting_gantry(tmp_path / "gantry.yaml")

    with pytest.raises(ValueError, match="follows the camera"):
        run_multi_instrument_calibration(
            path,
            reference_instrument="lights",
            lowest_instrument="left_probe",
            instruments_to_calibrate=("left_probe", "pi_camera", "lights"),
            dry_run=True,
            output=lambda _message: None,
            input_reader=lambda _prompt: "y",
        )


def test_multi_instrument_calibration_prompts_for_missing_block_height_and_writes_it(tmp_path):
    path = _write_multi_gantry(tmp_path / "gantry.yaml")
    raw = path.read_text(encoding="utf-8")
    path.write_text(
        raw.replace("  calibration_block_height_mm: 12.5\n", ""),
        encoding="utf-8",
    )
    out_path = tmp_path / "calibrated.yaml"
    prompts: list[str] = []

    def input_reader(prompt: str) -> str:
        prompts.append(prompt)
        return "18.75"

    result = run_multi_instrument_calibration(
        path,
        reference_instrument="left_probe",
        lowest_instrument="left_probe",
        instruments_to_calibrate=("left_probe",),
        skip_soft_limit_config=True,
        output_gantry_path=out_path,
        write_gantry_yaml=True,
        output=lambda _message: None,
        input_reader=input_reader,
        gantry_factory=_FakeGantry,
        key_reader=_key_reader(
            [
                ("\r", 1),
                ("Z", 1),
                ("\r", 1),
                ("\r", 1),
            ]
        ),
        stdin_flusher=lambda: None,
    )

    assert isinstance(result, MultiInstrumentCalibrationResult)
    assert result.z_origin_verification == (199.0, 149.5, 18.75)
    assert "Calibration block height in mm: " in prompts
    written = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert written["cnc"]["calibration_block_height_mm"] == 18.75


def test_multi_instrument_calibration_uses_multi_specific_block_prompt(tmp_path):
    path = _write_multi_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []

    run_multi_instrument_calibration(
        path,
        reference_instrument="left_probe",
        lowest_instrument="left_probe",
        instruments_to_calibrate=("left_probe",),
        skip_soft_limit_config=True,
        output=messages.append,
        input_reader=lambda _prompt: "12.5",
        gantry_factory=_FakeGantry,
        key_reader=_key_reader([("\r", 1), ("Z", 1), ("\r", 1), ("\r", 1)]),
        stdin_flusher=lambda: None,
    )

    output_text = "\n".join(messages)
    assert "use a calibration block" in output_text
    assert "every instrument can reach" in output_text
    assert "top of a well plate" not in output_text


def test_multi_instrument_calibration_q_abort_cancels_jog_before_disconnect(tmp_path):
    path = _write_multi_gantry(tmp_path / "gantry.yaml")

    with pytest.raises(KeyboardInterrupt):
        run_multi_instrument_calibration(
            path,
            reference_instrument="left_probe",
            lowest_instrument="left_probe",
            instruments_to_calibrate=("left_probe",),
            skip_soft_limit_config=True,
            output=lambda _message: None,
            input_reader=lambda _prompt: "12.5",
            gantry_factory=_FakeGantry,
            key_reader=_key_reader([("Q", 1)]),
            stdin_flusher=lambda: None,
        )

    calls = _FakeGantry.instance.calls
    assert ("jog_cancel",) in calls
    assert calls.index(("jog_cancel",)) < calls.index(("disconnect",))


def test_multi_instrument_calibration_keyboard_interrupt_cancels_jog_before_disconnect(tmp_path):
    path = _write_multi_gantry(tmp_path / "gantry.yaml")

    def interrupting_key_reader():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_multi_instrument_calibration(
            path,
            reference_instrument="left_probe",
            lowest_instrument="left_probe",
            instruments_to_calibrate=("left_probe",),
            skip_soft_limit_config=True,
            output=lambda _message: None,
            input_reader=lambda _prompt: "12.5",
            gantry_factory=_FakeGantry,
            key_reader=interrupting_key_reader,
            stdin_flusher=lambda: None,
        )

    calls = _FakeGantry.instance.calls
    assert ("jog_cancel",) in calls
    assert calls.index(("jog_cancel",)) < calls.index(("disconnect",))


def test_multi_instrument_calibration_waits_for_idle_before_recorded_reads(tmp_path):
    path = _write_multi_gantry(tmp_path / "gantry.yaml")

    run_multi_instrument_calibration(
        path,
        reference_instrument="left_probe",
        lowest_instrument="left_probe",
        instruments_to_calibrate=("left_probe", "camera"),
        skip_soft_limit_config=True,
        output=lambda _message: None,
        input_reader=lambda _prompt: "12.5",
        gantry_factory=_IdleTrackingFakeGantry,
        key_reader=_key_reader(
            [
                ("RIGHT", 1),
                ("\r", 1),
                ("Z", 1),
                ("\r", 1),
                ("LEFT", 1),
                ("\r", 1),
            ]
        ),
        stdin_flusher=lambda: None,
    )

    calls = _IdleTrackingFakeGantry.instance.calls
    for index, call in enumerate(calls):
        if call == ("get_coordinates",):
            assert index > 0
            assert calls[index - 1] == ("get_status",)


def test_multi_instrument_calibration_flushes_before_each_jog_session_and_after_automated_moves(tmp_path):
    path = _write_multi_gantry(tmp_path / "gantry.yaml")
    flushes: list[str] = []

    run_multi_instrument_calibration(
        path,
        reference_instrument="left_probe",
        lowest_instrument="left_probe",
        instruments_to_calibrate=("left_probe", "camera"),
        skip_soft_limit_config=True,
        output=lambda _message: None,
        input_reader=lambda _prompt: "12.5",
        gantry_factory=_FakeGantry,
        key_reader=_key_reader(
            [
                ("\r", 1),
                ("Z", 1),
                ("\r", 1),
                ("RIGHT", 1),
                ("\r", 1),
            ]
        ),
        stdin_flusher=lambda: flushes.append("flush"),
    )

    assert len(flushes) >= 5


def test_updated_multi_instrument_yaml_writes_block_height_and_requires_cnc_mapping():
    raw_config = {
        "cnc": {"factory_z_travel_mm": 100.0},
        "instruments": {"left_probe": {"type": "asmi"}},
        "grbl_settings": {"status_report": 0},
    }

    yaml_text = _updated_yaml_text(
        raw_config,
        measured_coords={"x": 398.0, "y": 299.0},
        instrument_calibrations={
            "left_probe": {"offset_x": 0.0, "offset_y": 0.0, "depth": 0.0}
        },
        max_travel={"max_travel_x": 408.0, "max_travel_y": 309.0, "max_travel_z": 106.0},
        z_min_mm=0.0,
        z_max_mm=96.0,
        calibration_block_height_mm=18.75,
    )

    written = yaml.safe_load(yaml_text)
    assert written["cnc"]["calibration_block_height_mm"] == 18.75

    with pytest.raises(ValueError, match="cnc section must be a mapping"):
        _updated_yaml_text(
            {"cnc": "bad", "instruments": {}, "grbl_settings": {}},
            measured_coords={"x": 398.0, "y": 299.0},
            instrument_calibrations={},
            max_travel={
                "max_travel_x": 408.0,
                "max_travel_y": 309.0,
                "max_travel_z": 106.0,
            },
            z_min_mm=0.0,
            z_max_mm=96.0,
            calibration_block_height_mm=18.75,
        )


def test_multi_instrument_calibration_raises_if_cnc_factory_z_travel_mm_missing(tmp_path):
    path = tmp_path / "gantry.yaml"
    path.write_text(
        """\
serial_port: /dev/ttyUSB0
gantry_type: cub_xl
cnc:
  y_axis_motion: head
working_volume:
  x_min: 0.0
  x_max: 400.0
  y_min: 0.0
  y_max: 300.0
  z_min: 0.0
  z_max: 100.0
instruments:
  left_probe:
    type: asmi
    vendor: vernier
    offset_x: 0.0
    offset_y: 0.0
    depth: 0.0
    offline: true
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cnc.factory_z_travel_mm"):
        run_multi_instrument_calibration(path, dry_run=True)
