import pytest
from unittest.mock import patch, MagicMock, PropertyMock, call

from cubos.instruments.base_instrument import BaseInstrument, InstrumentError
from cubos.instruments.pipette.models import (
    PipetteFamily,
    PipetteStatus,
    AspirateResult,
    MixResult,
    PIPETTE_MODELS,
)
from cubos.instruments.pipette.exceptions import (
    PipetteError,
    PipetteConnectionError,
    PipetteCommandError,
    PipetteTimeoutError,
    PipetteConfigError,
)
from cubos.instruments.pipette.vendors.opentrons import OpentronsPipette


# --- Model tests --------------------------------------------------------------

class TestPipetteConfig:

    def test_p300_calibrated_values(self):
        cfg = PIPETTE_MODELS["p300_single_gen2"]
        assert cfg.max_volume == 200.0
        assert cfg.prime_position == 36.0
        assert cfg.blowout_position == 46.0
        assert cfg.drop_tip_position == 60.0
        assert cfg.mm_to_ul == pytest.approx(0.1098)

    def test_p300_is_ot2_family(self):
        cfg = PIPETTE_MODELS["p300_single_gen2"]
        assert cfg.family == PipetteFamily.OT2

    def test_flex_is_flex_family(self):
        cfg = PIPETTE_MODELS["flex_1channel_1000"]
        assert cfg.family == PipetteFamily.FLEX

    def test_frozen_dataclass(self):
        cfg = PIPETTE_MODELS["p300_single_gen2"]
        with pytest.raises(AttributeError):
            cfg.max_volume = 500.0

    def test_all_models_have_positive_max_volume(self):
        for name, cfg in PIPETTE_MODELS.items():
            assert cfg.max_volume > 0, f"{name} has non-positive max_volume"

    def test_all_models_have_valid_channels(self):
        for name, cfg in PIPETTE_MODELS.items():
            assert cfg.channels in (1, 8, 96), f"{name} has unexpected channels={cfg.channels}"

    def test_model_count(self):
        assert len(PIPETTE_MODELS) == 10


class TestPipetteStatus:

    def test_valid_status(self):
        status = PipetteStatus(
            is_homed=True, position_mm=36.0, max_volume=200.0,
            has_tip=True, is_primed=True,
        )
        assert status.is_valid is True

    def test_invalid_zero_max_volume(self):
        status = PipetteStatus(
            is_homed=True, position_mm=0.0, max_volume=0.0,
            has_tip=False, is_primed=False,
        )
        assert status.is_valid is False

    def test_invalid_negative_position(self):
        status = PipetteStatus(
            is_homed=True, position_mm=-1.0, max_volume=200.0,
            has_tip=False, is_primed=False,
        )
        assert status.is_valid is False

    def test_frozen(self):
        status = PipetteStatus(
            is_homed=True, position_mm=0.0, max_volume=200.0,
            has_tip=False, is_primed=False,
        )
        with pytest.raises(AttributeError):
            status.is_homed = False


class TestAspirateResult:

    def test_creation(self):
        result = AspirateResult(success=True, volume_ul=100.0, position_mm=10.98)
        assert result.success is True
        assert result.volume_ul == 100.0
        assert result.position_mm == pytest.approx(10.98)

    def test_frozen(self):
        result = AspirateResult(success=True, volume_ul=100.0, position_mm=10.0)
        with pytest.raises(AttributeError):
            result.success = False


class TestMixResult:

    def test_creation(self):
        result = MixResult(success=True, volume_ul=50.0, cycles=3)
        assert result.success is True
        assert result.volume_ul == 50.0
        assert result.cycles == 3

    def test_frozen(self):
        result = MixResult(success=True, volume_ul=50.0, cycles=3)
        with pytest.raises(AttributeError):
            result.cycles = 5


# --- Exception hierarchy tests ------------------------------------------------

class TestExceptions:

    def test_pipette_error_is_instrument_error(self):
        assert issubclass(PipetteError, InstrumentError)

    def test_connection_error_hierarchy(self):
        assert issubclass(PipetteConnectionError, PipetteError)
        err = PipetteConnectionError("port not found")
        assert isinstance(err, InstrumentError)

    def test_command_error_hierarchy(self):
        assert issubclass(PipetteCommandError, PipetteError)

    def test_timeout_error_hierarchy(self):
        assert issubclass(PipetteTimeoutError, PipetteError)

    def test_config_error_hierarchy(self):
        assert issubclass(PipetteConfigError, PipetteError)


# --- Parsing tests ------------------------------------------------------------

class TestParsing:

    def test_parse_key_value_typical(self):
        response = "OK:{homed:1,pos:10.5,max_vol:200}"
        result = OpentronsPipette._parse_key_value(response)
        assert result["homed"] == 1.0
        assert result["pos"] == pytest.approx(10.5)
        assert result["max_vol"] == 200.0

    def test_parse_key_value_empty_body(self):
        result = OpentronsPipette._parse_key_value("OK:{}")
        assert result == {}

    def test_parse_key_value_single_pair(self):
        result = OpentronsPipette._parse_key_value("OK:{pos:36.0}")
        assert result["pos"] == pytest.approx(36.0)

    def test_parse_key_value_non_numeric_skipped(self):
        result = OpentronsPipette._parse_key_value("OK:{status:ready,pos:10.0}")
        assert "status" not in result
        assert result["pos"] == pytest.approx(10.0)

    def test_parse_position(self):
        response = "OK:{pos:36.5,homed:1}"
        assert OpentronsPipette._parse_position(response) == pytest.approx(36.5)

    def test_parse_position_missing(self):
        response = "OK:{homed:1}"
        assert OpentronsPipette._parse_position(response) == 0.0

    def test_parse_key_value_quoted_keys(self):
        # Exact format the Pawduino firmware sends for CMD_PIPETTE_STATUS.
        response = 'OK:{"homed":0,"pos":0.00,"max_vol":300.00}'
        result = OpentronsPipette._parse_key_value(response)
        assert result["homed"] == 0.0
        assert result["pos"] == pytest.approx(0.0)
        assert result["max_vol"] == pytest.approx(300.0)

    def test_parse_key_value_quoted_string_values_skipped(self):
        response = 'OK:{"msg":"Pipette moved","v":[55.00]}'
        result = OpentronsPipette._parse_key_value(response)
        assert "msg" not in result


# --- Driver constructor tests -------------------------------------------------

class TestPipetteConstructor:

    def test_unknown_model_raises_config_error(self):
        with pytest.raises(PipetteConfigError, match="Unknown pipette model"):
            OpentronsPipette(pipette_model="p9999_fake", port="/dev/null")

    def test_known_model_accepted(self):
        pip = OpentronsPipette(pipette_model="p300_single_gen2", port="/dev/null")
        assert pip.config.name == "p300_single_gen2"

    def test_is_base_instrument(self):
        pip = OpentronsPipette(pipette_model="p300_single_gen2", port="/dev/null")
        assert isinstance(pip, BaseInstrument)

    def test_custom_name(self):
        pip = OpentronsPipette(
            pipette_model="p300_single_gen2", port="/dev/null", name="my_pip"
        )
        assert pip.name == "my_pip"


# --- Driver lifecycle tests (mocked serial) -----------------------------------

class TestPipetteLifecycle:

    def _make_mock_serial(self, responses=None):
        """Create a mock serial.Serial that returns canned responses."""
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_ser.in_waiting = 0
        if responses is None:
            responses = ["OK:{homed:1,pos:0.0,max_vol:200}\n"]
        hello = 'OK:{"msg":"Hello from Pawduino!"}\n'
        mock_ser.readline.side_effect = [r.encode() for r in [hello, *responses]]
        return mock_ser

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_connect_opens_serial(self, mock_sleep, mock_serial_cls):
        mock_ser = self._make_mock_serial()
        mock_serial_cls.return_value = mock_ser

        pip = OpentronsPipette(pipette_model="p300_single_gen2", port="/dev/ttyUSB0")
        pip.connect()

        mock_serial_cls.assert_called_once_with(
            port="/dev/ttyUSB0", baudrate=115200, timeout=30.0,
        )
        assert mock_sleep.called

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_connect_homes_and_primes_unhomed_plunger(
        self, mock_sleep, mock_serial_cls
    ):
        # Port-open resets the Arduino, so real hardware reports homed:0;
        # connect must re-home and prime before any protocol motion.
        responses = [
            'OK:{"homed":0,"pos":0.00,"max_vol":300.00}\n',  # status
            'OK:{"msg":"Pipette homed"}\n',                   # home
            'OK:{"msg":"Pipette moved","v":[36.00]}\n',       # prime
        ]
        mock_ser = self._make_mock_serial(responses)
        mock_serial_cls.return_value = mock_ser

        pip = OpentronsPipette(pipette_model="p300_single_gen2", port="/dev/ttyUSB0")
        pip.connect()

        written = [c[0][0].decode() for c in mock_ser.write.call_args_list]
        assert written[0].startswith("0")       # link hello
        assert written[1].startswith("14")      # status
        assert written[2].startswith("10")      # home
        assert written[3].startswith("11,36.0")  # prime = move to prime_position

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_connect_skips_homing_when_already_homed(
        self, mock_sleep, mock_serial_cls
    ):
        mock_ser = self._make_mock_serial(
            ['OK:{"homed":1,"pos":36.00,"max_vol":300.00}\n']
        )
        mock_serial_cls.return_value = mock_ser

        pip = OpentronsPipette(pipette_model="p300_single_gen2", port="/dev/ttyUSB0")
        pip.connect()

        written = [c[0][0].decode() for c in mock_ser.write.call_args_list]
        assert len(written) == 2
        assert written[0].startswith("0")   # link hello
        assert written[1].startswith("14")

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_connect_discards_boot_banner(self, mock_sleep, mock_serial_cls):
        mock_ser = self._make_mock_serial()
        # Banner bytes pending on the first check, quiet on the second.
        type(mock_ser).in_waiting = PropertyMock(side_effect=[1, 0])
        mock_serial_cls.return_value = mock_ser

        pip = OpentronsPipette(pipette_model="p300_single_gen2", port="/dev/ttyUSB0")
        pip.connect()

        mock_ser.reset_input_buffer.assert_called_once()

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_connect_raises_when_homing_fails(self, mock_sleep, mock_serial_cls):
        responses = [
            'OK:{"homed":0,"pos":0.00,"max_vol":300.00}\n',
            'ERR:{"error":"Failed to home pipette"}\n',
            'ERR:{"error":"Failed to home pipette"}\n',
        ]
        mock_ser = self._make_mock_serial(responses)
        mock_serial_cls.return_value = mock_ser

        pip = OpentronsPipette(pipette_model="p300_single_gen2", port="/dev/ttyUSB0")
        with pytest.raises(PipetteConnectionError, match="home/prime"):
            pip.connect()
        mock_ser.close.assert_called_once()

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_connect_raises_on_serial_error(self, mock_sleep, mock_serial_cls):
        import serial as real_serial
        mock_serial_cls.side_effect = real_serial.SerialException("port busy")

        pip = OpentronsPipette(pipette_model="p300_single_gen2", port="/dev/ttyUSB0")
        with pytest.raises(PipetteConnectionError, match="Cannot open serial"):
            pip.connect()

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_connect_raises_on_no_response(self, mock_sleep, mock_serial_cls):
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_ser.in_waiting = 0
        mock_ser.readline.return_value = b""
        mock_serial_cls.return_value = mock_ser

        pip = OpentronsPipette(
            pipette_model="p300_single_gen2", port="/dev/ttyUSB0",
            command_timeout=0.1,
        )
        with pytest.raises(PipetteConnectionError, match="did not answer hello"):
            pip.connect()

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_disconnect_closes_serial(self, mock_sleep, mock_serial_cls):
        mock_ser = self._make_mock_serial()
        mock_serial_cls.return_value = mock_ser

        pip = OpentronsPipette(pipette_model="p300_single_gen2", port="/dev/ttyUSB0")
        pip.connect()
        pip.disconnect()

        mock_ser.close.assert_called_once()

    def test_disconnect_safe_when_not_connected(self):
        pip = OpentronsPipette(pipette_model="p300_single_gen2", port="/dev/null")
        pip.disconnect()  # Should not raise

    def test_health_check_false_when_not_connected(self):
        pip = OpentronsPipette(pipette_model="p300_single_gen2", port="/dev/null")
        assert pip.health_check() is False

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_health_check_true_when_connected(self, mock_sleep, mock_serial_cls):
        # Two status calls: one for connect, one for health_check
        responses = [
            "OK:{homed:1,pos:0.0,max_vol:200}\n",
            "OK:{homed:1,pos:0.0,max_vol:200}\n",
        ]
        mock_ser = self._make_mock_serial(responses)
        mock_serial_cls.return_value = mock_ser

        pip = OpentronsPipette(pipette_model="p300_single_gen2", port="/dev/ttyUSB0")
        pip.connect()
        assert pip.health_check() is True


# --- Driver command tests (mocked serial) -------------------------------------

class TestPipetteCommands:

    def _make_connected_pipette(self, mock_serial_cls, mock_sleep, responses):
        """Helper: create a connected OpentronsPipette with mocked serial."""
        all_responses = [
            'OK:{"msg":"Hello from Pawduino!"}\n',  # link hello
            "OK:{homed:1,pos:0.0,max_vol:200}\n",
        ] + responses
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_ser.in_waiting = 0
        mock_ser.readline.side_effect = [r.encode() for r in all_responses]
        mock_serial_cls.return_value = mock_ser

        pip = OpentronsPipette(pipette_model="p300_single_gen2", port="/dev/ttyUSB0")
        pip.connect()
        return pip, mock_ser

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_home(self, mock_sleep, mock_serial_cls):
        pip, mock_ser = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            ["OK:{homed:1,pos:0.0}\n"],
        )
        pip.home()
        written = [c[0][0].decode() for c in mock_ser.write.call_args_list]
        assert any("10" in cmd for cmd in written)

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_prime(self, mock_sleep, mock_serial_cls):
        pip, mock_ser = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            ["OK:{pos:36.0}\n"],
        )
        pip.prime()
        written = [c[0][0].decode() for c in mock_ser.write.call_args_list]
        assert any("11" in cmd for cmd in written)

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_aspirate_returns_result(self, mock_sleep, mock_serial_cls):
        pip, _ = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            ["OK:{pos:46.98}\n"],
        )
        result = pip.aspirate(100.0)
        assert isinstance(result, AspirateResult)
        assert result.success is True
        assert result.volume_ul == 100.0
        assert result.position_mm == pytest.approx(46.98)

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_dispense_returns_result(self, mock_sleep, mock_serial_cls):
        pip, _ = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            ["OK:{pos:36.0}\n"],
        )
        result = pip.dispense(100.0)
        assert isinstance(result, AspirateResult)
        assert result.success is True
        assert result.volume_ul == 100.0

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_mix_cycles_between_two_heights(self, mock_sleep, mock_serial_cls):
        pip, _ = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            ["OK:{pos:36.0}\n", "OK:{pos:0.0}\n"] * 4,
        )
        gantry = MagicMock()
        result = pip.mix(50.0, cycles=2, gantry=gantry, position=(10.0, 20.0, 5.0))
        assert isinstance(result, MixResult)
        assert result.success is True
        assert result.volume_ul == 50.0
        assert result.cycles == 2
        assert gantry.move.call_args_list == [
            call(pip, (10.0, 20.0, 6.0)),
            call(pip, (10.0, 20.0, 5.0)),
        ] * 2

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_get_status(self, mock_sleep, mock_serial_cls):
        pip, _ = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            ["OK:{homed:1,pos:36.0,max_vol:200,primed:1}\n"],
        )
        status = pip.get_status()
        assert isinstance(status, PipetteStatus)
        assert status.is_homed is True
        assert status.position_mm == pytest.approx(36.0)
        assert status.is_primed is True

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_pick_up_tip_sets_flag(self, mock_sleep, mock_serial_cls):
        pip, _ = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            ["OK:{pos:0.0}\n", "OK:{homed:1,pos:0.0,max_vol:200}\n"],
        )
        pip.pick_up_tip()
        status = pip.get_status()
        assert status.has_tip is True

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_drop_tip_clears_flag(self, mock_sleep, mock_serial_cls):
        pip, _ = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            [
                "OK:{pos:0.0}\n",   # pick_up_tip
                "OK:{pos:60.0}\n",  # drop move
                "OK:{pos:36.0}\n",  # return to prime
                "OK:{homed:1,pos:36.0,max_vol:200}\n",
            ],
        )
        pip.pick_up_tip()
        pip.drop_tip()
        status = pip.get_status()
        assert status.has_tip is False

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_drop_tip_returns_plunger_to_prime(self, mock_sleep, mock_serial_cls):
        # The ejector stroke parks the plunger at the bottom of travel; the
        # driver must move it back to prime so the next cycle starts ready.
        pip, mock_ser = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            ["OK:{pos:60.0}\n", "OK:{pos:36.0}\n"],
        )
        pip.drop_tip()
        written = [c[0][0].decode().strip() for c in mock_ser.write.call_args_list]
        moves = [w for w in written if w.startswith("11,")]
        assert moves[0].startswith("11,60.0")  # drop
        assert moves[1].startswith("11,36.0")  # return to prime
        assert pip._position_mm == pytest.approx(36.0)

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_drop_tip_failed_return_to_prime_does_not_raise(
        self, mock_sleep, mock_serial_cls
    ):
        # The tip is off once the drop move succeeds; a failure returning to
        # prime must not surface as a failed drop.
        pip, _ = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            [
                "OK:{pos:0.0}\n",           # pick_up_tip
                "OK:{pos:60.0}\n",          # drop move
                "ERR:motor stall detected\n",  # return to prime fails
                "OK:{homed:1,pos:60.0,max_vol:200}\n",
            ],
        )
        pip.pick_up_tip()
        pip.drop_tip()  # must not raise
        status = pip.get_status()
        assert status.has_tip is False

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_command_error_on_err_response(self, mock_sleep, mock_serial_cls):
        # home() retries once, so it takes two ERR responses to fail.
        pip, _ = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            ["ERR:motor stall detected\n", "ERR:motor stall detected\n"],
        )
        with pytest.raises(PipetteCommandError, match="motor stall"):
            pip.home()

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_home_retries_once_when_first_attempt_falls_short(
        self, mock_sleep, mock_serial_cls
    ):
        # Firmware caps upward travel per homing attempt below full plunger
        # travel, so a plunger parked low fails once and succeeds on retry.
        pip, mock_ser = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            [
                'ERR:{"error":"Failed to home pipette"}\n',
                'OK:{"msg":"Pipette homed"}\n',
            ],
        )
        pip.home()
        written = [c[0][0].decode().strip() for c in mock_ser.write.call_args_list]
        assert [w.split(",")[0] for w in written].count("10") == 2

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_status_skips_stray_ok_lines(self, mock_sleep, mock_serial_cls):
        # A late boot banner must not be taken as the status response.
        pip, _ = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            ["OK:Ready\n", 'OK:{"homed":1,"pos":36.00,"max_vol":300.00}\n'],
        )
        status = pip.get_status()
        assert status.is_homed is True
        assert status.position_mm == pytest.approx(36.0)

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_plunger_moves_use_firmware_default_speed(
        self, mock_sleep, mock_serial_cls
    ):
        # Firmware reads the speed arg as steps/second; 0 selects its own
        # calibrated default instead of a floor-clamped crawl.
        pip, mock_ser = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            [
                "OK:{pos:36.0}\n",  # prime
                "OK:{pos:0.0}\n",   # pick_up_tip
                "OK:{pos:60.0}\n",  # drop move
                "OK:{pos:36.0}\n",  # return to prime
            ],
        )
        pip.prime()
        pip.pick_up_tip()
        pip.drop_tip()
        written = [c[0][0].decode().strip() for c in mock_ser.write.call_args_list]
        moves = [w for w in written if w.startswith("11,")]
        assert len(moves) == 4
        assert all(w.split(",")[2] == "0.0" for w in moves)

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_blowout(self, mock_sleep, mock_serial_cls):
        pip, mock_ser = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            ["OK:{pos:46.0}\n"],
        )
        pip.blowout()
        written = [c[0][0].decode() for c in mock_ser.write.call_args_list]
        assert any("46.0" in cmd for cmd in written)

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_drip_stop(self, mock_sleep, mock_serial_cls):
        pip, mock_ser = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            ["OK:{pos:36.5}\n"],
        )
        pip.drip_stop(5.0)
        written = [c[0][0].decode() for c in mock_ser.write.call_args_list]
        assert any("28" in cmd for cmd in written)

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_warm_up_homes_and_primes(self, mock_sleep, mock_serial_cls):
        pip, mock_ser = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            ["OK:{homed:1,pos:0.0}\n", "OK:{pos:36.0}\n"],
        )
        pip.warm_up()
        written = [c[0][0].decode().strip() for c in mock_ser.write.call_args_list]
        codes = [w.split(",")[0] for w in written]
        assert "10" in codes  # home
        assert "11" in codes  # move_to (prime)


# --- Offline OpentronsPipette tests ----------------------------------------------------

class TestOfflinePipette:

    def test_is_base_instrument(self):
        pip = OpentronsPipette(offline=True)
        assert isinstance(pip, BaseInstrument)

    def test_unknown_model_raises_config_error(self):
        with pytest.raises(PipetteConfigError):
            OpentronsPipette(pipette_model="p9999_fake", offline=True)

    def test_connect_disconnect_cycle(self):
        pip = OpentronsPipette(offline=True)
        pip.connect()
        assert pip.health_check() is True
        pip.disconnect()  # safe no-op in offline mode

    def test_aspirate_returns_result(self):
        pip = OpentronsPipette(offline=True)
        pip.connect()
        result = pip.aspirate(100.0)
        assert isinstance(result, AspirateResult)
        assert result.success is True
        assert result.volume_ul == 100.0

    def test_aspirate_rejects_negative_volume(self):
        pip = OpentronsPipette(offline=True)
        with pytest.raises(PipetteCommandError, match="outside"):
            pip.aspirate(-50.0)

    def test_aspirate_rejects_over_capacity_volume(self):
        pip = OpentronsPipette(offline=True)
        with pytest.raises(PipetteCommandError, match="outside"):
            pip.aspirate(pip.config.max_volume + 1.0)

    def test_dispense_returns_result(self):
        pip = OpentronsPipette(offline=True)
        pip.connect()
        result = pip.dispense(100.0)
        assert isinstance(result, AspirateResult)
        assert result.success is True

    def test_mix_returns_result(self):
        pip = OpentronsPipette(offline=True)
        pip.connect()
        result = pip.mix(50.0, cycles=5, gantry=MagicMock(), position=(0.0, 0.0, 0.0))
        assert isinstance(result, MixResult)
        assert result.cycles == 5
        assert pip.get_status().position_mm == pytest.approx(pip._config.zero_position)

    def test_mix_rejects_non_positive_cycles(self):
        pip = OpentronsPipette(offline=True)
        pip.connect()
        with pytest.raises(ValueError, match="cycles"):
            pip.mix(50.0, cycles=0, gantry=MagicMock(), position=(0.0, 0.0, 0.0))

    def test_get_status_returns_status(self):
        pip = OpentronsPipette(offline=True)
        pip.connect()
        pip.home()
        pip.prime()
        status = pip.get_status()
        assert isinstance(status, PipetteStatus)
        assert status.is_homed is True
        assert status.is_primed is True
        assert status.max_volume == 200.0

    def test_tip_tracking(self):
        pip = OpentronsPipette(offline=True)
        pip.connect()
        pip.pick_up_tip()
        status = pip.get_status()
        assert status.has_tip is True
        pip.drop_tip()
        status = pip.get_status()
        assert status.has_tip is False

    def test_attached_tip_extension_changes_effective_depth(self):
        pip = OpentronsPipette(offline=True, depth=-17.0)

        assert pip.effective_depth == pytest.approx(-17.0)

        pip.set_attached_tip_extension(70.0)
        assert pip.attached_tip_extension == pytest.approx(70.0)
        assert pip.effective_depth == pytest.approx(53.0)

        pip.drop_tip()
        assert pip.attached_tip_extension == pytest.approx(0.0)
        assert pip.effective_depth == pytest.approx(-17.0)

    def test_warm_up_homes_and_primes(self):
        pip = OpentronsPipette(offline=True)
        pip.connect()
        pip.warm_up()
        status = pip.get_status()
        assert status.is_homed is True
        assert status.is_primed is True

    def test_disconnect_safe_when_not_connected(self):
        pip = OpentronsPipette(offline=True)
        pip.disconnect()  # Should not raise

    def test_config_property(self):
        pip = OpentronsPipette(pipette_model="flex_1channel_1000", offline=True)
        assert pip.config.family == PipetteFamily.FLEX
        assert pip.config.max_volume == 1000.0


# --- Liquid-class correction tests --------------------------------------------


class TestLiquidClassCorrection:

    def test_identity_by_default(self):
        pip = OpentronsPipette(offline=True)
        assert pip.liquid_classes == {}
        assert pip.correction_for(None).apply(123.0) == pytest.approx(123.0)

    def test_configured_multiplier_and_offset_applied(self):
        pip = OpentronsPipette(
            offline=True,
            liquid_classes={"viscous": {"multiplier": 1.1, "offset_ul": 5.0}},
        )
        correction = pip.correction_for("viscous")
        assert correction.apply(100.0) == pytest.approx(115.0)

    def test_unknown_liquid_class_raises(self):
        from cubos.instruments.pipette.liquid_class import LiquidClassConfigError

        pip = OpentronsPipette(offline=True)
        with pytest.raises(LiquidClassConfigError, match="Unknown liquid class"):
            pip.correction_for("does_not_exist")

    def test_disabled_when_liquid_classes_omitted(self):
        pip = OpentronsPipette(offline=True)
        assert pip.correction_for(None).apply(50.0) == pytest.approx(50.0)

    def test_multiplier_must_be_positive(self):
        from cubos.instruments.pipette.liquid_class import LiquidClassConfigError

        with pytest.raises(LiquidClassConfigError):
            OpentronsPipette(
                offline=True,
                liquid_classes={"bad": {"multiplier": 0.0}},
            )

    def test_rejects_unknown_config_fields(self):
        from cubos.instruments.pipette.liquid_class import LiquidClassConfigError

        with pytest.raises(LiquidClassConfigError, match="unknown fields"):
            OpentronsPipette(
                offline=True,
                liquid_classes={"bad": {"scale": 2.0}},
            )
