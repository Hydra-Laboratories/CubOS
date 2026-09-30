import inspect
import unittest
from unittest.mock import MagicMock, call, patch
import sys
from pathlib import Path
import math

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

from cubos.gantry.coordinates import Coordinates
from cubos.gantry.gantry_driver.driver import (
    Mill,
    looks_like_connection_loss,
    mpos_pattern,
    wpos_pattern,
)
from cubos.gantry.gantry_driver.exceptions import (
    CommandExecutionError,
    LocationNotFound,
    MillConnectionError,
    StatusReturnError,
)
from tests.gantry.fake_serial import FakeGrblSerial


class ScriptedSerial:
    def __init__(self, lines=None):
        self.lines = list(lines or [])
        self.writes = []
        self.is_open = True
        self.port = "/dev/mock"

    def write(self, data):
        self.writes.append(data)

    def readline(self):
        if self.lines:
            return self.lines.pop(0)
        return b""

    def readlines(self):
        lines = self.lines
        self.lines = []
        return lines

    def flush(self):
        pass

    def read_all(self):
        return b""

    def close(self):
        self.is_open = False

class TestCNCDriverLogic(unittest.TestCase):
    
    def test_regex_patterns(self):
        """Test that regex patterns correctly parse GRBL status strings."""
        
        # Test WPos pattern
        wpos_status = "<Idle|WPos:10.500,20.123,-5.000|FS:0,0|WCO:0,0,0>"
        match = wpos_pattern.search(wpos_status)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "10.500")
        self.assertEqual(match.group(2), "20.123")
        self.assertEqual(match.group(3), "-5.000")
        
        # Test MPos pattern
        mpos_status = "<Idle|MPos:-400.123,-299.999,-10.000|FS:0,0>"
        match = mpos_pattern.search(mpos_status)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "-400.123")
        self.assertEqual(match.group(2), "-299.999")
        self.assertEqual(match.group(3), "-10.000")
        
        # Test failure cases
        invalid_status = "<Idle|FS:0,0>"
        self.assertIsNone(wpos_pattern.search(invalid_status))
        self.assertIsNone(mpos_pattern.search(invalid_status))

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_verify_connection_accepts_grbl_settings_response(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        mill = Mill()
        fake = FakeGrblSerial()

        self.assertTrue(mill._verify_connection(fake))
        self.assertIn(b"?", fake.writes)
        self.assertIn(b"$$\n", fake.writes)

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_verify_connection_opens_closed_serial_and_rejects_noise(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        mill = Mill()
        fake = FakeGrblSerial(is_open=False)
        fake.write = MagicMock(return_value=1)
        fake.readlines = MagicMock(return_value=[b"noise\r\n"])

        self.assertFalse(mill._verify_connection(fake))
        self.assertTrue(fake.is_open)

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_locate_over_serial_uses_priority_port_before_scan(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        class _Port:
            device = "/dev/scanned"

        created: list[FakeGrblSerial] = []

        def factory(port=None, baudrate=None, timeout=None):
            if port is None:
                return FakeGrblSerial(port="/dev/unopened")
            fake = FakeGrblSerial(port=port, status="<Idle|WPos:0,0,0|FS:0,0>")
            created.append(fake)
            return fake

        mock_serial.side_effect = factory
        mill = Mill()
        mill._get_available_ports = MagicMock(return_value=[_Port()])

        serial_obj, port = mill._locate_over_serial("/dev/priority")

        self.assertIs(serial_obj, created[0])
        self.assertEqual(port, "/dev/priority")
        self.assertEqual(created[0].port, "/dev/priority")

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_locate_over_serial_errors_when_no_ports(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        mill = Mill()
        mill._get_available_ports = MagicMock(return_value=[])

        with self.assertRaises(MillConnectionError):
            mill._locate_over_serial()

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_locate_over_serial_closes_failed_candidates(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        class _Port:
            device = "/dev/bad"

        fake = FakeGrblSerial(port="/dev/bad")
        mock_serial.side_effect = [FakeGrblSerial(), fake]
        mill = Mill()
        mill._get_available_ports = MagicMock(return_value=[_Port()])
        mill._verify_connection = MagicMock(return_value=False)

        with self.assertRaises(MillConnectionError):
            mill._locate_over_serial()

        self.assertFalse(fake.is_open)

    @patch('cubos.gantry.gantry_driver.driver.os.name', 'posix')
    @patch('cubos.gantry.gantry_driver.driver.serial.tools.list_ports.grep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_get_available_ports_posix_and_windows_and_unsupported(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_grep,
    ):
        mill = Mill()
        mock_grep.return_value = ["port"]
        self.assertEqual(mill._get_available_ports(), ["port"])
        mock_grep.assert_called_with("ttyUSB|usbmodem|usbserial")

        with patch('cubos.gantry.gantry_driver.driver.os.name', 'nt'):
            self.assertEqual(mill._get_available_ports(), ["port"])
            mock_grep.assert_called_with("COM")
        with patch('cubos.gantry.gantry_driver.driver.os.name', 'java'):
            with self.assertRaises(OSError):
                mill._get_available_ports()

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_connect_boot_alarm_returns_before_config_setup(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        fake = FakeGrblSerial(status="<Alarm|WPos:0,0,0|FS:0,0>")
        mill = Mill()
        mill.read_config = MagicMock()
        mill.clear_buffers = MagicMock()
        mill.enforce_wpos_mode = MagicMock()
        mill.set_feed_rate = MagicMock()
        mill.seed_wco = MagicMock()
        with patch.object(Mill, '_locate_over_serial', return_value=(fake, '/dev/test')):
            self.assertIs(mill.connect('/dev/test'), fake)

        mill.read_config.assert_not_called()
        mill.enforce_wpos_mode.assert_not_called()

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_connect_opens_new_serial_when_detection_returns_closed(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        detected = FakeGrblSerial(is_open=False)
        opened = FakeGrblSerial(status="<Idle|WPos:0,0,0|WCO:0,0,0|FS:0,0>")
        mock_serial.return_value = opened
        mill = Mill()
        mill.read_config = MagicMock(return_value={})
        mill.clear_buffers = MagicMock()
        mill.enforce_wpos_mode = MagicMock()
        mill.set_feed_rate = MagicMock()
        mill.seed_wco = MagicMock()
        with patch.object(Mill, '_locate_over_serial', return_value=(detected, '/dev/test')):
            mill.connect('/dev/test')

        mock_serial.assert_called_with(port='/dev/test', baudrate=115200, timeout=3)
        self.assertIs(mill.ser_mill, opened)

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_basic_serial_helpers_and_grbl_parsing(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        mill = Mill(port="/dev/default")
        self.assertEqual(mill.connected_port(), "/dev/default")
        self.assertIsNone(mill.get_read_timeout())
        mill.disconnect()

        mill.ser_mill = FakeGrblSerial(port="/dev/live", timeout=1)
        self.assertEqual(mill.connected_port(), "/dev/live")
        mill.set_read_timeout(7)
        self.assertEqual(mill.get_read_timeout(), 7)

        settings = mill._parse_grbl_settings_response([
            "",
            "Grbl 1.1h",
            "[MSG:hello]",
            "not-a-setting",
            "$10=0 (status report)",
            "$20=1",
        ])
        self.assertEqual(settings, {"$10": "0", "$20": "1"})

        self.assertEqual(mill.execute_command("$$")["$10"], "0")
        self.assertEqual(mill.set_grbl_setting("20", "0"), "ok")
        mill.clear_buffers()
        self.assertEqual(mill.ser_mill.in_waiting, 0)

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_realtime_jog_unlock_reset_and_raw_status_paths(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        mill = Mill()
        mill.ser_mill = FakeGrblSerial(jog_response="error:8")

        mill.jog()
        mill.jog(x=1.0, z=-2.0)
        mill.jog_cancel()
        mill.unlock()
        mill.soft_reset()
        mill.soft_reset_and_unlock()
        raw = mill.query_raw_status()

        self.assertIn("<", raw)
        self.assertIn(b"\x85", mill.ser_mill.writes)
        self.assertIn(b"$X\n", mill.ser_mill.writes)
        self.assertIn(b"\x18", mill.ser_mill.writes)

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_unlock_error_timeout_stop_failure_and_settings_error(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        mill = Mill()
        mill.ser_mill = FakeGrblSerial()
        mill.ser_mill.queue_line("error:9")
        with self.assertRaises(CommandExecutionError):
            mill.unlock()

        mill.ser_mill = FakeGrblSerial()
        mill.ser_mill.write = MagicMock(return_value=1)
        with patch('cubos.gantry.gantry_driver.driver.time.time', side_effect=[0.0, 3.0]):
            with self.assertRaises(CommandExecutionError):
                mill.unlock()

        mill.ser_mill = FakeGrblSerial(chunks=["<Idle|WPos:0,0,0|FS:0,0>\r\n"])
        mill.ser_mill.write = MagicMock(return_value=1)
        with self.assertRaises(StatusReturnError):
            mill.stop()

        mill.ser_mill = FakeGrblSerial(chunks=["error:2\r\n"])
        with self.assertRaises(StatusReturnError):
            mill._collect_grbl_settings_response()

    def test_looks_like_connection_loss_classifies_errors(self):
        self.assertTrue(
            looks_like_connection_loss(
                CommandExecutionError("Unlock ($X) timed out waiting for ok")
            )
        )
        self.assertTrue(
            looks_like_connection_loss(OSError(6, "Device not configured"))
        )
        self.assertFalse(looks_like_connection_loss(CommandExecutionError("error:9")))
        self.assertFalse(looks_like_connection_loss("ALARM:1"))

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_soft_reset_and_unlock_reconnects_when_link_dies(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        mill = Mill()
        # Dead link: writes succeed into the void, reads never answer — the
        # signature of a controller that dropped off the USB bus.
        dead = FakeGrblSerial()
        dead.write = MagicMock(return_value=1)
        mill.ser_mill = dead
        alive = FakeGrblSerial()
        alive.queue_line("ok")

        def fake_reconnect(attempts=4, delay_s=1.0):
            mill.ser_mill = alive

        with patch.object(Mill, "reconnect", side_effect=fake_reconnect) as mock_reconnect:
            with patch(
                'cubos.gantry.gantry_driver.driver.time.time',
                side_effect=[0.0, 3.0, 10.0, 10.1, 10.2],
            ):
                mill.soft_reset_and_unlock()

        mock_reconnect.assert_called_once()
        self.assertIn(b"$X\n", alive.writes)

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_reconnect_prefers_same_port_then_succeeds(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        mill = Mill()
        mill.ser_mill = FakeGrblSerial(port="/dev/cu.usbserial-130")

        with patch.object(
            Mill,
            "connect",
            side_effect=[MillConnectionError("device not yet enumerated"), None],
        ) as mock_connect:
            mill.reconnect(attempts=3, delay_s=0.0)

        self.assertEqual(mock_connect.call_count, 2)
        mock_connect.assert_any_call(port="/dev/cu.usbserial-130")

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_reconnect_raises_after_exhausting_attempts(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        mill = Mill()
        mill.ser_mill = FakeGrblSerial(port="/dev/cu.usbserial-130")

        with patch.object(
            Mill,
            "connect",
            side_effect=MillConnectionError("device not yet enumerated"),
        ) as mock_connect:
            with self.assertRaises(MillConnectionError) as ctx:
                mill.reconnect(attempts=2, delay_s=0.0)

        self.assertEqual(mock_connect.call_count, 2)
        self.assertIn("reconnect failed", str(ctx.exception))
        # Final attempt falls back to auto-scan in case the name changed.
        mock_connect.assert_called_with(port=None)

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_seed_wco_polls_status_until_wco_reported(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        mill = Mill()
        mill.ser_mill = FakeGrblSerial(
            chunks=["<Idle|WPos:1.0,2.0,3.0|WCO:4.0,5.0,6.0>\r\n"]
        )
        mill.seed_wco()
        self.assertIn(b"?", mill.ser_mill.writes)
        self.assertIsNotNone(mill._wco)

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_seed_wco_and_mpos_coordinate_conversion(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        mill = Mill()
        mill.ser_mill = FakeGrblSerial(status="<Idle|MPos:11.000,22.000,33.000|WCO:1.000,2.000,3.000|FS:0,0>")
        mill.config = {"$10": "1"}

        coords = mill.current_coordinates()

        self.assertEqual(coords, Coordinates(10.0, 20.0, 30.0))
        self.assertEqual(mill._wco, Coordinates(1.0, 2.0, 3.0))

        mill.ser_mill = FakeGrblSerial(status="<Idle|MPos:11.000,22.000,33.000|FS:0,0>")
        mill.config = {"$10": "1"}
        mill._wco = Coordinates(1.0, 2.0, 3.0)
        self.assertEqual(mill.current_coordinates(), Coordinates(10.0, 20.0, 30.0))

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_current_coordinates_retries_and_invalid_modes(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        mill = Mill()
        mill.ser_mill = FakeGrblSerial(chunks=[
            "ok\r\n",
            "<Idle|FS:0,0>\r\n",
            "<Idle|WPos:1.000,2.000,3.000|FS:0,0>\r\n",
        ])
        mill.config = {"$10": "0"}
        self.assertEqual(mill.current_coordinates(), Coordinates(1.0, 2.0, 3.0))

        # Parsing is mode-agnostic: an unexpected $10 value must not matter
        # as long as the frame carries a position field.
        mill.ser_mill = FakeGrblSerial(status="<Idle|WPos:1,2,3|FS:0,0>")
        mill.config = {"$10": "9"}
        self.assertEqual(mill.current_coordinates(), Coordinates(1.0, 2.0, 3.0))

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_current_coordinates_parses_mpos_even_in_wpos_mode(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        # Controller reporting MPos despite $10=0 (e.g. settings drift)
        # still yields a position via the cached WCO.
        mill = Mill()
        mill.ser_mill = FakeGrblSerial(
            status="<Idle|MPos:11.000,22.000,33.000|WCO:1.000,2.000,3.000>"
        )
        mill.config = {"$10": "0"}
        self.assertEqual(mill.current_coordinates(), Coordinates(10.0, 20.0, 30.0))

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_current_coordinates_raises_location_not_found_with_context(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        mill = Mill()
        mill.ser_mill = FakeGrblSerial(status="<Idle|FS:0,0>")
        mill.config = {"$10": "0"}
        mill._wco = None
        # seed_wco polls the same positionless frames; keep it cheap.
        mill.seed_wco = MagicMock()
        with self.assertRaises(LocationNotFound) as ctx:
            mill.current_coordinates()
        self.assertIn("No parsable position", str(ctx.exception))

    def test_mill_motion_api_has_no_driver_level_instrument_offsets(self):
        """InstrumentedGantry owns instrument offsets; Mill only receives machine coordinates."""
        signature = inspect.signature(Mill.move_to)
        self.assertNotIn("instrument", signature.parameters)

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_build_direct_move_are_axis_by_axis(self, mock_cmd_logger, mock_mill_logger, mock_serial):
        """Direct moves emit X, Y, Z on separate G-code lines — never a
        combined ``G01 X… Y…`` interpolation. The mill must not command
        simultaneous multi-axis motion so callers own every straight
        segment of the path."""
        mill = Mill()

        current = Coordinates(0.0, 0.0, 0.0)
        target = Coordinates(10.0, 20.0, -5.0)
        commands = mill._build_direct_move(current, target)

        self.assertEqual(commands, [
            "G01 X10.0 F3000",
            "G01 Y20.0 F3000",
            "G01 Z-5.0 F3000",
        ])
        # Regression guard: no combined-XY command anywhere.
        self.assertFalse(any("Y" in c and "X" in c for c in commands))

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_default_feed_rate_is_overridable_per_instance(self, mock_cmd_logger, mock_mill_logger, mock_serial):
        """cnc.default_feed_rate_mm_min overrides the module default per Mill instance."""
        mill = Mill()
        mill.default_feed_rate = 4000

        current = Coordinates(0.0, 0.0, 0.0)
        target = Coordinates(10.0, 0.0, 0.0)
        commands = mill._build_direct_move(current, target)

        self.assertEqual(commands, ["G01 X10.0 F4000"])

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_build_direct_move_skips_unchanged_axes(self, mock_cmd_logger, mock_mill_logger, mock_serial):
        """Only the axes that actually changed get a G-code line."""
        mill = Mill()

        current = Coordinates(0.0, 5.0, -5.0)
        target = Coordinates(10.0, 5.0, -5.0)  # only X changes
        commands = mill._build_direct_move(current, target)

        self.assertEqual(commands, ["G01 X10.0 F3000"])

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_build_transit_move_lifts_traverses_descends(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        """Transit: lift → X → Y → (descend skipped when target_z == travel_z).

        Models an inter-well scan hop. X and Y always ship as separate
        G-code lines so no diagonal motion is ever commanded.
        """
        mill = Mill()

        current = Coordinates(-100.0, -50.0, -78.0)  # well_i action z
        target = Coordinates(-110.0, -60.0, -85.0)   # well_j approach z
        commands = mill._build_transit_move(current, target, travel_z=-85.0)

        self.assertEqual(commands, [
            "G01 Z-85.0 F3000",    # lift
            "G01 X-110.0 F3000",   # X alone
            "G01 Y-60.0 F3000",    # Y alone
            # target.z == travel_z, final descent skipped.
        ])

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_build_transit_move_skips_lift_when_already_at_travel_z(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        """Already at travel_z: no lift, just X (Y unchanged) then descent."""
        mill = Mill()

        current = Coordinates(-100.0, -50.0, -85.0)
        target = Coordinates(-110.0, -50.0, -90.0)  # Y unchanged
        commands = mill._build_transit_move(current, target, travel_z=-85.0)

        self.assertEqual(commands, [
            "G01 X-110.0 F3000",
            "G01 Z-90.0 F3000",
        ])

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_build_transit_move_skips_xy_when_target_xy_matches_current(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        """Same-XY transit: lift then descend — neither X nor Y emits."""
        mill = Mill()

        current = Coordinates(-100.0, -50.0, -78.0)
        target = Coordinates(-100.0, -50.0, -90.0)
        commands = mill._build_transit_move(current, target, travel_z=-85.0)

        self.assertEqual(commands, [
            "G01 Z-85.0 F3000",
            "G01 Z-90.0 F3000",
        ])

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_build_transit_move_emits_all_four_steps(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        """Lift → X → Y → descend, all four fire when every axis changes
        and travel_z differs from both current.z and target.z."""
        mill = Mill()

        current = Coordinates(-100.0, -50.0, -78.0)
        target = Coordinates(-110.0, -60.0, -90.0)
        commands = mill._build_transit_move(current, target, travel_z=-85.0)

        self.assertEqual(commands, [
            "G01 Z-85.0 F3000",    # lift
            "G01 X-110.0 F3000",   # X alone
            "G01 Y-60.0 F3000",    # Y alone
            "G01 Z-90.0 F3000",    # descend
        ])

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_move_to_survives_position_read_failure(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        """A transient LocationNotFound must not kill an absolute move:
        the full unpruned sequence is emitted instead (lift first)."""
        mill = Mill()
        mill.ser_mill = MagicMock()
        mill.current_coordinates = MagicMock(side_effect=LocationNotFound("boom"))
        mill.execute_command = MagicMock()

        mill.move_to(
            x_coordinate=-110.0,
            y_coordinate=-60.0,
            z_coordinate=-90.0,
            travel_z=-5.0,
        )

        commands = [c[0][0] for c in mill.execute_command.call_args_list]
        self.assertEqual(commands, [
            "G01 Z-5.0 F3000",     # lift first, unconditionally
            "G01 X-110.0 F3000",
            "G01 Y-60.0 F3000",
            "G01 Z-90.0 F3000",
        ])

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_move_to_uses_coordinates_directly(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        mill = Mill()
        mill.ser_mill = MagicMock()
        mill.current_coordinates = MagicMock(
            return_value=Coordinates(-100.0, -50.0, -78.0),
        )
        mill.execute_command = MagicMock()

        mill.move_to(
            x_coordinate=-110.0,
            y_coordinate=-60.0,
            z_coordinate=-90.0,
        )

        mill.execute_command.assert_any_call("G01 X-110.0 F3000")
        mill.execute_command.assert_any_call("G01 Y-60.0 F3000")
        mill.execute_command.assert_any_call("G01 Z-90.0 F3000")

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_move_to_with_travel_z_emits_ordered_commands(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        """End-to-end: move_to(..., travel_z=...) routes
        through _build_transit_move and emits lift → X → Y →
        descend in order. The emitted travel_z matches the input."""
        mill = Mill()
        mill.ser_mill = MagicMock()
        mill.current_coordinates = MagicMock(
            return_value=Coordinates(-100.0, -50.0, -78.0),
        )

        sent = []
        mill.execute_command = lambda cmd: sent.append(cmd) or "ok"

        mill.move_to(
            x_coordinate=-110.0, y_coordinate=-60.0, z_coordinate=-90.0,
            travel_z=-85.0,
        )

        self.assertEqual(sent, [
            "G01 Z-85.0 F3000",
            "G01 X-110.0 F3000",
            "G01 Y-60.0 F3000",
            "G01 Z-90.0 F3000",
        ])

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_move_to_rejects_non_finite_motion_inputs(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        """The low-level driver must not format NaN/Inf into G-code."""
        cases = [
            {"x_coordinate": math.nan, "y_coordinate": 0.0, "z_coordinate": 0.0},
            {"x_coordinate": 0.0, "y_coordinate": math.inf, "z_coordinate": 0.0},
            {"x_coordinate": 0.0, "y_coordinate": 0.0, "z_coordinate": -math.inf},
            {
                "x_coordinate": 0.0,
                "y_coordinate": 0.0,
                "z_coordinate": 0.0,
                "travel_z": math.nan,
            },
        ]

        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                mill = Mill()
                mill.current_coordinates = MagicMock(
                    return_value=Coordinates(0.0, 0.0, 0.0),
                )
                mill.execute_command = MagicMock()

                with self.assertRaisesRegex(ValueError, "finite"):
                    mill.move_to(**kwargs)

                mill.execute_command.assert_not_called()

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.time.time', side_effect=[0.0, 2.0])
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_wait_for_completion_raises_on_timeout(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_time, mock_sleep,
    ):
        mill = Mill()
        mill.current_status = MagicMock(return_value="<Hold|WPos:0,0,0|FS:0,0>")

        with self.assertRaises(StatusReturnError):
            mill._wait_until_idle(
                "<Hold|WPos:0,0,0|FS:0,0>", timeout=1,
            )

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_stop_sends_realtime_feed_hold_and_accepts_hold(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        mill = Mill()
        mill.ser_mill = ScriptedSerial([b"<Hold|WPos:0,0,0|FS:0,0>\r\n"])

        mill.stop()

        self.assertEqual(mill.ser_mill.writes, [b"!", b"?"])

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_feed_hold_realtime_and_resume_are_write_only(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        mill = Mill()
        mill.ser_mill = ScriptedSerial()

        mill.feed_hold_realtime()
        mill.resume()

        self.assertEqual(mill.ser_mill.writes, [b"!", b"~"])

    @patch('cubos.gantry.gantry_driver.driver.time.time', side_effect=[0.0, 6.0])
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_grbl_settings_command_times_out_without_ok(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_time,
    ):
        mill = Mill()
        mill.ser_mill = MagicMock()
        mill.ser_mill.readline.return_value = b"$20=1\n"

        with self.assertRaisesRegex(Exception, "Timed out"):
            mill.execute_command("$$")

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.time.time', side_effect=[0.0, 2.0])
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_home_raises_on_timeout(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_time, mock_sleep,
    ):
        mill = Mill()
        mill.execute_command = MagicMock()
        mill.current_status = MagicMock(return_value="<Run|WPos:0,0,0|FS:0,0>")

        with self.assertRaises(StatusReturnError):
            mill.home(timeout=1)

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.time.time', return_value=0.0)
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_home_raises_immediately_on_feed_hold(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_time, mock_sleep,
    ):
        mill = Mill()
        mill.execute_command = MagicMock()
        mill.current_status = MagicMock(
            return_value="<Hold:0|WPos:0,0,0|FS:0,0>"
        )

        with self.assertRaisesRegex(
            StatusReturnError,
            "Homing paused by feed hold",
        ):
            mill.home(timeout=90)

        mill.current_status.assert_called_once()

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.time.time', side_effect=[0.0, 0.2, 0.4])
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_home_retries_transient_status_failures(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_time, mock_sleep,
    ):
        mill = Mill()
        mill.execute_command = MagicMock()
        mill.current_status = MagicMock(
            side_effect=[
                StatusReturnError("Failed to get status from the mill"),
                "<Idle|WPos:0,0,0|FS:0,0>",
            ]
        )

        mill.home(timeout=1)

        self.assertTrue(mill.homed)
        self.assertEqual(mill.current_status.call_count, 2)

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.time.time', side_effect=[0.0, 0.2, 0.4])
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_home_alarm_retry_drains_before_retrying(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_time, mock_sleep,
    ):
        mill = Mill()
        mill.ser_mill = ScriptedSerial()
        mill.execute_command = MagicMock()
        mill.current_status = MagicMock(
            side_effect=[
                "<Alarm|WPos:0,0,0|FS:0,0>",
                "<Idle|WPos:0,0,0|FS:0,0>",
            ]
        )
        mill.clear_buffers = MagicMock()

        mill.home(timeout=1)

        self.assertEqual(mill.execute_command.call_args_list, [
            call("$H"),
            call("$H"),
        ])
        mill.clear_buffers.assert_called_once()

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_mock_connection(self, mock_cmd_logger, mock_mill_logger, mock_serial):
        """Test connecting with mocked serial port."""
        mock_serial_instance = MagicMock()
        mock_serial_instance.is_open = True
        mock_serial_instance.readline.return_value = b"<Idle|WPos:0,0,0|FS:0,0>\r\n"
        mock_serial.return_value = mock_serial_instance
        
        # Mock the locate_over_serial to return our mock
        with patch.object(Mill, '_locate_over_serial', return_value=(mock_serial_instance, '/dev/test')):
            mill = Mill()
            mill.read_config = MagicMock()
            mill.clear_buffers = MagicMock()
            mill.enforce_wpos_mode = MagicMock()
            mill.set_feed_rate = MagicMock()
            mill.seed_wco = MagicMock()
            mill.connect(port='/dev/test')
            
            self.assertTrue(mill.active_connection)
            self.assertEqual(mill.ser_mill, mock_serial_instance)

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_connect_requires_initial_status_before_wpos_enforcement(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        mock_serial_instance = ScriptedSerial([b"", b"", b""])

        with patch.object(Mill, '_locate_over_serial', return_value=(mock_serial_instance, '/dev/test')):
            mill = Mill()
            with self.assertRaises(MillConnectionError):
                mill.connect(port='/dev/test')

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_disconnect_resets_transient_state(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        mill = Mill()
        mill.ser_mill = ScriptedSerial()
        mill.active_connection = True
        mill.homed = True
        mill._wco = Coordinates(1.0, 2.0, 3.0)
        mill.config = {"$10": "0"}

        mill.disconnect()

        self.assertFalse(mill.active_connection)
        self.assertFalse(mill.homed)
        self.assertIsNone(mill._wco)
        self.assertEqual(mill.config, {})

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_read_grbl_settings_reads_live_controller(self, mock_cmd_logger, mock_mill_logger, mock_serial):
        """Test that read_grbl_settings issues a live $$ read."""
        mill = Mill()
        mill.ser_mill = MagicMock()
        mill.ser_mill.is_open = True
        mill.execute_command = MagicMock(return_value={"$130": "400.000"})

        settings = mill.read_grbl_settings()

        mill.execute_command.assert_called_once_with("$$")
        self.assertEqual(settings["$130"], "400.000")
        self.assertEqual(mill.config["$130"], "400.000")

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_current_coordinates_does_not_require_homing_pull_off_setting(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        mill = Mill()
        mill.ser_mill = MagicMock()
        mill._read_serial = MagicMock(return_value="<Idle|WPos:1.000,2.000,3.000|FS:0,0>")
        mill.config = {"$10": "0"}

        coords = mill.current_coordinates()

        self.assertEqual(coords, Coordinates(1.0, 2.0, 3.0))

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_read_serial_reassembles_split_status(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        mill = Mill()
        mill.ser_mill = ScriptedSerial([
            b"<Idle|WPos:1.000,2.000,",
            b"35.000|FS:0,0>\r\n",
        ])

        self.assertEqual(
            mill._read_serial(),
            "<Idle|WPos:1.000,2.000,35.000|FS:0,0>",
        )

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_read_serial_discards_truncated_status_mid_number(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        mill = Mill()
        mill.ser_mill = ScriptedSerial([b"<Idle|WPos:1.000,2.000,3"])

        self.assertEqual(mill._read_serial(), "")

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_current_status_handles_unstripped_ok(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        mill = Mill()
        mill.ser_mill = ScriptedSerial([
            b"ok\r\n",
            b"<Idle|WPos:0,0,0|FS:0,0>\r\n",
        ])

        self.assertEqual(mill.current_status(), "<Idle|WPos:0,0,0|FS:0,0>")
        self.assertEqual(mill.ser_mill.writes, [b"?"])

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_wait_until_idle_classifies_alarm_case_insensitively(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        mill = Mill()

        with self.assertRaises(StatusReturnError):
            mill._wait_until_idle("<Alarm|WPos:0,0,0|FS:0,0>")

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_enforce_wpos_mode_sets_ten_to_zero(self, mock_cmd_logger, mock_mill_logger, mock_serial):
        """Test that enforce_wpos_mode sends $10=0 when not already set."""
        mill = Mill()
        mock_ser = MagicMock()
        mill.ser_mill = mock_ser
        mill.config["$10"] = "1"

        # Mock execute_command to track calls
        mill.execute_command = MagicMock()
        mill.enforce_wpos_mode()

        mill.execute_command.assert_any_call("$10=0")
        mill.execute_command.assert_any_call("G90")
        self.assertEqual(mill.config["$10"], "0")

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_enforce_wpos_mode_skips_when_already_zero(self, mock_cmd_logger, mock_mill_logger, mock_serial):
        """Test that enforce_wpos_mode does not re-send $10=0 if already set."""
        mill = Mill()
        mill.config["$10"] = "0"
        mill.execute_command = MagicMock()

        mill.enforce_wpos_mode()

        calls = [str(c) for c in mill.execute_command.call_args_list]
        self.assertNotIn("call('$10=0')", calls)
        mill.execute_command.assert_called_with("G90")

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_jog_raises_when_not_connected(self, mock_cmd_logger, mock_mill_logger, mock_serial):
        """Test that jog raises MillConnectionError when ser_mill is None."""
        from cubos.gantry.gantry_driver.exceptions import MillConnectionError
        mill = Mill()
        mill.ser_mill = None
        with self.assertRaises(MillConnectionError):
            mill.jog(x=1.0)

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_jog_raises_on_alarm_response(self, mock_cmd_logger, mock_mill_logger, mock_serial):
        """Test that jog treats alarm responses as failed motion."""
        from cubos.gantry.gantry_driver.exceptions import CommandExecutionError
        mill = Mill()
        mill.ser_mill = MagicMock()
        mill._read_serial = MagicMock(return_value="<Alarm|WPos:0,0,0|Pn:Y>")

        with self.assertRaises(CommandExecutionError):
            mill.jog(y=-1.0)

        mill.ser_mill.write.assert_called_once()

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_jog_raises_on_check_limits_response(self, mock_cmd_logger, mock_mill_logger, mock_serial):
        """Test that GRBL check-limits messages are treated as failed jogs."""
        from cubos.gantry.gantry_driver.exceptions import CommandExecutionError
        mill = Mill()
        mill.ser_mill = MagicMock()
        mill._read_serial = MagicMock(return_value="[MSG:Check Limits]\nok")

        with self.assertRaises(CommandExecutionError):
            mill.jog(y=-1.0)

        mill.ser_mill.write.assert_called_once()

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_jog_cancel_raises_when_not_connected(self, mock_cmd_logger, mock_mill_logger, mock_serial):
        """Test that jog_cancel raises MillConnectionError when ser_mill is None."""
        from cubos.gantry.gantry_driver.exceptions import MillConnectionError
        mill = Mill()
        mill.ser_mill = None
        with self.assertRaises(MillConnectionError):
            mill.jog_cancel()

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_unguarded_read_methods_raise_mill_connection_error(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        mill = Mill()
        for method_name in (
            "current_status",
            "clear_buffers",
            "seed_wco",
            "current_coordinates",
        ):
            with self.subTest(method=method_name):
                with self.assertRaises(MillConnectionError):
                    getattr(mill, method_name)()

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_unlock_raises_when_not_connected(self, mock_cmd_logger, mock_mill_logger, mock_serial):
        """Test that unlock ($X) raises when ser_mill is None."""
        from cubos.gantry.gantry_driver.exceptions import MillConnectionError
        mill = Mill()
        mill.ser_mill = None
        with self.assertRaises(MillConnectionError):
            mill.unlock()

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_soft_reset_raises_when_not_connected(self, mock_cmd_logger, mock_mill_logger, mock_serial):
        """Test that soft_reset raises when ser_mill is None."""
        from cubos.gantry.gantry_driver.exceptions import MillConnectionError
        mill = Mill()
        mill.ser_mill = None
        with self.assertRaises(MillConnectionError):
            mill.soft_reset()

    # ── Silent-failure regression guards ────────────────────────────────

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_enforce_wpos_mode_propagates_g90_failure(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        """A failed G90 must raise — silent fallthrough risks relative-motion bugs."""
        from cubos.gantry.gantry_driver.exceptions import CommandExecutionError
        mill = Mill()
        mill.config["$10"] = "0"
        mill.execute_command = MagicMock(
            side_effect=CommandExecutionError("G90 rejected")
        )

        with self.assertRaises(CommandExecutionError):
            mill.enforce_wpos_mode()

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_query_raw_status_raises_on_serial_error(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        """OSError on transport must surface, not be flattened to ''."""
        from cubos.gantry.gantry_driver.exceptions import MillConnectionError
        mill = Mill()
        mill.ser_mill = MagicMock()
        mill.ser_mill.is_open = True
        mill.ser_mill.write.side_effect = OSError("port disappeared")

        with self.assertRaises(MillConnectionError):
            mill.query_raw_status()

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_query_raw_status_returns_empty_only_when_disconnected(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        mill = Mill()
        mill.ser_mill = None
        self.assertEqual(mill.query_raw_status(), "")

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_current_status_raises_instead_of_returning_empty_string(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        """No more interactive-mode `return ""` after alarm/error detection."""
        from cubos.gantry.gantry_driver.exceptions import StatusReturnError
        mill = Mill()
        mill.ser_mill = MagicMock()
        mill._read_serial = MagicMock(return_value="")
        mill.ser_mill.readlines.return_value = [b"<Alarm|MPos:0,0,0>\n"]

        with self.assertRaises(StatusReturnError):
            mill.current_status()

    @patch('cubos.gantry.gantry_driver.driver.time.sleep')
    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_execute_command_error22_is_bounded(
        self, mock_cmd_logger, mock_mill_logger, mock_serial, mock_sleep,
    ):
        """Persistent error:22 must terminate after ERROR_22_MAX_RETRIES, not recurse."""
        from cubos.gantry.gantry_driver.driver import ERROR_22_MAX_RETRIES
        from cubos.gantry.gantry_driver.exceptions import CommandExecutionError
        mill = Mill()
        mill.ser_mill = MagicMock()
        mill._read_serial = MagicMock(return_value="error:22\nok")
        mill._wait_until_idle = MagicMock(return_value="error:22\nok")
        mill.set_feed_rate = MagicMock()

        with self.assertRaises(CommandExecutionError):
            mill.execute_command("G01 X10")

        # set_feed_rate is called once per retry attempt (not including the final
        # giving-up attempt that raises).
        self.assertEqual(mill.set_feed_rate.call_count, ERROR_22_MAX_RETRIES)

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def test_execute_command_logs_even_when_suppress_errors(
        self, mock_cmd_logger, mock_mill_logger, mock_serial,
    ):
        """suppress_errors only demotes log level — it never produces a silent path."""
        from cubos.gantry.gantry_driver.exceptions import CommandExecutionError
        mill = Mill()
        mill.ser_mill = MagicMock()
        mill._read_serial = MagicMock(return_value="error:9\nok")
        mill._wait_until_idle = MagicMock(return_value="error:9\nok")
        mill.logger = MagicMock()

        with self.assertRaises(CommandExecutionError):
            mill.execute_command("G01 X10", suppress_errors=True)

        # Every failure must produce at least one log call.
        self.assertTrue(mill.logger.log.called or mill.logger.error.called)


if __name__ == '__main__':
    unittest.main()
