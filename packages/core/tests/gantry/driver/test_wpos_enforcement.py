"""Unit tests for WPos enforcement in Mill driver."""

import unittest
from unittest.mock import MagicMock, patch

from cubos.gantry.coordinates import Coordinates
from cubos.gantry.gantry_driver.driver import Mill, wco_pattern
from cubos.gantry.gantry_driver.exceptions import LocationNotFound


class TestWposEnforcement(unittest.TestCase):
    """Tests that Mill always returns work position coordinates."""

    @patch('cubos.gantry.gantry_driver.driver.serial.Serial')
    @patch('cubos.gantry.gantry_driver.driver.set_up_mill_logger')
    @patch('cubos.gantry.gantry_driver.driver.set_up_command_logger')
    def _make_mill(self, mock_cmd_logger, mock_mill_logger, mock_serial):
        """Create a Mill with mocked serial for testing."""
        mill = Mill()
        mill.ser_mill = MagicMock()
        mill.active_connection = True
        mill.config["$10"] = "0"
        mill.config["$27"] = "2.000"
        return mill

    def test_current_coordinates_returns_wpos_when_wpos_reported(self):
        mill = self._make_mill()
        mill.ser_mill.write = MagicMock()
        mill._read_serial = MagicMock(return_value="<Idle|WPos:10.000,20.000,5.000|Bf:15,127|FS:0,0>")
        coords = mill.current_coordinates()
        self.assertEqual(coords.x, 10.0)
        self.assertEqual(coords.y, 20.0)
        self.assertEqual(coords.z, 5.0)

    def test_current_coordinates_converts_mpos_to_wpos(self):
        mill = self._make_mill()
        mill.config["$10"] = "1"  # Force MPos mode
        mill._wco = Coordinates(-300.0, -200.0, -80.0)
        mill.ser_mill.write = MagicMock()
        mill._read_serial = MagicMock(
            return_value="<Idle|MPos:-290.000,-190.000,-75.000|Bf:15,127|FS:0,0>"
        )
        coords = mill.current_coordinates()
        # WPos = MPos - WCO = -290 - (-300) = 10, etc.
        self.assertAlmostEqual(coords.x, 10.0, places=3)
        self.assertAlmostEqual(coords.y, 10.0, places=3)
        self.assertAlmostEqual(coords.z, 5.0, places=3)

    def test_current_coordinates_rejects_mpos_without_wco(self):
        mill = self._make_mill()
        mill.config["$10"] = "1"
        mill.ser_mill.write = MagicMock()
        mill._read_serial = MagicMock(
            return_value="<Idle|MPos:-290.000,-190.000,-75.000|Bf:15,127|FS:0,0>"
        )
        mill.seed_wco = MagicMock()

        with self.assertRaises(LocationNotFound):
            mill.current_coordinates()

    def test_current_coordinates_updates_wco_from_status(self):
        mill = self._make_mill()
        mill.ser_mill.write = MagicMock()
        mill._read_serial = MagicMock(
            return_value="<Idle|WPos:0.000,0.000,0.000|Bf:15,127|FS:0,0|WCO:-300.000,-200.000,-80.000>"
        )
        self.assertIsNone(mill._wco)
        mill.current_coordinates()
        self.assertIsNotNone(mill._wco)
        self.assertEqual(mill._wco.x, -300.0)
        self.assertEqual(mill._wco.y, -200.0)
        self.assertEqual(mill._wco.z, -80.0)

    def test_wco_pattern_matches(self):
        status = "<Idle|WPos:0,0,0|Bf:15,127|FS:0,0|WCO:-300.000,-200.000,-80.000>"
        match = wco_pattern.search(status)
        self.assertIsNotNone(match)
        self.assertEqual(float(match.group(1)), -300.0)
        self.assertEqual(float(match.group(2)), -200.0)
        self.assertEqual(float(match.group(3)), -80.0)

    def test_move_to_uses_wpos_internally(self):
        """Verify move_to's internal current_coordinates call gets WPos."""
        mill = self._make_mill()
        mill.config["$10"] = "0"
        mill.ser_mill.write = MagicMock()
        mill._read_serial = MagicMock(return_value="<Idle|WPos:0.000,0.000,0.000|Bf:15,127|FS:0,0>")

        commands_sent = []
        mill.execute_command = lambda cmd: commands_sent.append(cmd) or "ok"

        mill.move_to(x_coordinate=-10.0, y_coordinate=-10.0, z_coordinate=0.0)

        xy_commands = [command for command in commands_sent if "X" in command or "Y" in command]
        self.assertEqual(len(xy_commands), 1)
        axes = {
            word[0]: float(word[1:])
            for word in xy_commands[0].split()
            if word.startswith(("X", "Y", "Z"))
        }
        self.assertEqual(axes, {"X": -10.0, "Y": -10.0})
        self.assertFalse(any("Z" in command for command in commands_sent))



if __name__ == '__main__':
    unittest.main()
