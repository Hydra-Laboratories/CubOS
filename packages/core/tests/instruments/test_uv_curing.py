"""Tests for the Excelitas OmniCure S1500 PRO UV curing driver."""

import unittest
from unittest.mock import patch

from cubos.instruments.base_instrument import BaseInstrument
from cubos.instruments.uv_curing.vendors.excelitas import ExcelitasUVCuring
from cubos.instruments.uv_curing.exceptions import (
    UVCuringError,
    UVCuringConnectionError,
    UVCuringCommandError,
    UVCuringTimeoutError,
)


class TestExceptionHierarchy(unittest.TestCase):


    def test_timeout_error(self):
        self.assertTrue(issubclass(UVCuringTimeoutError, UVCuringError))


class TestUVCuringIsBaseInstrument(unittest.TestCase):

    def test_is_subclass(self):
        self.assertTrue(issubclass(ExcelitasUVCuring, BaseInstrument))


class TestUVCuringOffline(unittest.TestCase):

    def setUp(self):
        self.uv = ExcelitasUVCuring(offline=True, default_intensity=50.0,
                           default_exposure_time=2.0)

    def test_connect_disconnect(self):
        self.uv.connect()
        self.uv.disconnect()

    def test_health_check(self):
        self.assertTrue(self.uv.health_check())


    def test_cure_rejects_zero_intensity(self):
        with self.assertRaises(UVCuringCommandError):
            self.uv.cure(intensity=0)

    def test_cure_rejects_intensity_over_100(self):
        with self.assertRaises(UVCuringCommandError):
            self.uv.cure(intensity=101)

    def test_cure_rejects_zero_exposure(self):
        with self.assertRaises(UVCuringCommandError):
            self.uv.cure(exposure_time=0)

    def test_cure_rejects_exposure_below_tenth_second(self):
        with self.assertRaises(UVCuringCommandError):
            self.uv.cure(exposure_time=0.05)


class TestUVCuringOnlineGuards(unittest.TestCase):

    def test_health_check_false_without_connect(self):
        uv = ExcelitasUVCuring(offline=False)
        self.assertFalse(uv.health_check())

    def test_send_command_raises_without_connect(self):
        uv = ExcelitasUVCuring(offline=False)
        with self.assertRaises(UVCuringCommandError):
            uv._send_command("CONN")


class TestUVCuringMockedSerial(unittest.TestCase):


    @patch('cubos.instruments.uv_curing.vendors.excelitas.serial.Serial')
    def test_connect_raises_on_serial_error(self, mock_serial_cls):
        mock_serial_cls.side_effect = serial.SerialException("no port")
        uv = ExcelitasUVCuring(port="/dev/fake", offline=False)
        with self.assertRaises(UVCuringConnectionError):
            uv.connect()


    @patch('cubos.instruments.uv_curing.vendors.excelitas.serial.Serial')
    def test_send_command_timeout(self, mock_serial_cls):
        mock_ser = mock_serial_cls.return_value
        mock_ser.is_open = True
        mock_ser.readline.return_value = b"READY\r\n"
        uv = ExcelitasUVCuring(offline=False)
        uv.connect()
        mock_ser.readline.return_value = b""
        with self.assertRaises(UVCuringTimeoutError):
            uv._send_command("SIL50")

    @patch('cubos.instruments.uv_curing.vendors.excelitas.serial.Serial')
    def test_disconnect_closes_serial(self, mock_serial_cls):
        mock_ser = mock_serial_cls.return_value
        mock_ser.is_open = True
        mock_ser.readline.return_value = b"READY\r\n"
        uv = ExcelitasUVCuring(offline=False)
        uv.connect()
        uv.disconnect()
        mock_ser.close.assert_called_once()

    @patch('cubos.instruments.uv_curing.vendors.excelitas.time.sleep')
    @patch('cubos.instruments.uv_curing.vendors.excelitas.serial.Serial')
    def test_cure_rounds_exposure_to_tenths(self, mock_serial_cls, mock_sleep):
        mock_ser = mock_serial_cls.return_value
        mock_ser.is_open = True
        mock_ser.readline.side_effect = [
            b"READY\r\n",
            b"OK\r\n",
            b"OK\r\n",
            b"OK\r\n",
        ]
        uv = ExcelitasUVCuring(offline=False)
        uv.connect()
        mock_ser.reset_mock()

        uv.cure(intensity=50, exposure_time=1.99)

        written = [call[0][0] for call in mock_ser.write.call_args_list]
        self.assertIn(b"STM20XX\r", written)

    @patch('cubos.instruments.uv_curing.vendors.excelitas.serial.Serial')
    def test_failed_handshake_closes_port_and_health_check_false(self, mock_serial_cls):
        mock_ser = mock_serial_cls.return_value
        mock_ser.is_open = True
        mock_ser.readline.return_value = b"NOPE\r\n"
        uv = ExcelitasUVCuring(offline=False)

        with self.assertRaises(UVCuringConnectionError):
            uv.connect()

        mock_ser.close.assert_called_once()
        self.assertFalse(uv.health_check())

    @patch('cubos.instruments.uv_curing.vendors.excelitas.time.sleep')
    @patch('cubos.instruments.uv_curing.vendors.excelitas.serial.Serial')
    def test_sil_nack_raises_command_error(self, mock_serial_cls, mock_sleep):
        mock_ser = mock_serial_cls.return_value
        mock_ser.is_open = True
        mock_ser.readline.side_effect = [
            b"READY\r\n",
            b"NACK\r\n",
        ]
        uv = ExcelitasUVCuring(offline=False)
        uv.connect()

        with self.assertRaises(UVCuringCommandError):
            uv.cure(intensity=50, exposure_time=1.0)


import serial  # needed for SerialException in test
