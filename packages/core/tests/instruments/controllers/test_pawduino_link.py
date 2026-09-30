"""Tests for the shared, refcounted Pawduino serial link."""

import threading
from unittest.mock import MagicMock, patch

import pytest
import serial as real_serial

from cubos.instruments.controllers.pawduino import (
    PawduinoLink,
    PawduinoLinkCommandError,
    PawduinoLinkConfigError,
    PawduinoLinkConnectionError,
    PawduinoLinkTimeoutError,
)


HELLO_RESPONSE = 'OK:{"msg":"Hello from Pawduino!"}\n'


def _mock_serial(responses=None):
    """Mock serial whose first queued response answers the connect hello."""
    mock_ser = MagicMock()
    mock_ser.is_open = True
    mock_ser.in_waiting = 0
    if responses is not None:
        mock_ser.readline.side_effect = [
            r.encode() for r in [HELLO_RESPONSE, *responses]
        ]
    else:
        mock_ser.readline.return_value = HELLO_RESPONSE.encode()
    return mock_ser


class TestRegistry:
    def test_same_port_same_instance(self):
        assert PawduinoLink.acquire("/dev/ttyACM0") is PawduinoLink.acquire(
            "/dev/ttyACM0"
        )

    def test_different_ports_different_instances(self):
        assert PawduinoLink.acquire("/dev/ttyACM0") is not PawduinoLink.acquire(
            "/dev/ttyACM1"
        )

    def test_empty_port_rejected(self):
        with pytest.raises(PawduinoLinkConfigError):
            PawduinoLink.acquire("")

    def test_baud_conflict_rejected(self):
        PawduinoLink.acquire("/dev/ttyACM0", baud_rate=115200)
        with pytest.raises(PawduinoLinkConfigError, match="115200"):
            PawduinoLink.acquire("/dev/ttyACM0", baud_rate=9600)


class TestLifecycle:
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    def test_two_holders_one_open(self, mock_serial_cls, mock_sleep):
        mock_serial_cls.return_value = _mock_serial()
        link = PawduinoLink.acquire("/dev/ttyACM0")
        link.connect()
        link.connect()
        # One physical open — the second holder must not DTR-reset the board.
        mock_serial_cls.assert_called_once()
        assert link.is_open

    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    def test_closes_only_when_last_holder_leaves(self, mock_serial_cls, mock_sleep):
        mock_ser = _mock_serial()
        mock_serial_cls.return_value = mock_ser
        link = PawduinoLink.acquire("/dev/ttyACM0")
        link.connect()
        link.connect()
        link.disconnect()
        mock_ser.close.assert_not_called()
        link.disconnect()
        mock_ser.close.assert_called_once()
        assert not link.is_open

    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    def test_open_failure_counts_no_holder(self, mock_serial_cls, mock_sleep):
        mock_serial_cls.side_effect = real_serial.SerialException("busy")
        link = PawduinoLink.acquire("/dev/ttyACM0")
        with pytest.raises(PawduinoLinkConnectionError, match="Cannot open"):
            link.connect()
        assert not link.is_open
        # A later disconnect must not underflow the count.
        link.disconnect()

    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    def test_extra_disconnect_is_noop(self, mock_serial_cls, mock_sleep):
        mock_serial_cls.return_value = _mock_serial()
        link = PawduinoLink.acquire("/dev/ttyACM0")
        link.connect()
        link.disconnect()
        link.disconnect()
        assert not link.is_open


class TestCommands:
    @patch("cubos.instruments.controllers.pawduino.time.sleep")
    @patch("cubos.instruments.controllers.pawduino.serial.Serial")
    def _connected_link(self, responses, mock_serial_cls, mock_sleep):
        mock_ser = _mock_serial(responses)
        mock_serial_cls.return_value = mock_ser
        link = PawduinoLink.acquire("/dev/ttyACM0")
        link.connect()
        return link, mock_ser

    def test_command_round_trip(self):
        link, mock_ser = self._connected_link(["OK:White lights on\n"])
        assert link.send_command(17) == "OK:White lights on"
        assert mock_ser.write.call_args[0][0] == b"17\n"

    def test_command_args_serialized(self):
        link, mock_ser = self._connected_link(["OK:done\n"])
        link.send_command(11, 12.5, 0.0)
        assert mock_ser.write.call_args[0][0] == b"11,12.5,0.0\n"

    def test_err_response_raises(self):
        link, _ = self._connected_link(["ERR:jam\n"])
        with pytest.raises(PawduinoLinkCommandError, match="jam"):
            link.send_command(5)

    def test_timeout(self):
        link, mock_ser = self._connected_link(None)
        mock_ser.readline.return_value = b""
        with pytest.raises(PawduinoLinkTimeoutError):
            link.send_command(5, timeout=0.05)

    def test_not_connected_raises(self):
        link = PawduinoLink.acquire("/dev/ttyACM0")
        with pytest.raises(PawduinoLinkCommandError, match="not connected"):
            link.send_command(5)

    def test_expect_skips_stale_responses(self):
        link, _ = self._connected_link(["OK:stale thing\n", "OK:Hello\n"])
        assert link.send_command(0, expect="Hello") == "OK:Hello"

    def test_concurrent_commands_pair_correctly(self):
        # Two threads share the link; each must get the response paired to
        # its own write. The lock serializes write→readline round-trips, so
        # response N always follows write N.
        link, mock_ser = self._connected_link(None)
        sent = []
        lock = threading.Lock()

        def fake_write(payload):
            with lock:
                sent.append(payload)

        def fake_readline():
            with lock:
                return f"OK:reply-to-{sent[-1].decode().strip()}\n".encode()

        mock_ser.write.side_effect = fake_write
        mock_ser.readline.side_effect = fake_readline

        results = {}

        def worker(code):
            results[code] = link.send_command(code)

        threads = [threading.Thread(target=worker, args=(c,)) for c in (17, 23)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results[17] == "OK:reply-to-17"
        assert results[23] == "OK:reply-to-23"


class TestErrorPaths:
    def _connected(self):
        with patch("cubos.instruments.controllers.pawduino.serial.Serial") as cls, \
                patch("cubos.instruments.controllers.pawduino.time.sleep"):
            mock_ser = _mock_serial()
            cls.return_value = mock_ser
            link = PawduinoLink.acquire("/dev/ttyACM0")
            link.connect()
        return link, mock_ser

    def test_port_property(self):
        assert PawduinoLink.acquire("/dev/ttyACM9").port == "/dev/ttyACM9"

    def test_write_failure_wrapped(self):
        link, mock_ser = self._connected()
        mock_ser.write.side_effect = real_serial.SerialException("gone")
        with pytest.raises(PawduinoLinkCommandError, match="Failed to send"):
            link.send_command(5)

    def test_read_failure_wrapped(self):
        link, mock_ser = self._connected()
        mock_ser.readline.side_effect = real_serial.SerialException("gone")
        with pytest.raises(PawduinoLinkCommandError, match="read error"):
            link.send_command(5)

    def test_close_swallows_serial_exception(self):
        link, mock_ser = self._connected()
        mock_ser.close.side_effect = real_serial.SerialException("stuck")
        link.disconnect()
        assert not link.is_open

    def test_reset_registry_force_closes_open_links(self):
        link, mock_ser = self._connected()
        PawduinoLink.reset_registry()
        mock_ser.close.assert_called_once()
        assert not link.is_open

    def test_hello_resyncs_past_late_boot_banner(self):
        # A banner arriving after the drain must not become the first
        # command's response: hello absorbs it, so replies pair correctly.
        with patch("cubos.instruments.controllers.pawduino.serial.Serial") as cls, \
                patch("cubos.instruments.controllers.pawduino.time.sleep"):
            mock_ser = MagicMock()
            mock_ser.is_open = True
            mock_ser.in_waiting = 0
            mock_ser.readline.side_effect = [
                b"OK:Ready\n",              # late boot banner
                HELLO_RESPONSE.encode(),
                b"OK:White lights on\n",
            ]
            cls.return_value = mock_ser
            link = PawduinoLink.acquire("/dev/ttyACM0")
            link.connect()
            assert link.send_command(17) == "OK:White lights on"

    def test_connect_fails_when_hello_unanswered(self):
        with patch("cubos.instruments.controllers.pawduino.serial.Serial") as cls, \
                patch("cubos.instruments.controllers.pawduino.time.sleep"):
            mock_ser = MagicMock()
            mock_ser.is_open = True
            mock_ser.in_waiting = 0
            mock_ser.readline.return_value = b""
            cls.return_value = mock_ser
            link = PawduinoLink.acquire("/dev/ttyACM0")
            with pytest.raises(
                PawduinoLinkConnectionError, match="did not answer hello",
            ):
                link.connect(timeout=0.05)
            mock_ser.close.assert_called_once()
            assert not link.is_open

    def test_drain_resets_pending_input(self):
        class Waiting:
            def __init__(self):
                self.calls = 0

            def __get__(self, obj, objtype=None):
                self.calls += 1
                return 1 if self.calls == 1 else 0

        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_ser.readline.return_value = HELLO_RESPONSE.encode()
        type(mock_ser).in_waiting = Waiting()
        with patch("cubos.instruments.controllers.pawduino.serial.Serial") as cls, \
                patch("cubos.instruments.controllers.pawduino.time.sleep"):
            cls.return_value = mock_ser
            link = PawduinoLink.acquire("/dev/ttyACM0")
            link.connect()
        mock_ser.reset_input_buffer.assert_called_once()
