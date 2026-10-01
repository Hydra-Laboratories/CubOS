import pytest
from unittest.mock import patch, MagicMock, PropertyMock, call

from cubos.instruments.base_instrument import InstrumentError
from cubos.instruments.pipette.models import (
    PipetteStatus,
    MixResult,
)
from cubos.instruments.pipette.exceptions import (
    PipetteError,
    PipetteConnectionError,
    PipetteCommandError,
    PipetteConfigError,
)
from cubos.instruments.pipette.vendors.opentrons import OpentronsPipette


# --- Model tests --------------------------------------------------------------


class TestPipetteStatus:


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


# --- Exception hierarchy tests ------------------------------------------------

class TestExceptions:


    def test_connection_error_hierarchy(self):
        assert issubclass(PipetteConnectionError, PipetteError)
        err = PipetteConnectionError("port not found")
        assert isinstance(err, InstrumentError)


# --- Parsing tests ------------------------------------------------------------

class TestParsing:


    def test_parse_key_value_quoted_keys(self):
        # Exact format the Pawduino firmware sends for CMD_PIPETTE_STATUS.
        response = 'OK:{"homed":0,"pos":0.00,"max_vol":300.00}'
        result = OpentronsPipette._parse_key_value(response)
        assert result["homed"] == 0.0
        assert result["pos"] == pytest.approx(0.0)
        assert result["max_vol"] == pytest.approx(300.0)


# --- Driver constructor tests -------------------------------------------------

class TestPipetteConstructor:

    def test_unknown_model_raises_config_error(self):
        with pytest.raises(PipetteConfigError, match="Unknown pipette model"):
            OpentronsPipette(pipette_model="p9999_fake", port="/dev/null")


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
    def test_drip_stop(self, mock_sleep, mock_serial_cls):
        pip, mock_ser = self._make_connected_pipette(
            mock_serial_cls, mock_sleep,
            ["OK:{pos:36.5}\n"],
        )
        pip.drip_stop(5.0)
        written = [c[0][0].decode() for c in mock_ser.write.call_args_list]
        assert any("28" in cmd for cmd in written)


# --- Offline OpentronsPipette tests ----------------------------------------------------

class TestOfflinePipette:


    def test_unknown_model_raises_config_error(self):
        with pytest.raises(PipetteConfigError):
            OpentronsPipette(pipette_model="p9999_fake", offline=True)

    def test_connect_disconnect_cycle(self):
        pip = OpentronsPipette(offline=True)
        pip.connect()
        assert pip.health_check() is True
        pip.disconnect()  # safe no-op in offline mode


    def test_aspirate_rejects_negative_volume(self):
        pip = OpentronsPipette(offline=True)
        with pytest.raises(PipetteCommandError, match="outside"):
            pip.aspirate(-50.0)

    def test_aspirate_rejects_over_capacity_volume(self):
        pip = OpentronsPipette(offline=True)
        with pytest.raises(PipetteCommandError, match="outside"):
            pip.aspirate(pip.config.max_volume + 1.0)


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


    def test_attached_tip_extension_changes_effective_depth(self):
        pip = OpentronsPipette(offline=True, depth=-17.0)

        assert pip.effective_depth == pytest.approx(-17.0)

        pip.set_attached_tip_extension(70.0)
        assert pip.attached_tip_extension == pytest.approx(70.0)
        assert pip.effective_depth == pytest.approx(53.0)

        pip.drop_tip()
        assert pip.attached_tip_extension == pytest.approx(0.0)
        assert pip.effective_depth == pytest.approx(-17.0)


    def test_disconnect_safe_when_not_connected(self):
        pip = OpentronsPipette(offline=True)
        pip.disconnect()  # Should not raise


# --- Liquid-class correction tests --------------------------------------------


class TestLiquidClassCorrection:


    def test_unknown_liquid_class_raises(self):
        from cubos.instruments.pipette.liquid_class import LiquidClassConfigError

        pip = OpentronsPipette(offline=True)
        with pytest.raises(LiquidClassConfigError, match="Unknown liquid class"):
            pip.correction_for("does_not_exist")


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
