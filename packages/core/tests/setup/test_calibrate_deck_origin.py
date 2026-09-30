"""Offline tests for single-instrument calibration helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from cubos.gantry.errors import (
    CommandExecutionError,
    StatusReturnError,
)
from cubos.gantry.gantry_driver.exceptions import MillConnectionError
from cubos.tools.calibration.single_instrument_calibration import (
    DeckOriginCalibrationResult,
    _calculate_block_z_calibration,
    _calibration_block_height_mm,
    _factory_z_travel_mm,
    _read_hard_limits_enabled_if_available,
    _restore_hard_limits_after_origin_jog,
    _set_hard_limits_enabled_if_available,
    _temporarily_enable_hard_limits_for_origin_jog,
    _updated_gantry_yaml_text,
    run_calibration,
)


def _write_gantry(path: Path, *, x_min: float = 0.0) -> Path:
    path.write_text(
        f"""\
serial_port: /dev/ttyUSB0
gantry_type: cub_xl
cnc:
  factory_z_travel_mm: 100.0
  y_axis_motion: head
  safe_z: 85.0
working_volume:
  x_min: {x_min}
  x_max: 400.0
  y_min: 0.0
  y_max: 300.0
  z_min: 0.0
  z_max: 100.0
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
            self.coords = {"x": 398.5, "y": 299.25, "z": 96.75}

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
        return self.coords

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

    def reset_and_unlock(self) -> None:
        self.calls.append(("reset_and_unlock",))

    def set_serial_timeout(self, timeout: float) -> None:
        self.calls.append(("set_serial_timeout", timeout))

    def set_grbl_setting(self, setting: str, value: float) -> None:
        self.calls.append(("set_grbl_setting", setting, value))
        if setting in {"$27", "27"}:
            self.homing_pull_off = float(value)

    def homing_pull_off_mm(self) -> float:
        self.calls.append(("homing_pull_off_mm",))
        return self.homing_pull_off

    def set_expected_grbl_settings(
        self,
        settings: dict[str, float] | None,
        *,
        source: str = "gantry",
    ) -> None:
        self.calls.append(("set_expected_grbl_settings", settings, source))

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


class _LimitRecoveringFakeGantry(_FakeGantry):
    def __init__(self, config: dict):
        super().__init__(config)
        self.fail_next_jog = True
        self.fail_next_recovery_readback = False

    def jog(
        self,
        x: float = 0,
        y: float = 0,
        z: float = 0,
        feed_rate: float = 2000,
    ) -> None:
        if self.fail_next_jog:
            self.fail_next_jog = False
            self.calls.append(("jog", x, y, z, feed_rate))
            raise CommandExecutionError("Alarm in status: <Alarm|WPos:0,0,0|Pn:Y>")
        super().jog(x=x, y=y, z=z, feed_rate=feed_rate)

    def get_coordinates(self) -> dict[str, float]:
        if self.fail_next_recovery_readback:
            self.fail_next_recovery_readback = False
            self.calls.append(("get_coordinates_failed",))
            raise StatusReturnError("WPos readback unavailable")
        return super().get_coordinates()


class _LimitRecoveringNoReadbackFakeGantry(_LimitRecoveringFakeGantry):
    def __init__(self, config: dict):
        super().__init__(config)
        self.fail_next_recovery_readback = True


class _SoftLimitAwareFakeGantry(_FakeGantry):
    def __init__(self, config: dict):
        super().__init__(config)
        self.soft_limits_are_enabled = True

    def soft_limits_enabled(self) -> bool | None:
        self.calls.append(("soft_limits_enabled",))
        return self.soft_limits_are_enabled

    def set_soft_limits_enabled(self, enabled: bool) -> None:
        self.calls.append(("set_soft_limits_enabled", enabled))
        self.soft_limits_are_enabled = enabled


class _BothLimitsAwareFakeGantry(_SoftLimitAwareFakeGantry):
    def __init__(self, config: dict):
        super().__init__(config)
        self.hard_limits_are_enabled = False

    def hard_limits_enabled(self) -> bool | None:
        self.calls.append(("hard_limits_enabled",))
        return self.hard_limits_are_enabled

    def set_hard_limits_enabled(self, enabled: bool) -> None:
        self.calls.append(("set_hard_limits_enabled", enabled))
        self.hard_limits_are_enabled = enabled


class _SoftLimitRejectingFakeGantry(_FakeGantry):
    def __init__(self, config: dict):
        super().__init__(config)
        self.fail_next_jog = True

    def jog(
        self,
        x: float = 0,
        y: float = 0,
        z: float = 0,
        feed_rate: float = 2000,
    ) -> None:
        if self.fail_next_jog:
            self.fail_next_jog = False
            self.calls.append(("jog", x, y, z, feed_rate))
            raise CommandExecutionError("Jog failed: error:15")
        super().jog(x=x, y=y, z=z, feed_rate=feed_rate)


class _SoftLimitAwareFailingJogFakeGantry(_SoftLimitAwareFakeGantry):
    def jog(
        self,
        x: float = 0,
        y: float = 0,
        z: float = 0,
        feed_rate: float = 2000,
    ) -> None:
        self.calls.append(("jog", x, y, z, feed_rate))
        raise CommandExecutionError("Jog failed: unexpected controller error")


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


def test_run_calibration_sets_xy_then_z_and_measures_home(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []

    result = run_calibration(
        path,
        output=messages.append,
        gantry_factory=_FakeGantry,
        key_reader=_key_reader(
            [
                ("LEFT", 1),
                ("DOWN", 2),
                ("Z", 3),
                ("\r", 1),
            ]
        ),
        stdin_flusher=lambda: None,
    )

    assert isinstance(result, DeckOriginCalibrationResult)
    assert result.xy_origin_verification == (0.0, 0.0, -3.0)
    assert result.z_reference_verification == (0.0, 0.0, 0.0)
    assert result.z_min_mm == 0.0
    assert result.factory_z_travel_mm == 100.0
    assert result.reachable_z_min_mm == 0.0
    assert result.measured_working_volume == (398.5, 299.25, 96.75)
    assert result.grbl_max_travel == (398.5, 299.25, 96.75)
    assert result.plan.origin_wpos == (0.0, 0.0, 0.0)
    assert _FakeGantry.instance.calls == [
        ("connect",),
        ("set_grbl_setting", "$10", 0),
        ("set_serial_timeout", 10.0),
        ("home",),
        ("set_serial_timeout", 1.0),
        ("enforce_work_position_reporting",),
        ("activate_work_coordinate_system", "G54"),
        ("clear_g92_offsets",),
        ("jog", -1.0, 0.0, 0.0, 2500.0),
        ("get_coordinates",),
        ("jog", 0.0, -2.0, 0.0, 2500.0),
        ("get_coordinates",),
        ("jog", 0.0, 0.0, -3.0, 2500.0),
        ("get_coordinates",),
        ("get_coordinates",),
        ("set_work_coordinates", 0.0, 0.0, None),
        ("get_coordinates",),
        ("set_work_coordinates", None, None, 0.0),
        ("get_coordinates",),
        ("set_serial_timeout", 10.0),
        ("home",),
        ("set_serial_timeout", 1.0),
        ("get_coordinates",),
        ("homing_pull_off_mm",),
        ("configure_soft_limits_from_spans", 398.5, 299.25, 96.75, 0, 0.0, None, 0.25),
        ("jog_cancel",),
        ("set_serial_timeout", 0.05),
        ("disconnect",),
    ]
    assert any("WPos Z=0" in message for message in messages)
    assert any("Z reference point after XY origining" in message for message in messages)
    assert any("Calibrated working volume" in message for message in messages)


def test_run_calibration_block_mode_uses_home_to_block_travel(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []

    result = run_calibration(
        path,
        output=messages.append,
        gantry_factory=_FakeGantry,
        key_reader=_key_reader(
            [
                ("Z", 50),
                ("\r", 1),
            ]
        ),
        stdin_flusher=lambda: None,
        tip_gap_mm=35.0,
        z_reference_mode="block",
    )

    assert isinstance(result, DeckOriginCalibrationResult)
    assert result.xy_origin_verification == (0.0, 0.0, -50.0)
    assert result.z_reference_verification == (0.0, 0.0, 35.0)
    assert result.z_min_mm == 0.0
    assert result.factory_z_travel_mm == 100.0
    assert result.reachable_z_min_mm == 0.0
    assert result.block_height_mm == 35.0
    assert result.block_touch_wpos_z_mm == -50.0
    assert result.home_to_block_travel_mm == 50.0
    assert result.can_reach_deck_bottom is True
    assert result.measured_working_volume == (398.5, 299.25, 96.75)
    assert result.grbl_max_travel == (398.5, 299.25, 96.75)
    assert ("set_work_coordinates", None, None, 35.0) in _FakeGantry.instance.calls
    assert any("block_height: 35.000" in message for message in messages)
    assert any("block_touch_wpos_z: -50.000" in message for message in messages)
    assert any(
        "home_to_block_travel: 50.000" in message
        for message in messages
    )


def test_run_calibration_block_mode_prompts_for_missing_block_height_and_writes_it(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    output_path = tmp_path / "written_gantry.yaml"
    prompts: list[str] = []

    def input_reader(prompt: str) -> str:
        prompts.append(prompt)
        return "35.0"

    result = run_calibration(
        path,
        output=lambda _message: None,
        input_reader=input_reader,
        gantry_factory=_FakeGantry,
        key_reader=_key_reader(
            [
                ("Z", 50),
                ("\r", 1),
            ]
        ),
        stdin_flusher=lambda: None,
        z_reference_mode="block",
        write_gantry_yaml=True,
        output_gantry_path=output_path,
    )

    assert isinstance(result, DeckOriginCalibrationResult)
    assert result.block_height_mm == 35.0
    assert "Reference height above the deck in mm: " in prompts
    written = output_path.read_text(encoding="utf-8")
    assert "calibration_block_height_mm: 35.0" in written


def test_run_calibration_reprompts_when_block_height_exceeds_factory_travel(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    prompts: list[str] = []
    messages: list[str] = []
    responses = iter(["500.0", "35.0"])

    def input_reader(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    result = run_calibration(
        path,
        output=messages.append,
        input_reader=input_reader,
        gantry_factory=_FakeGantry,
        key_reader=_key_reader(
            [
                ("Z", 50),
                ("\r", 1),
            ]
        ),
        stdin_flusher=lambda: None,
        z_reference_mode="block",
        skip_soft_limit_config=True,
    )

    assert isinstance(result, DeckOriginCalibrationResult)
    assert result.block_height_mm == 35.0
    assert prompts == [
        "Reference height above the deck in mm: ",
        "Reference height above the deck in mm: ",
    ]
    assert any("factory Z travel" in message for message in messages)


def test_calibration_block_height_prompts_when_reader_and_output_supplied():
    prompts: list[str] = []

    value = _calibration_block_height_mm(
        {},
        explicit_block_height_mm=None,
        input_reader=lambda prompt: prompts.append(prompt) or "18.125",
        output=lambda _message: None,
    )

    assert value == 18.125
    assert prompts == ["Reference height above the deck in mm: "]


def test_calibration_block_height_reprompts_for_out_of_range_value():
    responses = iter(["101", "99"])
    messages: list[str] = []

    value = _calibration_block_height_mm(
        {},
        explicit_block_height_mm=None,
        max_height_mm=100.0,
        input_reader=lambda _prompt: next(responses),
        output=messages.append,
    )

    assert value == 99.0
    assert any("factory Z travel" in message for message in messages)


def test_updated_gantry_yaml_writes_block_height_and_requires_cnc_mapping():
    raw_config = {
        "cnc": {"factory_z_travel_mm": 100.0},
        "working_volume": {},
    }

    yaml_text = _updated_gantry_yaml_text(
        raw_config,
        measured_coords={"x": 398.5, "y": 299.25},
        z_min_mm=0.0,
        z_max_mm=96.75,
        calibration_block_height_mm=35.0,
    )

    assert "calibration_block_height_mm: 35.0" in yaml_text

    with pytest.raises(ValueError, match="cnc section must be a mapping"):
        _updated_gantry_yaml_text(
            {"cnc": "bad"},
            measured_coords={"x": 398.5, "y": 299.25},
            z_min_mm=0.0,
            z_max_mm=96.75,
            calibration_block_height_mm=35.0,
        )


def test_run_calibration_records_ruler_gap_but_sets_z_min_to_wpos_zero(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []

    result = run_calibration(
        path,
        output=messages.append,
        gantry_factory=_FakeGantry,
        key_reader=_key_reader(
            [
                ("\r", 1),
            ]
        ),
        stdin_flusher=lambda: None,
        tip_gap_mm=43.0,
        z_reference_mode="ruler-gap",
    )

    assert isinstance(result, DeckOriginCalibrationResult)
    assert result.xy_origin_verification == (0.0, 0.0, 0.0)
    assert result.z_reference_verification == (0.0, 0.0, 0.0)
    assert result.z_min_mm == 0.0
    assert result.factory_z_travel_mm == 100.0
    assert result.reachable_z_min_mm == 43.0
    assert result.measured_working_volume == (398.5, 299.25, 96.75)
    assert result.grbl_max_travel == (398.5, 299.25, 96.75)
    assert ("set_work_coordinates", 0.0, 0.0, None) in _FakeGantry.instance.calls
    assert ("set_work_coordinates", None, None, 0.0) in _FakeGantry.instance.calls
    assert any("WPos Z=0" in message for message in messages)
    assert any("z_min: 0.000" in message for message in messages)
    assert any("reference_tcp_reachable_z_min: 43.000" in message for message in messages)


def test_run_calibration_prints_full_gantry_yaml_with_grbl_settings(tmp_path):
    path = tmp_path / "gantry.yaml"
    path.write_text(
        """\
serial_port: /dev/ttyUSB0
gantry_type: cub_xl
cnc:
  factory_z_travel_mm: 100.0
  y_axis_motion: head
  safe_z: 85.0
working_volume:
  x_min: 0.0
  x_max: 400.0
  y_min: 0.0
  y_max: 300.0
  z_min: 0.0
  z_max: 100.0
grbl_settings:
  dir_invert_mask: 1
  homing_pull_off: 10.0
  steps_per_mm_x: 400.0
instruments:
  asmi:
    type: asmi
    vendor: vernier
""",
        encoding="utf-8",
    )
    messages: list[str] = []

    result = run_calibration(
        path,
        output=messages.append,
        gantry_factory=_FakeGantry,
        key_reader=_key_reader([("\r", 1)]),
        stdin_flusher=lambda: None,
        tip_gap_mm=24.0,
        z_reference_mode="ruler-gap",
        skip_soft_limit_config=True,
    )

    assert isinstance(result, DeckOriginCalibrationResult)
    assert result.grbl_max_travel == (408.5, 309.25, 106.75)
    output_text = "\n".join(messages)
    assert "Full gantry YAML to copy/paste:" in output_text
    assert "dir_invert_mask: 1" in output_text
    assert "homing_pull_off: 10.0" in output_text
    assert "steps_per_mm_x: 400.0" in output_text
    assert "soft_limits: true" in output_text
    assert "homing_enable: true" in output_text
    assert "max_travel_x: 408.5" in output_text
    assert "max_travel_y: 309.25" in output_text
    assert "factory_z_travel_mm: 100.0" in output_text
    assert "z_max: 96.75" in output_text
    assert "max_travel_z: 106.75" in output_text
    assert "instruments:" in output_text
    assert _FakeGantry.instance.calls[0] == ("connect",)


def test_run_calibration_can_prompt_and_write_gantry_yaml(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    output_path = tmp_path / "written_gantry.yaml"
    responses = iter([str(output_path), "y"])

    run_calibration(
        path,
        output=lambda message: None,
        input_reader=lambda prompt: next(responses),
        gantry_factory=_FakeGantry,
        key_reader=_key_reader([("\r", 1)]),
        stdin_flusher=lambda: None,
        write_gantry_yaml=True,
        skip_soft_limit_config=True,
    )

    written = output_path.read_text(encoding="utf-8")
    assert "grbl_settings:" in written
    assert "soft_limits: true" in written
    assert "factory_z_travel_mm: 100.0" in written
    assert "z_max: 96.75" in written
    assert "max_travel_z: 96.75" in written


def test_run_calibration_in_place_write_creates_timestamped_backup(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    original = path.read_text(encoding="utf-8")

    run_calibration(
        path,
        output=lambda message: None,
        gantry_factory=_FakeGantry,
        key_reader=_key_reader([("\r", 1)]),
        stdin_flusher=lambda: None,
        write_gantry_yaml=True,
        output_gantry_path=path,
        backup_existing_output=True,
        skip_soft_limit_config=True,
    )

    backups = list(tmp_path.glob("gantry.yaml.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    assert "grbl_settings:" in path.read_text(encoding="utf-8")


def test_run_calibration_writes_yaml_before_soft_limit_programming_failure(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    output_path = tmp_path / "calibrated.yaml"
    messages: list[str] = []

    with pytest.raises(RuntimeError, match="soft-limit settings did not verify"):
        run_calibration(
            path,
            output=messages.append,
            gantry_factory=_SoftLimitProgrammingFailsFakeGantry,
            key_reader=_key_reader([("\r", 1)]),
            stdin_flusher=lambda: None,
            write_gantry_yaml=True,
            output_gantry_path=output_path,
        )

    assert output_path.exists()
    assert "grbl_settings:" in output_path.read_text(encoding="utf-8")
    output_text = "\n".join(messages)
    assert "Full gantry YAML to copy/paste:" in output_text
    assert f"was written to: {output_path}" in output_text
    assert any(
        call[0] == "configure_soft_limits_from_spans_failed"
        for call in _SoftLimitProgrammingFailsFakeGantry.instance.calls
    )


def test_run_calibration_prompts_for_tip_gap_when_omitted(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    prompts: list[str] = []

    def input_reader(prompt: str) -> str:
        prompts.append(prompt)
        return "12.5"

    result = run_calibration(
        path,
        output=lambda message: None,
        input_reader=input_reader,
        gantry_factory=_FakeGantry,
        key_reader=_key_reader([("\r", 1)]),
        stdin_flusher=lambda: None,
        tip_gap_mm=None,
        z_reference_mode="ruler-gap",
    )

    assert isinstance(result, DeckOriginCalibrationResult)
    assert result.xy_origin_verification == (0.0, 0.0, 0.0)
    assert result.z_reference_verification == (0.0, 0.0, 0.0)
    assert prompts == ["Deck-to-TCP gap in mm: "]


def test_run_calibration_prompt_mode_can_ground_z_on_bottom_contact(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    prompts: list[str] = []
    responses = iter(["y"])

    def input_reader(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    result = run_calibration(
        path,
        output=lambda message: None,
        input_reader=input_reader,
        gantry_factory=_FakeGantry,
        key_reader=_key_reader([("\r", 1)]),
        stdin_flusher=lambda: None,
        tip_gap_mm=None,
        z_reference_mode="prompt",
    )

    assert isinstance(result, DeckOriginCalibrationResult)
    assert result.z_reference_mode == "bottom"
    assert result.z_min_mm == 0.0
    assert result.z_reference_verification == (0.0, 0.0, 0.0)
    assert prompts == [
        "Is the TCP touching true deck bottom at the current pose? [y/N]: ",
    ]


def test_run_calibration_prompt_mode_uses_ruler_gap_when_not_touching(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []
    prompts: list[str] = []
    responses = iter(["", "14.5"])

    def input_reader(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    result = run_calibration(
        path,
        output=messages.append,
        input_reader=input_reader,
        gantry_factory=_FakeGantry,
        key_reader=_key_reader(
            [
                ("\r", 1),
            ]
        ),
        stdin_flusher=lambda: None,
        tip_gap_mm=None,
        z_reference_mode="prompt",
        instrument_name="asmi",
    )

    assert isinstance(result, DeckOriginCalibrationResult)
    assert result.z_reference_mode == "ruler-gap"
    assert result.z_min_mm == 0.0
    assert result.z_reference_verification == (0.0, 0.0, 0.0)
    assert result.reachable_z_min_mm == pytest.approx(14.5)
    assert prompts == [
        "Is the TCP touching true deck bottom at the current pose? [y/N]: ",
        "Deck-to-TCP gap in mm: ",
    ]
    assert any("asmi_reachable_z_min: 14.500" in message for message in messages)


def test_run_calibration_recovers_from_limit_alarm_during_jog(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []

    result = run_calibration(
        path,
        output=messages.append,
        gantry_factory=_LimitRecoveringFakeGantry,
        key_reader=_key_reader(
            [
                ("DOWN", 1),
                ("DOWN", 1),
                ("\r", 1),
                ("\r", 1),
            ]
        ),
        stdin_flusher=lambda: None,
        limit_pull_off_mm=2.0,
    )

    assert isinstance(result, DeckOriginCalibrationResult)
    assert (
        ("jog", 0.0, -1.0, 0.0, 2500.0)
        in _LimitRecoveringFakeGantry.instance.calls
    )
    assert ("jog_cancel",) in _LimitRecoveringFakeGantry.instance.calls
    assert ("reset_and_unlock",) in _LimitRecoveringFakeGantry.instance.calls
    assert (
        ("jog", 0.0, 5.0, 0.0, 2500.0)
        in _LimitRecoveringFakeGantry.instance.calls
    )
    assert any("Limit alarm detected" in message for message in messages)


def test_run_calibration_temporarily_disables_stale_soft_limits(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []

    result = run_calibration(
        path,
        output=messages.append,
        gantry_factory=_SoftLimitAwareFakeGantry,
        key_reader=_key_reader([("\r", 1)]),
        stdin_flusher=lambda: None,
    )

    assert isinstance(result, DeckOriginCalibrationResult)
    calls = _SoftLimitAwareFakeGantry.instance.calls
    disable_call = ("set_soft_limits_enabled", False)
    restore_call = ("set_soft_limits_enabled", True)
    assert disable_call in calls
    assert restore_call in calls
    assert calls.index(disable_call) < calls.index(restore_call)
    assert calls.index(restore_call) < calls.index(
        ("set_work_coordinates", 0.0, 0.0, None)
    )
    assert any("Temporarily disabling GRBL soft limits" in m for m in messages)
    assert any("Restoring GRBL soft limits" in m for m in messages)


def test_run_calibration_enables_hard_limits_for_origin_jog(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []

    result = run_calibration(
        path,
        output=messages.append,
        gantry_factory=_BothLimitsAwareFakeGantry,
        key_reader=_key_reader([("\r", 1)]),
        stdin_flusher=lambda: None,
    )

    assert isinstance(result, DeckOriginCalibrationResult)
    calls = _BothLimitsAwareFakeGantry.instance.calls
    enable_call = ("set_hard_limits_enabled", True)
    revert_call = ("set_hard_limits_enabled", False)
    assert enable_call in calls
    assert revert_call in calls
    assert calls.index(enable_call) < calls.index(revert_call)
    # Soft limits come back on before the hard-limit backstop is dropped.
    assert calls.index(("set_soft_limits_enabled", True)) < calls.index(revert_call)
    assert any("Enabling GRBL hard limits" in m for m in messages)
    assert any("Restoring pre-calibration GRBL hard limits" in m for m in messages)


def test_hard_limit_helpers_handle_missing_or_failing_capabilities():
    messages: list[str] = []

    class RaisingReader:
        def hard_limits_enabled(self):
            raise CommandExecutionError("read failed")

    assert (
        _read_hard_limits_enabled_if_available(RaisingReader(), output=messages.append)
        is None
    )
    assert any("Could not read GRBL hard-limit state" in m for m in messages)

    class ConnectionLostReader:
        def hard_limits_enabled(self):
            raise MillConnectionError("gone")

    with pytest.raises(MillConnectionError):
        _read_hard_limits_enabled_if_available(
            ConnectionLostReader(), output=messages.append
        )

    # No setter available: nothing is written and nothing needs restoring.
    assert _set_hard_limits_enabled_if_available(object(), True) is False

    class ReaderWithoutSetter:
        def hard_limits_enabled(self):
            return False

    assert (
        _temporarily_enable_hard_limits_for_origin_jog(
            ReaderWithoutSetter(), output=messages.append
        )
        is False
    )
    assert any("No GRBL setting writer" in m for m in messages)

    _restore_hard_limits_after_origin_jog(object(), output=messages.append)
    assert any("hard limits stay" in m for m in messages)


def test_outer_finally_reverts_hard_limits_when_soft_restore_fails(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")

    class SoftRestoreFailsGantry(_BothLimitsAwareFakeGantry):
        def set_soft_limits_enabled(self, enabled: bool) -> None:
            super().set_soft_limits_enabled(enabled)
            if enabled:
                raise MillConnectionError("link died during soft-limit restore")

    with pytest.raises(MillConnectionError):
        run_calibration(
            path,
            output=lambda _message: None,
            gantry_factory=SoftRestoreFailsGantry,
            key_reader=_key_reader([("\r", 1)]),
            stdin_flusher=lambda: None,
        )

    # The inner cleanup died before reaching the hard-limit revert; the
    # outer safety net must still drop $21 back to its prior state.
    calls = SoftRestoreFailsGantry.instance.calls
    assert ("set_hard_limits_enabled", False) in calls


def test_run_calibration_leaves_hard_limits_alone_when_already_on(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")

    class _HardLimitsOnFakeGantry(_BothLimitsAwareFakeGantry):
        def __init__(self, config: dict):
            super().__init__(config)
            self.hard_limits_are_enabled = True

    result = run_calibration(
        path,
        output=lambda _message: None,
        gantry_factory=_HardLimitsOnFakeGantry,
        key_reader=_key_reader([("\r", 1)]),
        stdin_flusher=lambda: None,
    )

    assert isinstance(result, DeckOriginCalibrationResult)
    calls = _HardLimitsOnFakeGantry.instance.calls
    assert not any(call[0] == "set_hard_limits_enabled" for call in calls)


def test_run_calibration_continues_after_error_15_jog_rejection(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []

    result = run_calibration(
        path,
        output=messages.append,
        gantry_factory=_SoftLimitRejectingFakeGantry,
        key_reader=_key_reader([("LEFT", 1), ("\r", 1)]),
        stdin_flusher=lambda: None,
    )

    assert isinstance(result, DeckOriginCalibrationResult)
    calls = _SoftLimitRejectingFakeGantry.instance.calls
    assert ("jog", -1.0, 0.0, 0.0, 2500.0) in calls
    assert ("unlock",) not in calls
    assert any("target exceeds the current soft-limit travel" in m for m in messages)


def test_run_calibration_restores_soft_limits_when_jog_aborts(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []

    with pytest.raises(CommandExecutionError):
        run_calibration(
            path,
            output=messages.append,
            gantry_factory=_SoftLimitAwareFailingJogFakeGantry,
            key_reader=_key_reader([("LEFT", 1)]),
            stdin_flusher=lambda: None,
        )

    calls = _SoftLimitAwareFailingJogFakeGantry.instance.calls
    assert ("set_soft_limits_enabled", False) in calls
    assert ("set_soft_limits_enabled", True) in calls
    assert calls.index(("set_soft_limits_enabled", False)) < calls.index(
        ("set_soft_limits_enabled", True)
    )
    assert ("disconnect",) in calls


def test_run_calibration_q_abort_cancels_jog_before_disconnect(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")

    with pytest.raises(KeyboardInterrupt):
        run_calibration(
            path,
            output=lambda _message: None,
            gantry_factory=_FakeGantry,
            key_reader=_key_reader([("Q", 1)]),
            stdin_flusher=lambda: None,
        )

    calls = _FakeGantry.instance.calls
    assert ("jog_cancel",) in calls
    assert calls.index(("jog_cancel",)) < calls.index(("disconnect",))


def test_run_calibration_keyboard_interrupt_cancels_jog_before_disconnect(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")

    def interrupting_key_reader():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_calibration(
            path,
            output=lambda _message: None,
            gantry_factory=_FakeGantry,
            key_reader=interrupting_key_reader,
            stdin_flusher=lambda: None,
        )

    calls = _FakeGantry.instance.calls
    assert ("jog_cancel",) in calls
    assert calls.index(("jog_cancel",)) < calls.index(("disconnect",))


def test_run_calibration_waits_for_idle_before_confirmed_coordinate_read(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")

    run_calibration(
        path,
        output=lambda _message: None,
        gantry_factory=_IdleTrackingFakeGantry,
        key_reader=_key_reader([("RIGHT", 1), ("\r", 1)]),
        stdin_flusher=lambda: None,
    )

    calls = _IdleTrackingFakeGantry.instance.calls
    first_echo_read = calls.index(("get_coordinates",))
    assert calls[first_echo_read - 1] == ("get_status",)
    confirm_read = calls.index(("get_coordinates",), first_echo_read + 1)
    assert calls[confirm_read - 1] == ("get_status",)


def test_run_calibration_aborts_when_recovery_readback_is_unavailable(tmp_path):
    """Recovery readback failure must abort calibration: silently continuing
    would let the operator zero WPos at an unknown physical pose."""
    path = _write_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []

    with pytest.raises(StatusReturnError):
        run_calibration(
            path,
            output=messages.append,
            gantry_factory=_LimitRecoveringNoReadbackFakeGantry,
            key_reader=_key_reader(
                [
                    ("DOWN", 1),
                    ("DOWN", 1),
                    ("\r", 1),
                    ("\r", 1),
                ]
            ),
            stdin_flusher=lambda: None,
            limit_pull_off_mm=2.0,
        )

    assert ("get_coordinates_failed",) in (
        _LimitRecoveringNoReadbackFakeGantry.instance.calls
    )
    assert any("Pulled off the limit switch" in message for message in messages)


def test_dry_run_prints_commands_without_connecting(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []

    def fail_factory(**kwargs):
        raise AssertionError("dry run should not construct a gantry")

    run_calibration(
        path,
        dry_run=True,
        output=messages.append,
        gantry_factory=fail_factory,
    )

    assert "  $H" in messages
    assert "  $10=0" in messages
    assert "  G90" in messages
    assert "  G54" in messages
    assert "  G92.1" in messages
    assert "  <interactive jog to front-left XY origin/lower reach point>" in messages
    assert "  G10 L20 P1 X0 Y0" in messages
    assert "  <confirm true deck-bottom contact>" in messages
    assert "  G10 L20 P1 Z0" in messages
    assert "  $20=0" in messages
    assert "  $130=<x_span_mm>" in messages
    assert "  $131=<y_span_mm>" in messages
    assert "  $132=<z_span_mm>" in messages
    assert "  $22=1" in messages
    assert "  $20=1" in messages


def test_dry_run_prints_ruler_gap_step(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []

    run_calibration(
        path,
        dry_run=True,
        output=messages.append,
        z_reference_mode="ruler-gap",
        tip_gap_mm=5.0,
    )

    assert "  <confirm bottom contact or enter ruler-measured TCP gap; reach metadata=5>" in messages
    assert "  G10 L20 P1 Z0" in messages


def test_dry_run_prints_bottom_reference_step(tmp_path):
    path = _write_gantry(tmp_path / "gantry.yaml")
    messages: list[str] = []

    run_calibration(
        path,
        dry_run=True,
        output=messages.append,
        z_reference_mode="bottom",
    )

    assert "  <confirm true deck-bottom contact>" in messages
    assert "  G10 L20 P1 Z0" in messages


def test_rejects_legacy_negative_space_config(tmp_path):
    path = _write_gantry(tmp_path / "legacy.yaml", x_min=-400.0)

    with pytest.raises(ValueError, match="Deck-origin calibration requires"):
        run_calibration(path, dry_run=True)


# --- _factory_z_travel_mm unit tests ---


def test_factory_z_travel_mm_returns_value():
    assert _factory_z_travel_mm({"cnc": {"factory_z_travel_mm": 87.0}}) == 87.0


def test_factory_z_travel_mm_raises_if_cnc_key_missing():
    with pytest.raises(ValueError, match="cnc.factory_z_travel_mm"):
        _factory_z_travel_mm({})


def test_factory_z_travel_mm_raises_if_factory_z_travel_mm_key_missing():
    with pytest.raises(ValueError, match="cnc.factory_z_travel_mm"):
        _factory_z_travel_mm({"cnc": {}})


def test_factory_z_travel_mm_raises_if_cnc_not_a_dict():
    with pytest.raises(ValueError, match="cnc.factory_z_travel_mm"):
        _factory_z_travel_mm({"cnc": "standard"})


def test_factory_z_travel_mm_raises_if_non_numeric():
    with pytest.raises(ValueError, match="numeric"):
        _factory_z_travel_mm({"cnc": {"factory_z_travel_mm": "not-a-number"}})


def test_factory_z_travel_mm_raises_if_zero():
    with pytest.raises(ValueError, match="> 0"):
        _factory_z_travel_mm({"cnc": {"factory_z_travel_mm": 0.0}})


def test_factory_z_travel_mm_raises_if_negative():
    with pytest.raises(ValueError, match="> 0"):
        _factory_z_travel_mm({"cnc": {"factory_z_travel_mm": -5.0}})


# --- block mode guard tests ---


def test_block_z_calculation_scenario_a_reaches_deck_bottom():
    result = _calculate_block_z_calibration(
        initial_home_z_mm=110.0,
        block_touch_wpos_z_mm=60.0,
        block_height_mm=35.0,
        factory_z_travel_mm=110.0,
        tolerance_mm=0.25,
    )

    assert result.home_to_block_travel_mm == 50.0
    assert result.remaining_below_block_mm == 60.0
    assert result.can_reach_deck_bottom is True
    assert result.z_min_mm == 0.0
    assert result.expected_home_z_mm == 85.0


def test_block_z_calculation_scenario_b_cannot_reach_deck_bottom():
    result = _calculate_block_z_calibration(
        initial_home_z_mm=110.0,
        block_touch_wpos_z_mm=10.0,
        block_height_mm=35.0,
        factory_z_travel_mm=110.0,
        tolerance_mm=0.25,
    )

    assert result.home_to_block_travel_mm == 100.0
    assert result.remaining_below_block_mm == 10.0
    assert result.can_reach_deck_bottom is False
    assert result.z_min_mm == 25.0
    assert result.expected_home_z_mm == 135.0


def _write_gantry_small_z(path):
    path.write_text(
        """\
serial_port: /dev/ttyUSB0
gantry_type: cub_xl
cnc:
  factory_z_travel_mm: 15.0
  y_axis_motion: head
  safe_z: 12.0
working_volume:
  x_min: 0.0
  x_max: 400.0
  y_min: 0.0
  y_max: 300.0
  z_min: 0.0
  z_max: 15.0
""",
        encoding="utf-8",
    )
    return path


def test_block_z_calculation_raises_if_travel_exceeds_factory_range():
    with pytest.raises(RuntimeError, match="exceeds the configured factory Z travel"):
        _calculate_block_z_calibration(
            initial_home_z_mm=110.0,
            block_touch_wpos_z_mm=-10.0,
            block_height_mm=35.0,
            factory_z_travel_mm=100.0,
            tolerance_mm=0.25,
        )
