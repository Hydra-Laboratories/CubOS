"""Tests for the Sartorius Picus 2 driver.

The fake serial below parses the frames the driver actually writes and replies
with correctly-numbered result tokens, rather than replaying a fixed list.
That keeps the tests honest about sequence correlation, which is the part of
this protocol most likely to break silently.
"""

import json
import pathlib

import pytest
from unittest.mock import MagicMock, call, patch

from cubos.instruments.base_instrument import BaseInstrument
from cubos.instruments.pipette.interface import PipetteInstrument
from cubos.instruments.pipette.models import (
    PICUS2_MODELS,
    PipetteConfig,
    PipetteFamily,
    PlungerPipetteConfig,
)
from cubos.instruments.pipette.exceptions import (
    PipetteBatteryError,
    PipetteCommandError,
    PipetteConfigError,
    PipetteConnectionError,
    PipetteMotorControlError,
    PipetteTimeoutError,
)
from cubos.instruments.pipette.vendors.sartorius import (
    SartoriusPicus2Pipette,
    _speed_index,
)


DEFAULT_SCRIPT = {
    "GET_NOMINAL_VOLUME": (["1000"], "OK"),
    "GET_BATTERY_LEVEL": (["87"], "OK"),
    "GET_MODEL": (["Picus 2 1000uL"], "OK"),
    "GET_SERIAL": (["SN-12345"], "OK"),
    "GET_VERSION": (["1.4.2"], "OK"),
}


class FakePicusSerial:
    """Minimal stand-in that speaks the Picus reply grammar."""

    def __init__(self, script=None, no_reply=()):
        self.is_open = True
        self.commands: list[str] = []
        self.buttons: list[str] = []
        self._replies: list[bytes] = []
        self._script = dict(DEFAULT_SCRIPT)
        if script:
            self._script.update(script)
        self.no_reply = list(no_reply)
        # Hazards the real pipette exhibits, opt-in per test.
        self.inject_async = False
        self.interleave_foreign = False

    # -- driver-facing API --------------------------------------------------

    def write(self, frame: bytes) -> None:
        payload = json.loads(frame.decode().strip())
        if "button" in payload:
            self.buttons.append(payload["button"])
            return
        data = payload["data"]
        self.commands.append(data)
        if any(data.startswith(prefix) for prefix in self.no_reply):
            return
        no = payload["no"]
        lines, result = self._lookup(data)
        # The grammar observed on hardware: every frame is answered with
        # ACK/BEGIN, then either a result code *or* bare data lines, then END.
        # A query never sends OK at all, so END is the only terminator.
        if self.inject_async:
            self._replies.append(b'{"button":"RIGHT_PRESSED"}\r\n')
        self._replies.append(f"ACK {no}\r\n".encode())
        self._replies.append(f"BEGIN {no}\r\n".encode())
        if self.interleave_foreign:
            # Another frame's scope opening and closing mid-reply, carrying a
            # data line that must not be collected as ours.
            self._replies.append(b"BEGIN 999\r\n")
            self._replies.append(b"9999\r\n")
            self._replies.append(b"END 999\r\n")
        if lines:
            for line in lines:
                self._replies.append(f"{line}\r\n".encode())
        else:
            self._replies.append(f"{result} {no}\r\n".encode())
        self._replies.append(f"END {no}\r\n".encode())

    def readline(self) -> bytes:
        return self._replies.pop(0) if self._replies else b""

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False

    def reset_input_buffer(self) -> None:
        pass

    # -- helpers ------------------------------------------------------------

    def _lookup(self, data: str):
        for prefix, reply in self._script.items():
            if data.startswith(prefix):
                lines, result = reply
                # An error is reported as a result code, never as data.
                return ([], result) if result != "OK" else (lines, result)
        return ([], "OK")

    def sent(self, prefix: str) -> list[str]:
        return [c for c in self.commands if c.startswith(prefix)]


def connected(script=None, no_reply=(), **kwargs):
    """Return a connected pipette plus its fake serial."""
    fake = FakePicusSerial(script=script, no_reply=no_reply)
    with patch(
        "cubos.instruments.pipette.vendors.sartorius.serial.Serial",
        return_value=fake,
    ):
        pip = SartoriusPicus2Pipette(port="/dev/ttyUSB0", **kwargs)
        pip.connect()
    return pip, fake


# ─── Model registry ──────────────────────────────────────────────────────────


class TestPicusModels:

    def test_registers_the_models_in_use(self):
        assert sorted(PICUS2_MODELS) == [
            "picus2_1ch_10",
            "picus2_1ch_1000",
            "picus2_1ch_120",
            "picus2_1ch_5000",
        ]

    def test_published_vendor_figures(self):
        small = PICUS2_MODELS["picus2_1ch_10"]
        assert (small.min_volume, small.max_volume) == (0.5, 10.0)
        assert small.volume_increment_ul == 0.01
        medium = PICUS2_MODELS["picus2_1ch_120"]
        assert (medium.min_volume, medium.max_volume) == (5.0, 120.0)
        assert medium.volume_increment_ul == 0.1
        large = PICUS2_MODELS["picus2_1ch_1000"]
        assert (large.min_volume, large.max_volume) == (50.0, 1000.0)
        assert large.volume_increment_ul == 1.0
        extra_large = PICUS2_MODELS["picus2_1ch_5000"]
        assert (extra_large.min_volume, extra_large.max_volume) == (
            100.0, 5000.0,
        )
        assert extra_large.volume_increment_ul == 5.0

    def test_all_single_channel(self):
        assert {cfg.channels for cfg in PICUS2_MODELS.values()} == {1}

    def test_family(self):
        assert {cfg.family for cfg in PICUS2_MODELS.values()} == {PipetteFamily.PICUS2}

    def test_carries_no_plunger_geometry(self):
        """The split is the point: a Picus has capability, not millimetres."""
        config = PICUS2_MODELS["picus2_1ch_10"]
        assert isinstance(config, PipetteConfig)
        assert not isinstance(config, PlungerPipetteConfig)
        for field in ("mm_to_ul", "prime_position", "blowout_position"):
            assert not hasattr(config, field)

    def test_unknown_model_rejected(self):
        with pytest.raises(PipetteConfigError, match="Unknown Picus 2 model"):
            SartoriusPicus2Pipette(pipette_model="picus2_nope", offline=True)


# ─── Unit conversions ────────────────────────────────────────────────────────


class TestSpeedMapping:

    def test_interface_default_lands_on_vendor_default(self):
        assert _speed_index(50.0) == 5

    def test_endpoints(self):
        assert _speed_index(0.0) == 1
        assert _speed_index(100.0) == 9

    def test_clamps_out_of_range(self):
        assert _speed_index(-40.0) == 1
        assert _speed_index(400.0) == 9

    def test_monotonic(self):
        values = [_speed_index(pct) for pct in range(0, 101, 10)]
        assert values == sorted(values)

    def test_junk_falls_back_to_the_default(self):
        assert _speed_index(float("nan")) == 5
        assert _speed_index("fast") == 5


class TestVolumeQuantization:

    def test_rounds_to_the_model_increment(self):
        pip = SartoriusPicus2Pipette(pipette_model="picus2_1ch_1000", offline=True)
        assert pip._quantize(500.4) == 500.0
        assert pip._quantize(500.6) == 501.0

    def test_fine_model_keeps_two_decimals(self):
        pip = SartoriusPicus2Pipette(pipette_model="picus2_1ch_10", offline=True)
        assert pip._quantize(1.234) == 1.23

    def test_5000_model_quantizes_to_five_microlitres(self):
        pip = SartoriusPicus2Pipette(
            pipette_model="picus2_1ch_5000", offline=True,
        )
        assert pip._quantize(2497.0) == 2495.0
        assert pip._quantize(2498.0) == 2500.0

    def test_5000_model_enforces_its_bounds(self):
        pip = SartoriusPicus2Pipette(
            pipette_model="picus2_1ch_5000", offline=True,
        )
        assert pip._quantize(100.0) == 100.0
        assert pip._quantize(5000.0) == 5000.0
        with pytest.raises(PipetteCommandError, match="outside"):
            pip._quantize(95.0)
        with pytest.raises(PipetteCommandError, match="outside"):
            pip._quantize(5005.0)

    def test_120_model_quantizes_to_a_tenth_of_a_microlitre(self):
        pip = SartoriusPicus2Pipette(
            pipette_model="picus2_1ch_120", offline=True,
        )
        assert pip._quantize(60.04) == 60.0
        assert pip._quantize(60.06) == 60.1

    def test_120_model_enforces_its_bounds(self):
        pip = SartoriusPicus2Pipette(
            pipette_model="picus2_1ch_120", offline=True,
        )
        assert pip._quantize(5.0) == 5.0
        assert pip._quantize(120.0) == 120.0
        with pytest.raises(PipetteCommandError, match="outside"):
            pip._quantize(4.9)
        with pytest.raises(PipetteCommandError, match="outside"):
            pip._quantize(120.1)

    def test_rejects_volumes_outside_the_model_range(self):
        pip = SartoriusPicus2Pipette(pipette_model="picus2_1ch_1000", offline=True)
        with pytest.raises(PipetteCommandError, match="outside"):
            pip._quantize(10.0)
        with pytest.raises(PipetteCommandError, match="outside"):
            pip._quantize(5000.0)

    def test_rejects_an_aspirate_that_would_overfill_the_tip(self):
        """Per-stroke bounds pass, but the running total must not exceed the tip."""
        pip = SartoriusPicus2Pipette(pipette_model="picus2_1ch_1000", offline=True)
        pip.connect()
        pip.aspirate(600.0)
        with pytest.raises(PipetteCommandError, match="already loaded"):
            pip.aspirate(600.0)
        # Room reappears once some is dispensed.
        pip.dispense(400.0)
        assert pip.aspirate(600.0).loaded_volume_ul == 800.0

    def test_a_full_stroke_is_still_allowed(self):
        pip = SartoriusPicus2Pipette(pipette_model="picus2_1ch_1000", offline=True)
        pip.connect()
        assert pip.aspirate(1000.0).loaded_volume_ul == 1000.0

    def test_rejects_non_finite(self):
        pip = SartoriusPicus2Pipette(offline=True)
        for bad in (float("nan"), float("inf"), True, "500"):
            with pytest.raises(PipetteCommandError):
                pip._quantize(bad)

    def test_whole_microlitre_model_formats_as_an_integer(self):
        pip = SartoriusPicus2Pipette(pipette_model="picus2_1ch_1000", offline=True)
        assert pip._format_volume(500.0) == "500"

    def test_fine_model_formats_with_decimals(self):
        pip = SartoriusPicus2Pipette(pipette_model="picus2_1ch_10", offline=True)
        assert pip._format_volume(1.23) == "1.23"

    def test_5000_model_formats_as_an_integer(self):
        pip = SartoriusPicus2Pipette(
            pipette_model="picus2_1ch_5000", offline=True,
        )
        assert pip._format_volume(2500.0) == "2500"

    def test_5000_model_tracks_full_stroke_and_rejects_overfill(self):
        pip = SartoriusPicus2Pipette(
            pipette_model="picus2_1ch_5000", offline=True,
        )
        pip.connect()
        assert pip.aspirate(5000.0).loaded_volume_ul == 5000.0
        with pytest.raises(PipetteCommandError, match="already loaded"):
            pip.aspirate(100.0)

    def test_120_model_formats_with_one_decimal(self):
        pip = SartoriusPicus2Pipette(
            pipette_model="picus2_1ch_120", offline=True,
        )
        assert pip._format_volume(60.0) == "60.0"

    def test_120_model_tracks_full_stroke_and_rejects_overfill(self):
        pip = SartoriusPicus2Pipette(
            pipette_model="picus2_1ch_120", offline=True,
        )
        pip.connect()
        assert pip.aspirate(120.0).loaded_volume_ul == 120.0
        with pytest.raises(PipetteCommandError, match="already loaded"):
            pip.aspirate(5.0)

    def test_escape_hatch_forces_integers(self):
        """F-4: fractional uL are unverified on hardware."""
        pip = SartoriusPicus2Pipette(
            pipette_model="picus2_1ch_10", offline=True, whole_microlitres_only=True,
        )
        assert pip._format_volume(1.23) == "1"


# ─── Connect ─────────────────────────────────────────────────────────────────


class TestConnect:

    def test_arms_initializes_and_verifies(self):
        _, fake = connected()
        assert fake.sent("AUTO 1")
        assert fake.sent("ENABLE_MOTOR_CONTROL 2")
        assert fake.sent("RUN_INIT")
        assert fake.sent("GET_NOMINAL_VOLUME")
        # The on-screen confirmation is satisfied over the wire.
        assert fake.buttons and fake.buttons[0] == "TRIGGER_BUTTON_RIGHT"

    def test_rejects_a_mismatched_physical_pipette(self):
        """A 1000 uL config on a 10 uL device would over-aspirate 100x."""
        with pytest.raises(PipetteConnectionError, match="reports 10 uL"):
            connected(script={"GET_NOMINAL_VOLUME": (["10"], "OK")})

    def test_model_check_can_be_disabled(self):
        pip, _ = connected(
            script={"GET_NOMINAL_VOLUME": (["10"], "OK")}, verify_model=False,
        )
        assert pip.get_status().is_homed

    def test_unparseable_nominal_volume_warns_but_connects(self):
        pip, _ = connected(script={"GET_NOMINAL_VOLUME": (["unknown"], "OK")})
        assert pip.get_status().is_homed

    def test_refuses_to_start_on_a_flat_battery(self):
        with pytest.raises(PipetteBatteryError, match="below"):
            connected(script={"GET_BATTERY_LEVEL": (["11"], "OK")})

    def test_battery_floor_is_configurable(self):
        pip, _ = connected(
            script={"GET_BATTERY_LEVEL": (["11"], "OK")}, min_battery_percent=5.0,
        )
        assert pip.get_status().battery_percent == 11.0

    def test_arm_timeout_names_the_physical_action(self):
        with patch(
            "cubos.instruments.pipette.vendors.sartorius._ARM_TIMEOUT", 0.05,
        ):
            with pytest.raises(PipetteConnectionError, match="right softkey"):
                connected(no_reply=["ENABLE_MOTOR_CONTROL"])

    def test_port_is_closed_when_connect_fails(self):
        fake = FakePicusSerial(script={"GET_BATTERY_LEVEL": (["1"], "OK")})
        with patch(
            "cubos.instruments.pipette.vendors.sartorius.serial.Serial",
            return_value=fake,
        ):
            pip = SartoriusPicus2Pipette(port="/dev/ttyUSB0")
            with pytest.raises(PipetteBatteryError):
                pip.connect()
        assert not fake.is_open

    def test_unopenable_port_raises_connection_error(self):
        import serial as pyserial

        with patch(
            "cubos.instruments.pipette.vendors.sartorius.serial.Serial",
            side_effect=pyserial.SerialException("no such device"),
        ):
            pip = SartoriusPicus2Pipette(port="/dev/nope")
            with pytest.raises(PipetteConnectionError, match="Cannot open"):
                pip.connect()


# ─── Commands on the wire ────────────────────────────────────────────────────


class TestCommands:

    def test_aspirate(self):
        pip, fake = connected()
        result = pip.aspirate(500.0)
        assert fake.sent("RUN_ASPIRATE") == ["RUN_ASPIRATE 500 5"]
        assert result.volume_ul == 500.0
        assert result.loaded_volume_ul == 500.0
        # No position readback exists on this vendor.
        assert result.position_mm == 0.0

    def test_dispense_tracks_loaded_volume(self):
        pip, fake = connected()
        pip.aspirate(500.0)
        result = pip.dispense(200.0)
        assert fake.sent("RUN_DISPENSE") == ["RUN_DISPENSE 200 5"]
        assert result.loaded_volume_ul == 300.0

    def test_speed_always_comes_from_the_caller(self):
        """There is no per-instrument default that could silently do nothing."""
        pip, fake = connected()
        pip.aspirate(500.0, speed=0.0)
        assert fake.sent("RUN_ASPIRATE") == ["RUN_ASPIRATE 500 1"]

    def test_speed_reaches_the_wire(self):
        pip, fake = connected()
        pip.aspirate(500.0, speed=100.0)
        assert fake.sent("RUN_ASPIRATE") == ["RUN_ASPIRATE 500 9"]

    def test_quantized_volume_is_what_gets_commanded_and_reported(self):
        pip, fake = connected()
        result = pip.aspirate(500.6)
        assert fake.sent("RUN_ASPIRATE") == ["RUN_ASPIRATE 501 5"]
        assert result.volume_ul == 501.0

    def test_blowout_uses_the_configured_delay(self):
        pip, fake = connected(blowout_delay_ms=1500, blowout_go_home=False)
        pip.aspirate(500.0)
        pip.blowout()
        assert fake.sent("BLOW_OUT") == ["BLOW_OUT 0 5 1500"]
        assert pip.loaded_volume_ul == 0.0

    def test_mix_is_a_host_side_two_height_loop(self):
        pip, fake = connected()
        gantry = MagicMock()
        result = pip.mix(200.0, cycles=3, gantry=gantry, position=(1.0, 2.0, 3.0))
        assert len(fake.sent("RUN_ASPIRATE")) == 6
        assert len(fake.sent("RUN_DISPENSE")) == 6
        assert result.cycles == 3
        assert gantry.move.call_count == 6
        assert gantry.move.call_args_list[0] == call(pip, (1.0, 2.0, 4.0))
        assert gantry.move.call_args_list[1] == call(pip, (1.0, 2.0, 3.0))
        assert pip.loaded_volume_ul == 0.0

    def test_pick_up_tip_sends_nothing_to_the_pipette(self):
        """Tip pickup is gantry motion; the cone is pressed onto the tip."""
        pip, fake = connected()
        before = list(fake.commands)
        pip.pick_up_tip()
        assert fake.commands == before
        assert pip.get_status().has_tip

    def test_drop_tip_uses_the_electronic_ejector(self):
        pip, fake = connected()
        pip.pick_up_tip()
        pip.set_attached_tip_extension(59.3)
        pip.drop_tip()
        assert fake.sent("TIP_EJECT") == ["TIP_EJECT"]
        assert not pip.get_status().has_tip
        assert pip.attached_tip_extension == 0.0

    def test_home_initializes_once_then_moves(self):
        pip, fake = connected()
        pip.home()
        pip.home()
        # RUN_INIT during connect; plain HOME afterwards.
        assert len(fake.sent("RUN_INIT")) == 1
        assert len(fake.sent("HOME")) == 2

    def test_prime_is_a_documented_no_op(self):
        pip, fake = connected()
        before = list(fake.commands)
        pip.prime()
        assert fake.commands == before

    def test_disconnect_releases_motor_control(self):
        pip, fake = connected()
        pip.disconnect()
        assert fake.sent("ENABLE_MOTOR_CONTROL 0")
        assert not fake.is_open

    def test_health_check_round_trips(self):
        pip, _ = connected()
        assert pip.health_check() is True

    def test_status_reports_battery_but_not_position(self):
        pip, _ = connected()
        status = pip.get_status()
        assert status.battery_percent == 87.0
        assert status.position_mm == 0.0
        assert status.max_volume == 1000.0
        assert status.is_valid


# ─── Failure handling ────────────────────────────────────────────────────────


class TestFailures:

    def test_error_result_raises_command_error(self):
        pip, _ = connected(script={"RUN_ASPIRATE": ([], "ERR_RANGE_PARAMETERS")})
        with pytest.raises(PipetteCommandError, match="ERR_RANGE_PARAMETERS"):
            pip.aspirate(500.0)

    def test_full_is_surfaced_not_swallowed(self):
        pip, _ = connected(script={"RUN_ASPIRATE": ([], "FULL")})
        with pytest.raises(PipetteCommandError, match="FULL"):
            pip.aspirate(500.0)

    def test_motor_control_abort_raises_its_own_error(self):
        pip, _ = connected(script={"RUN_ASPIRATE": ([], "MOTOR_CONTROL_ABORTED")})
        with pytest.raises(PipetteMotorControlError, match="position is now unknown"):
            pip.aspirate(500.0)

    def test_driver_fails_closed_after_an_abort(self):
        """The piston position is unknown, so nothing may be retried."""
        pip, _ = connected(script={"RUN_ASPIRATE": ([], "MOTOR_CONTROL_ABORTED")})
        with pytest.raises(PipetteMotorControlError):
            pip.aspirate(500.0)
        with pytest.raises(PipetteMotorControlError, match="reconnect"):
            pip.dispense(100.0)
        assert pip.health_check() is False

    def test_reconnect_recovers_from_an_abort(self):
        """The abort message says to reconnect, so reconnecting must work."""
        pip, _ = connected(script={"RUN_ASPIRATE": ([], "MOTOR_CONTROL_ABORTED")})
        with pytest.raises(PipetteMotorControlError):
            pip.aspirate(500.0)
        pip.disconnect()
        with patch(
            "cubos.instruments.pipette.vendors.sartorius.serial.Serial",
            return_value=FakePicusSerial(),
        ):
            pip.connect()
        assert pip.health_check() is True
        assert pip.aspirate(500.0).volume_ul == 500.0

    def test_timeout_raises(self):
        # Motion commands carry their own generous deadline, so shortening
        # `command_timeout` alone would leave this spinning for two minutes.
        pip, _ = connected()
        pip._serial.no_reply = ["RUN_ASPIRATE"]
        with patch(
            "cubos.instruments.pipette.vendors.sartorius._MOTION_TIMEOUT", 0.05,
        ):
            with pytest.raises(PipetteTimeoutError, match="Timed out"):
                pip.aspirate(500.0)

    def test_commands_before_connect_fail(self):
        pip = SartoriusPicus2Pipette(port="/dev/ttyUSB0")
        with pytest.raises(PipetteCommandError, match="Not connected"):
            pip.aspirate(500.0)

    def test_mix_failure_attempts_to_return_liquid(self):
        pip, fake = connected()
        calls = {"n": 0}
        original = pip._send

        def flaky(data, **kwargs):
            if data.startswith("RUN_DISPENSE"):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise PipetteCommandError("dispense failed")
            return original(data, **kwargs)

        pip._send = flaky
        with pytest.raises(PipetteCommandError, match="dispense failed"):
            pip.mix(200.0, cycles=3, gantry=MagicMock(), position=(0.0, 0.0, 0.0))
        assert fake.sent("BLOW_OUT"), "expected blow-out recovery"

    def test_health_check_false_when_disconnected(self):
        pip = SartoriusPicus2Pipette(port="/dev/ttyUSB0")
        assert pip.health_check() is False


# ─── Offline path ────────────────────────────────────────────────────────────


class TestOffline:

    def test_is_a_base_instrument(self):
        assert isinstance(SartoriusPicus2Pipette(offline=True), BaseInstrument)
        assert isinstance(SartoriusPicus2Pipette(offline=True), PipetteInstrument)

    def test_full_cycle_without_hardware(self):
        pip = SartoriusPicus2Pipette(offline=True)
        pip.connect()
        pip.warm_up()
        pip.pick_up_tip()
        pip.set_attached_tip_extension(59.3)
        assert pip.aspirate(500.0).loaded_volume_ul == 500.0
        assert pip.dispense(500.0).loaded_volume_ul == 0.0
        pip.blowout()
        assert pip.mix(
            200.0, cycles=2, gantry=MagicMock(), position=(0.0, 0.0, 0.0),
        ).success
        pip.drop_tip()
        pip.disconnect()

    def test_offline_still_validates_volume(self):
        pip = SartoriusPicus2Pipette(offline=True)
        pip.connect()
        with pytest.raises(PipetteCommandError, match="outside"):
            pip.aspirate(5000.0)

    def test_offline_reports_no_battery(self):
        pip = SartoriusPicus2Pipette(offline=True)
        pip.connect()
        assert pip.get_status().battery_percent is None

    def test_offline_health_check_passes(self):
        pip = SartoriusPicus2Pipette(offline=True)
        assert pip.health_check() is True


# ─── Mount geometry ──────────────────────────────────────────────────────────


class TestMountGeometry:

    def test_effective_depth_includes_the_tip(self):
        pip = SartoriusPicus2Pipette(offline=True, depth=100.0)
        pip.set_attached_tip_extension(59.3)
        assert pip.effective_depth == pytest.approx(159.3)

    def test_tip_extension_must_be_non_negative_and_finite(self):
        pip = SartoriusPicus2Pipette(offline=True)
        for bad in (-1.0, float("nan"), True, "59"):
            with pytest.raises(PipetteConfigError):
                pip.set_attached_tip_extension(bad)

    def test_liquid_classes_are_vendor_agnostic(self):
        pip = SartoriusPicus2Pipette(
            offline=True,
            liquid_classes={"glycerol": {"multiplier": 1.03, "offset_ul": 4.9}},
        )
        correction = pip.correction_for("glycerol")
        assert correction.apply(100.0) == pytest.approx(107.9)


# ─── Error paths and edge cases ──────────────────────────────────────────────


class TestRobustness:
    """A hardware driver is mostly its failure paths."""

    def test_config_is_exposed_for_stroke_planning(self):
        pip = SartoriusPicus2Pipette(pipette_model="picus2_1ch_1000", offline=True)
        assert pip.config is PICUS2_MODELS["picus2_1ch_1000"]

    def test_identity_getters(self):
        pip, _ = connected()
        assert pip.get_model() == "Picus 2 1000uL"
        assert pip.get_serial_number() == "SN-12345"
        assert pip.get_nominal_volume() == "1000"

    def test_identity_getters_offline(self):
        pip = SartoriusPicus2Pipette(offline=True)
        assert pip.get_model() == ""
        assert pip.get_serial_number() == ""
        assert pip.get_nominal_volume() == "1000.0"

    def test_disconnect_closes_the_port_even_if_release_fails(self):
        pip, fake = connected(script={"ENABLE_MOTOR_CONTROL 0": ([], "FAILED")})
        pip.disconnect()
        assert not fake.is_open

    def test_health_check_false_after_motor_control_is_lost(self):
        pip, _ = connected(script={"GET_VERSION": ([], "FAILED")})
        assert pip.health_check() is False

    def test_prime_initializes_when_the_piston_never_was(self):
        pip = SartoriusPicus2Pipette(offline=True)
        pip.prime()
        assert pip.get_status().is_homed

    def test_pick_up_tip_initializes_first_if_needed(self):
        fake = FakePicusSerial()
        with patch(
            "cubos.instruments.pipette.vendors.sartorius.serial.Serial",
            return_value=fake,
        ):
            pip = SartoriusPicus2Pipette(port="/dev/ttyUSB0")
            pip.connect()
        pip._initialized = False
        pip.pick_up_tip()
        assert len(fake.sent("RUN_INIT")) == 2

    def test_missing_nominal_volume_skips_the_model_check(self):
        pip, _ = connected(script={"GET_NOMINAL_VOLUME": ([], "OK")})
        assert pip.get_status().is_homed

    def test_battery_read_failure_is_not_fatal(self):
        pip, _ = connected(script={"GET_BATTERY_LEVEL": ([], "OK")})
        assert pip.get_status().battery_percent is None

    def test_refused_motor_control_raises(self):
        with pytest.raises(PipetteConnectionError, match="refused motor control"):
            connected(script={"ENABLE_MOTOR_CONTROL 2": ([], "NOT_ALLOWED")})

    def test_serial_write_failure_surfaces_as_connection_error(self):
        import serial as pyserial

        pip, fake = connected()

        def boom(_frame):
            raise pyserial.SerialException("cable yanked")

        fake.write = boom
        with pytest.raises(PipetteConnectionError, match="write failed"):
            pip.aspirate(500.0)

    def test_serial_read_failure_surfaces_as_connection_error(self):
        import serial as pyserial

        pip, fake = connected()

        def boom():
            raise pyserial.SerialException("cable yanked")

        fake.readline = boom
        with pytest.raises(PipetteConnectionError, match="read failed"):
            pip.aspirate(500.0)

    def test_close_tolerates_a_failing_port(self):
        import serial as pyserial

        pip, fake = connected()

        def boom():
            raise pyserial.SerialException("already gone")

        fake.close = boom
        pip.disconnect()
        assert pip._serial is None

    def test_mix_recovery_is_skipped_when_the_tip_is_empty(self):
        pip, fake = connected()
        pip._recover_interrupted_mix(50.0)
        assert not fake.sent("BLOW_OUT")

    def test_parse_line_sorts_the_reply_grammar(self):
        parse = SartoriusPicus2Pipette._parse_line
        assert parse("") == ("ignore", None, None)
        assert parse("ACK 5") == ("ack", 5, None)
        assert parse("BEGIN 5") == ("begin", 5, None)
        assert parse("END 5") == ("end", 5, None)
        assert parse("OK 5") == ("result", 5, "OK")
        assert parse("NOT_ALLOWED 5") == ("result", 5, "NOT_ALLOWED")
        # Payload lines carry no sequence number of their own.
        assert parse("1000") == ("data", None, "1000")
        assert parse("Picus 2 1000uL") == ("data", None, "Picus 2 1000uL")
        # Unsolicited button events must never be read as a command's output.
        assert parse('{"button":"RIGHT_PRESSED"}') == ("ignore", None, None)

    def test_query_reply_without_a_result_code_is_read(self):
        """Observed on hardware: a query answers ACK/BEGIN/data/END, no OK."""
        pip, _ = connected()
        assert pip.get_nominal_volume() == "1000"

    def test_async_button_events_do_not_corrupt_a_reply(self):
        pip, fake = connected()
        fake.inject_async = True
        assert pip.get_model() == "Picus 2 1000uL"

    def test_interleaved_replies_are_attributed_by_scope(self):
        """Replies for different frames interleave; data belongs to its scope."""
        pip, fake = connected()
        fake.interleave_foreign = True
        assert pip.get_serial_number() == "SN-12345"

    def test_first_number_parsing(self):
        from cubos.instruments.pipette.vendors.sartorius import _first_number

        assert _first_number("87") == 87.0
        assert _first_number("battery 87 %") == 87.0
        assert _first_number("1,000") == 1000.0
        assert _first_number("no digits here") is None


# ─── End to end through the protocol engine ──────────────────────────────────

FIXTURE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "fixtures" / "configs" / "pipette_tip_transfer"
)


class TestThroughTheEngine:
    """The driver is only useful if the engine can drive it.

    These run the shipped tip-transfer fixture -- pick_up_tip, transfer,
    blowout, drop_tip -- with the vendor swapped from opentrons to sartorius,
    which exercises stroke planning, tip-state modelling, and reachability
    against a config that carries no plunger geometry at all.
    """

    def test_setup_validation_passes_offline(self):
        from cubos.protocol_engine.setup import setup_protocol

        protocol, context = setup_protocol(
            str(FIXTURE / "gantry_sartorius.yaml"),
            str(FIXTURE / "deck.yaml"),
            str(FIXTURE / "protocol.yaml"),
            mock_mode=True,
        )
        assert len(protocol.steps) == 6
        assert isinstance(
            context.gantry.instruments["pipette"], SartoriusPicus2Pipette,
        )

    def test_protocol_executes_offline(self):
        from cubos.protocol_engine.setup import setup_protocol

        protocol, context = setup_protocol(
            str(FIXTURE / "gantry_sartorius.yaml"),
            str(FIXTURE / "deck.yaml"),
            str(FIXTURE / "protocol.yaml"),
            mock_mode=True,
        )
        context.gantry.connect_instruments()
        try:
            protocol.execute(context)
        finally:
            context.gantry.disconnect_instruments()

        pipette = context.gantry.instruments["pipette"]
        status = pipette.get_status()
        assert not status.has_tip, "drop_tip should have cleared the tip"
        assert pipette.attached_tip_extension == 0.0
        assert pipette.loaded_volume_ul == 0.0

    def test_stroke_planning_sees_the_picus_capacity(self):
        """`pipette_capacity` isinstance-checks the base config, so the split
        must not break volume splitting for a plunger-free vendor."""
        from cubos.protocol_engine.commands._liquid_transfer import (
            pipette_capacity,
            plan_strokes,
        )

        pipette = SartoriusPicus2Pipette(
            pipette_model="picus2_1ch_1000", offline=True,
        )
        capacity = pipette_capacity(pipette)
        assert capacity is not None
        assert capacity.max_volume == 1000.0
        # 1500 uL exceeds one stroke and splits evenly within the range.
        assert plan_strokes(1500.0, capacity) == [750.0, 750.0]


# ─── Liquid hold across disconnect / reconnect ──────────────────────────────


class TestLiquidHold:
    """A run that fails with liquid in the tip must not expel it.

    ``ENABLE_MOTOR_CONTROL 0`` and ``RUN_INIT`` both sweep the piston, so
    neither may be sent while ``loaded_volume_ul`` is positive.
    """

    def test_disconnect_keeps_session_while_holding_liquid(self):
        pip, fake = connected()
        pip.aspirate(200.0)
        pip.disconnect()
        assert not fake.sent("ENABLE_MOTOR_CONTROL 0")
        assert fake.is_open
        assert pip.loaded_volume_ul == 200.0
        assert pip.get_status().is_homed

    def test_reconnect_while_holding_reuses_session_without_init(self):
        pip, fake = connected()
        pip.aspirate(200.0)
        pip.disconnect()
        pip.connect()
        assert len(fake.sent("RUN_INIT")) == 1
        assert pip.loaded_volume_ul == 200.0
        # The operator can now return the liquid deliberately ...
        pip.blowout()
        assert pip.loaded_volume_ul == 0.0
        # ... after which release proceeds as usual.
        pip.disconnect()
        assert fake.sent("ENABLE_MOTOR_CONTROL 0")
        assert not fake.is_open

    def test_reconnect_after_port_loss_skips_init_while_holding(self):
        pip, fake = connected()
        pip.aspirate(150.0)
        fake.close()
        fresh = FakePicusSerial()
        with patch(
            "cubos.instruments.pipette.vendors.sartorius.serial.Serial",
            return_value=fresh,
        ):
            pip.connect()
        assert not fresh.sent("RUN_INIT")
        assert fresh.sent("ENABLE_MOTOR_CONTROL 2")
        assert pip.loaded_volume_ul == 150.0
        assert pip.get_status().is_homed

    def test_reconnect_after_abort_rearms_without_init(self):
        pip, fake = connected()
        pip.aspirate(100.0)
        pip._motor_control_lost = True
        pip.disconnect()
        assert fake.is_open
        pip.connect()
        assert len(fake.sent("ENABLE_MOTOR_CONTROL 2")) == 2
        assert len(fake.sent("RUN_INIT")) == 1
        assert pip.loaded_volume_ul == 100.0

    def test_home_refuses_while_holding(self):
        pip, fake = connected()
        pip.aspirate(50.0)
        before = list(fake.commands)
        with pytest.raises(PipetteCommandError, match="still holds 50 uL"):
            pip.home()
        assert fake.commands == before
        pip.dispense(50.0)
        pip.home()
        assert fake.sent("HOME")

    def test_empty_tip_disconnects_normally(self):
        pip, fake = connected()
        pip.aspirate(100.0)
        pip.dispense(100.0)
        pip.disconnect()
        assert fake.sent("ENABLE_MOTOR_CONTROL 0")
        assert not fake.is_open
