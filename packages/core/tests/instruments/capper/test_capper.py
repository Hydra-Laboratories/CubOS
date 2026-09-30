"""Tests for the generic capper interface, mock vendor, and pawduino vendor."""

from unittest.mock import MagicMock, patch

import pytest

from cubos.instruments.base_instrument import BaseInstrument, InstrumentError
from cubos.instruments.capper.exceptions import (
    CapperCommandError,
    CapperConfigError,
    CapperConnectionError,
    CapperError,
    CapperSensorFault,
    CapperTimeoutError,
)
from cubos.instruments.capper.models import CapperStatus
from cubos.instruments.capper.vendors.mock import MockCapper
from cubos.instruments.controllers.pawduino import PawduinoLink
from cubos.instruments.capper.vendors.pawduino import PawduinoCapper


def _mock_capper(**overrides):
    kwargs = dict(engage_depth_mm=-10.0)
    kwargs.update(overrides)
    return MockCapper(**kwargs)


# --- Interface / config validation --------------------------------------------


class TestCapperInterfaceConfig:
    def test_is_base_instrument(self):
        assert issubclass(MockCapper, BaseInstrument)

    def test_stores_config(self):
        capper = _mock_capper(
            engage_depth_mm=-12.5, capture_retries=5, capture_settle_s=0.25,
        )
        assert capper.engage_depth_mm == -12.5
        assert capper.capture_retries == 5
        assert capper.capture_settle_s == 0.25

    def test_rejects_non_finite_engage_depth(self):
        with pytest.raises(CapperConfigError):
            _mock_capper(engage_depth_mm=float("nan"))

    def test_rejects_negative_capture_retries(self):
        with pytest.raises(CapperConfigError):
            _mock_capper(capture_retries=-1)

    def test_rejects_negative_capture_settle(self):
        with pytest.raises(CapperConfigError):
            _mock_capper(capture_settle_s=-0.1)

    def test_rejects_bool_engage_depth(self):
        with pytest.raises(CapperConfigError):
            _mock_capper(engage_depth_mm=True)


class TestCapperExceptions:
    def test_capper_error_is_instrument_error(self):
        assert issubclass(CapperError, InstrumentError)

    def test_hierarchy(self):
        for exc_cls in (
            CapperConnectionError, CapperCommandError, CapperTimeoutError,
            CapperConfigError, CapperSensorFault,
        ):
            assert issubclass(exc_cls, CapperError)


class TestCapperStatus:
    def test_valid(self):
        status = CapperStatus(cap_present=True)
        assert status.is_valid is True

    def test_frozen(self):
        status = CapperStatus(cap_present=False)
        with pytest.raises(AttributeError):
            status.cap_present = True


# --- MockCapper ----------------------------------------------------------------


class TestMockCapper:
    def test_starts_without_cap(self):
        capper = _mock_capper()
        assert capper.read_cap_present() is False

    def test_capture_then_read(self):
        capper = _mock_capper()
        capper.capture_cap()
        assert capper.read_cap_present() is True

    def test_release_then_read(self):
        capper = _mock_capper()
        capper.capture_cap()
        capper.release_cap()
        assert capper.read_cap_present() is False

    def test_actuation_log_order(self):
        capper = _mock_capper()
        capper.capture_cap()
        capper.read_cap_present()
        capper.release_cap()
        capper.read_cap_present()
        assert capper.actuation_log == [
            "capture_cap", "read_cap_present", "release_cap", "read_cap_present",
        ]

    def test_get_status_reflects_state(self):
        capper = _mock_capper()
        capper.capture_cap()
        assert capper.get_status() == CapperStatus(cap_present=True)

    def test_connect_disconnect_health_check(self):
        capper = _mock_capper()
        capper.connect()
        assert capper.health_check() is True
        capper.disconnect()

    def test_offline_default_true(self):
        capper = _mock_capper()
        assert capper._offline is True


# --- PawduinoCapper (offline) ---------------------------------------------------


class TestPawduinoCapperOffline:
    def _make(self, **overrides):
        kwargs = dict(engage_depth_mm=-10.0, offline=True)
        kwargs.update(overrides)
        return PawduinoCapper(**kwargs)

    def test_capture_and_read(self):
        capper = self._make()
        capper.capture_cap()
        assert capper.read_cap_present() is True

    def test_release_and_read(self):
        capper = self._make()
        capper.capture_cap()
        capper.release_cap()
        assert capper.read_cap_present() is False

    def test_connect_disconnect_offline(self):
        capper = self._make()
        capper.connect()
        assert capper.health_check() is True
        capper.disconnect()


# --- PawduinoCapper serial protocol (mocked) ------------------------------------


class TestPawduinoCapperSerial:
    def _make_mock_serial(self, responses):
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_ser.in_waiting = 0
        mock_ser.readline.side_effect = [r.encode() for r in responses]
        return mock_ser

    def _attach_link(self, capper, mock_ser):
        """Hand the capper a pre-opened shared link (bypassing connect)."""
        link = PawduinoLink.acquire(capper._port, capper._baud_rate)
        link._serial = mock_ser
        link._holders = 1
        capper._link = link

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_connect_sends_line_break_handshake(self, mock_sleep, mock_serial_cls):
        mock_ser = self._make_mock_serial(
            ['OK:{"msg":"Hello from Pawduino!"}\n', 'OK:{"value1":0}\n'],
        )
        mock_serial_cls.return_value = mock_ser

        capper = PawduinoCapper(
            engage_depth_mm=-10.0, port="/dev/ttyUSB0",
        )
        capper.connect()
        mock_serial_cls.assert_called_once_with(
            port="/dev/ttyUSB0", baudrate=115200, timeout=30.0,
        )

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_connect_raises_on_serial_error(self, mock_sleep, mock_serial_cls):
        import serial as real_serial
        mock_serial_cls.side_effect = real_serial.SerialException("port busy")

        capper = PawduinoCapper(
            engage_depth_mm=-10.0, port="/dev/ttyUSB0",
        )
        with pytest.raises(CapperConnectionError, match="Cannot open serial"):
            capper.connect()

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_capture_cap_sends_emag_on(self, mock_sleep, mock_serial_cls):
        mock_ser = self._make_mock_serial(["OK:Electromagnet on\n"])
        mock_serial_cls.return_value = mock_ser
        capper = PawduinoCapper(
            engage_depth_mm=-10.0, port="/dev/ttyUSB0",
        )
        self._attach_link(capper, mock_ser)
        capper.capture_cap()
        sent = mock_ser.write.call_args[0][0]
        assert sent == b"5\n"

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_release_cap_sends_emag_off(self, mock_sleep, mock_serial_cls):
        mock_ser = self._make_mock_serial(["OK:Electromagnet off\n"])
        mock_serial_cls.return_value = mock_ser
        capper = PawduinoCapper(
            engage_depth_mm=-10.0, port="/dev/ttyUSB0",
        )
        self._attach_link(capper, mock_ser)
        capper.release_cap()
        sent = mock_ser.write.call_args[0][0]
        assert sent == b"6\n"

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_read_cap_present_true_on_broken_beam(self, mock_sleep, mock_serial_cls):
        mock_ser = self._make_mock_serial(['OK:{"value1":1}\n'])
        mock_serial_cls.return_value = mock_ser
        capper = PawduinoCapper(
            engage_depth_mm=-10.0, port="/dev/ttyUSB0",
        )
        self._attach_link(capper, mock_ser)
        assert capper.read_cap_present() is True
        sent = mock_ser.write.call_args[0][0]
        assert sent == b"7\n"

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_read_cap_present_false_on_unbroken_beam(self, mock_sleep, mock_serial_cls):
        mock_ser = self._make_mock_serial(['OK:{"value1":0}\n'])
        mock_serial_cls.return_value = mock_ser
        capper = PawduinoCapper(
            engage_depth_mm=-10.0, port="/dev/ttyUSB0",
        )
        self._attach_link(capper, mock_ser)
        assert capper.read_cap_present() is False

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_command_error_on_err_response(self, mock_sleep, mock_serial_cls):
        mock_ser = self._make_mock_serial(["ERR:jam\n"])
        mock_serial_cls.return_value = mock_ser
        capper = PawduinoCapper(
            engage_depth_mm=-10.0, port="/dev/ttyUSB0",
        )
        self._attach_link(capper, mock_ser)
        with pytest.raises(CapperCommandError, match="jam"):
            capper.capture_cap()

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_timeout_when_no_response(self, mock_sleep, mock_serial_cls):
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_ser.in_waiting = 0
        mock_ser.readline.return_value = b""
        mock_serial_cls.return_value = mock_ser
        capper = PawduinoCapper(
            engage_depth_mm=-10.0, port="/dev/ttyUSB0",
            command_timeout=0.05,
        )
        self._attach_link(capper, mock_ser)
        with pytest.raises(CapperTimeoutError):
            capper.capture_cap()

    def test_command_error_when_not_connected(self):
        capper = PawduinoCapper(
            engage_depth_mm=-10.0, port="/dev/ttyUSB0",
        )
        with pytest.raises(CapperCommandError, match="Not connected"):
            capper.capture_cap()

    def test_parse_line_break_unquoted_key_form(self):
        # Documents the parser also tolerates an unquoted-key form, in case a
        # future firmware revision aligns it with the pipette-status format.
        assert PawduinoCapper._parse_line_break_response("OK:{value1:1}") is True
        assert PawduinoCapper._parse_line_break_response("OK:{value1:0}") is False

    def test_parse_line_break_missing_field_raises_sensor_fault(self):
        with pytest.raises(CapperSensorFault):
            PawduinoCapper._parse_line_break_response("OK:{}")

    def test_parse_line_break_bad_value_raises_sensor_fault(self):
        with pytest.raises(CapperSensorFault):
            PawduinoCapper._parse_line_break_response('OK:{"value1":"bad"}')


# --- PawduinoCapper link lifecycle (mocked serial) -------------------------------


class TestPawduinoCapperLinkLifecycle:
    def _make_mock_serial(self, responses):
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_ser.in_waiting = 0
        mock_ser.readline.side_effect = [r.encode() for r in responses]
        return mock_ser

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_disconnect_releases_link(self, mock_sleep, mock_serial_cls):
        mock_ser = self._make_mock_serial(
            ['OK:{"msg":"Hello from Pawduino!"}\n', 'OK:{"value1":0}\n'],
        )
        mock_serial_cls.return_value = mock_ser
        capper = PawduinoCapper(
            engage_depth_mm=-10.0, port="/dev/ttyUSB0",
        )
        capper.connect()
        capper.disconnect()
        assert capper._link is None
        mock_ser.close.assert_called_once()
        assert capper.health_check() is False

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_probe_failure_releases_link(self, mock_sleep, mock_serial_cls):
        mock_ser = self._make_mock_serial(
            ['OK:{"msg":"Hello from Pawduino!"}\n', "ERR:dead\n"],
        )
        mock_serial_cls.return_value = mock_ser
        capper = PawduinoCapper(
            engage_depth_mm=-10.0, port="/dev/ttyUSB0",
        )
        with pytest.raises(CapperConnectionError, match="did not respond"):
            capper.connect()
        assert capper._link is None
        mock_ser.close.assert_called_once()

    def test_empty_port_rejected(self):
        capper = PawduinoCapper(
            engage_depth_mm=-10.0, port="",
        )
        with pytest.raises(CapperConnectionError, match="non-empty"):
            capper.connect()

    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    def test_shares_link_with_pipette_on_same_port(
        self, mock_sleep, mock_serial_cls,
    ):
        from cubos.instruments.pipette.vendors.opentrons import OpentronsPipette

        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_ser.in_waiting = 0
        mock_ser.readline.side_effect = [
            b'OK:{"msg":"Hello from Pawduino!"}\n',      # link hello
            b'OK:{"value1":0}\n',                       # capper handshake
            b'OK:{"homed":1,"pos":0.0,"max_vol":300}\n',  # pipette status
            b'OK:{"homed":1,"pos":0.0,"max_vol":300}\n',  # pipette primed check
        ]
        mock_serial_cls.return_value = mock_ser

        capper = PawduinoCapper(
            engage_depth_mm=-10.0, port="/dev/ttyUSB0",
        )
        pipette = OpentronsPipette(port="/dev/ttyUSB0")
        capper.connect()
        pipette.connect()
        # One physical open for both instruments: the second connect must not
        # DTR-reset the Arduino out from under the first.
        mock_serial_cls.assert_called_once()
        assert capper._link is pipette._link
        pipette.disconnect()
        mock_ser.close.assert_not_called()
        capper.disconnect()
        mock_ser.close.assert_called_once()
