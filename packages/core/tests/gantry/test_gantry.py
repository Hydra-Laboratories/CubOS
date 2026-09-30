import unittest
from unittest.mock import patch

from cubos.gantry.errors import (
    CommandExecutionError,
    LocationNotFound,
    MillConnectionError,
    StatusReturnError,
)
from cubos.gantry.coordinates import Coordinates
from cubos.gantry.gantry import Gantry


class TestGantry(unittest.TestCase):
    def setUp(self):
        self.config = {"serial_port": "/dev/tty.usbserial"}

    @patch("cubos.gantry.gantry.Mill")
    def test_connect_auto_scans_even_with_configured_port(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        gantry = Gantry(config=self.config)
        gantry.connect()
        mock_mill.connect.assert_called_with(port=None)

    @patch("cubos.gantry.gantry.Mill")
    def test_connect_accepts_explicit_port_override(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        gantry = Gantry(config=self.config)
        gantry.connect(port="/dev/tty.usbserial-130")
        mock_mill.connect.assert_called_with(port="/dev/tty.usbserial-130")

    @patch("cubos.gantry.gantry.Mill")
    def test_connect_leaves_default_feed_rate_unset_without_config(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.default_feed_rate = 3000
        gantry = Gantry(config=self.config)
        gantry.connect()
        self.assertEqual(mock_mill.default_feed_rate, 3000)

    @patch("cubos.gantry.gantry.Mill")
    def test_connect_applies_configured_default_feed_rate(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        config = {**self.config, "cnc": {"default_feed_rate_mm_min": 4000.0}}
        gantry = Gantry(config=config)
        gantry.connect()
        self.assertEqual(mock_mill.default_feed_rate, 4000.0)

    @patch("cubos.gantry.gantry.Mill")
    def test_move_delegates_to_mill_move_to(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        gantry = Gantry(config=self.config)
        gantry.move_to(10, 20, 30)
        mock_mill.move_to.assert_called_with(
            x_coordinate=10.0,
            y_coordinate=20.0,
            z_coordinate=30.0,
            travel_z=None,
        )

    @patch("cubos.gantry.gantry.Mill")
    def test_move_with_travel_z_passes_through_deck_frame_z(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        gantry = Gantry(config=self.config)
        gantry.move_to(10, 20, 30, travel_z=50)
        mock_mill.move_to.assert_called_with(
            x_coordinate=10.0,
            y_coordinate=20.0,
            z_coordinate=30.0,
            travel_z=50.0,
        )

    @patch("cubos.gantry.gantry.Mill")
    def test_is_healthy(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.active_connection = True
        mock_mill.ser_mill.is_open = True
        mock_mill.current_status.return_value = "<Idle|MPos:0,0,0|FS:0,0>"
        gantry = Gantry(config=self.config)
        self.assertTrue(gantry.is_healthy())

        mock_mill.current_status.return_value = "<Alarm|MPos:0,0,0|FS:0,0>"
        self.assertFalse(gantry.is_healthy())

        mock_mill.current_status.return_value = "ALARM:1"
        self.assertFalse(gantry.is_healthy())

    @patch("cubos.gantry.gantry.Mill")
    def test_connect_raises_mill_connection_error(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.connect.side_effect = MillConnectionError("no port")
        gantry = Gantry(config=self.config)
        with self.assertRaises(MillConnectionError):
            gantry.connect()

    @patch("cubos.gantry.gantry.Mill")
    def test_connect_does_not_catch_unexpected_errors(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.connect.side_effect = RuntimeError("unexpected")
        gantry = Gantry(config=self.config)
        with self.assertRaises(RuntimeError):
            gantry.connect()

    @patch("cubos.gantry.gantry.Mill")
    def test_disconnect_reraises_mill_connection_error(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.disconnect.side_effect = MillConnectionError("port busy")
        gantry = Gantry(config=self.config)
        with self.assertRaises(MillConnectionError):
            gantry.disconnect()

    @patch("cubos.gantry.gantry.Mill")
    def test_disconnect_does_not_catch_unexpected_errors(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.disconnect.side_effect = RuntimeError("unexpected")
        gantry = Gantry(config=self.config)
        with self.assertRaises(RuntimeError):
            gantry.disconnect()

    @patch("cubos.gantry.gantry.Mill")
    def test_is_healthy_returns_false_on_status_error(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.active_connection = True
        mock_mill.ser_mill.is_open = True
        mock_mill.current_status.side_effect = StatusReturnError("bad status")
        gantry = Gantry(config=self.config)
        self.assertFalse(gantry.is_healthy())

    @patch("cubos.gantry.gantry.Mill")
    def test_is_healthy_returns_false_on_connection_error(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.active_connection = True
        mock_mill.ser_mill.is_open = True
        mock_mill.current_status.side_effect = MillConnectionError("lost")
        gantry = Gantry(config=self.config)
        self.assertFalse(gantry.is_healthy())

    @patch("cubos.gantry.gantry.Mill")
    def test_is_healthy_propagates_unexpected_errors(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.active_connection = True
        mock_mill.ser_mill.is_open = True
        mock_mill.current_status.side_effect = RuntimeError("unexpected")
        gantry = Gantry(config=self.config)
        with self.assertRaises(RuntimeError):
            gantry.is_healthy()

    @patch("cubos.gantry.gantry.Mill")
    def test_home_raises_on_connection_error(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.home.side_effect = MillConnectionError("homing failed")
        gantry = Gantry(config=self.config)
        with self.assertRaises(MillConnectionError):
            gantry.home()

    @patch("cubos.gantry.gantry.Mill")
    def test_home_raises_on_status_error(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.home.side_effect = StatusReturnError("alarm")
        gantry = Gantry(config=self.config)
        with self.assertRaises(StatusReturnError):
            gantry.home()

    @patch("cubos.gantry.gantry.Mill")
    def test_home_does_not_catch_unexpected_errors(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.home.side_effect = RuntimeError("unexpected")
        gantry = Gantry(config=self.config)
        with self.assertRaises(RuntimeError):
            gantry.home()

    @patch("cubos.gantry.gantry.Mill")
    def test_home_uses_standard_grbl_homing_by_default(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        gantry = Gantry(config=self.config)
        gantry.home()
        mock_mill.home.assert_called_once_with()

    @patch("cubos.gantry.gantry.Mill")
    def test_prepare_for_protocol_run_unlocks_alarm_and_restores_state(
        self, mock_mill_cls
    ):
        mock_mill = mock_mill_cls.return_value
        mock_mill.query_raw_status.side_effect = [
            "<Alarm|WPos:0,0,0|FS:0,0>",
            "<Idle|WPos:0,0,0|FS:0,0>",
            "<Idle|WPos:0,0,0|FS:0,0>",
        ]
        mock_mill.read_grbl_settings.return_value = {}
        gantry = Gantry(config=self.config)

        gantry.prepare_for_protocol_run()

        mock_mill.soft_reset_and_unlock.assert_called_once()
        mock_mill.read_config.assert_called_once()
        mock_mill.clear_buffers.assert_called_once()
        mock_mill.enforce_wpos_mode.assert_called_once()
        mock_mill.set_feed_rate.assert_called_once()
        mock_mill.seed_wco.assert_called_once()

    @patch("cubos.gantry.gantry.Mill")
    def test_prepare_for_protocol_run_noops_when_not_in_alarm(
        self, mock_mill_cls
    ):
        mock_mill = mock_mill_cls.return_value
        mock_mill.query_raw_status.return_value = "<Idle|WPos:0,0,0|FS:0,0>"
        gantry = Gantry(config=self.config)

        gantry.prepare_for_protocol_run()

        mock_mill.soft_reset_and_unlock.assert_not_called()
        mock_mill.read_config.assert_not_called()

    @patch("cubos.gantry.gantry.Mill")
    def test_prepare_for_protocol_run_raises_if_alarm_persists(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.query_raw_status.side_effect = [
            "<Alarm|WPos:0,0,0|FS:0,0>",
            "<Idle|WPos:0,0,0|FS:0,0>",
            "<Alarm|WPos:0,0,0|FS:0,0>",
        ]
        mock_mill.read_grbl_settings.return_value = {}
        gantry = Gantry(config=self.config)

        with self.assertRaises(MillConnectionError):
            gantry.prepare_for_protocol_run()

    @patch("cubos.gantry.gantry.Mill")
    def test_move_to_raises_on_command_error(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.move_to.side_effect = CommandExecutionError("move failed")
        gantry = Gantry(config=self.config)
        with self.assertRaises(CommandExecutionError):
            gantry.move_to(10, 20, 30)

    @patch("cubos.gantry.gantry.Mill")
    def test_move_to_does_not_catch_unexpected_errors(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.move_to.side_effect = RuntimeError("unexpected")
        gantry = Gantry(config=self.config)
        with self.assertRaises(RuntimeError):
            gantry.move_to(10, 20, 30)

    @patch("cubos.gantry.gantry.Mill")
    def test_get_status_raises_on_status_error(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.current_status.side_effect = StatusReturnError("bad")
        gantry = Gantry(config=self.config)
        with self.assertRaises(StatusReturnError):
            gantry.get_status()

    @patch("cubos.gantry.gantry.Mill")
    def test_get_status_propagates_unexpected_errors(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.current_status.side_effect = RuntimeError("unexpected")
        gantry = Gantry(config=self.config)
        with self.assertRaises(RuntimeError):
            gantry.get_status()

    @patch("cubos.gantry.gantry.Mill")
    def test_stop_raises_on_known_errors(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.stop.side_effect = CommandExecutionError("stop failed")
        gantry = Gantry(config=self.config)
        with self.assertRaises(CommandExecutionError):
            gantry.stop()

    @patch("cubos.gantry.gantry.Mill")
    def test_stop_propagates_unexpected_errors(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.stop.side_effect = RuntimeError("unexpected")
        gantry = Gantry(config=self.config)
        with self.assertRaises(RuntimeError):
            gantry.stop()

    @patch("cubos.gantry.gantry.Mill")
    def test_feed_hold_realtime_and_resume_delegate_to_mill(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        gantry = Gantry(config=self.config)

        gantry.feed_hold_realtime()
        gantry.resume()

        mock_mill.feed_hold_realtime.assert_called_once()
        mock_mill.resume.assert_called_once()

    @patch("cubos.gantry.gantry.Mill")
    def test_get_coordinates_raises_on_known_error(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.current_coordinates.side_effect = LocationNotFound()
        gantry = Gantry(config=self.config)
        with self.assertRaises(LocationNotFound):
            gantry.get_coordinates()

    @patch("cubos.gantry.gantry.Mill")
    def test_get_coordinates_propagates_unexpected_errors(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.current_coordinates.side_effect = RuntimeError("unexpected")
        gantry = Gantry(config=self.config)
        with self.assertRaises(RuntimeError):
            gantry.get_coordinates()

    @patch("cubos.gantry.gantry.Mill")
    def test_jog_cancel_raises_on_connection_error(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.jog_cancel.side_effect = MillConnectionError("not connected")
        gantry = Gantry(config=self.config)
        with self.assertRaises(MillConnectionError):
            gantry.jog_cancel()

    @patch("cubos.gantry.gantry.Mill")
    def test_get_position_info_raises_on_error(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.current_coordinates.side_effect = StatusReturnError("fail")
        gantry = Gantry(config=self.config)
        with self.assertRaises(StatusReturnError):
            gantry.get_position_info()

    @patch("cubos.gantry.gantry.Mill")
    def test_extract_status_returns_idle_from_grbl_string(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.last_status = "<Idle|WPos:0,0,0|FS:0,0>"
        gantry = Gantry(config=self.config)
        self.assertEqual(gantry._extract_status(), "Idle")

    @patch("cubos.gantry.gantry.Mill")
    def test_extract_status_returns_unknown_when_empty(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.last_status = ""
        gantry = Gantry(config=self.config)
        self.assertEqual(gantry._extract_status(), "Unknown")

    @patch("cubos.gantry.gantry.Mill")
    def test_clear_g92_offsets_sends_g92_1(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        gantry = Gantry(config=self.config)
        gantry.clear_g92_offsets()
        mock_mill.execute_command.assert_called_with("G92.1")

    @patch("cubos.gantry.gantry.Mill")
    def test_enforce_work_position_reporting_delegates_to_mill(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        gantry = Gantry(config=self.config)
        gantry.enforce_work_position_reporting()
        mock_mill.enforce_wpos_mode.assert_called_once_with()

    @patch("cubos.gantry.gantry.Mill")
    def test_activate_work_coordinate_system_sends_g54(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        gantry = Gantry(config=self.config)
        gantry.activate_work_coordinate_system()
        mock_mill.execute_command.assert_called_with("G54")

    @patch("cubos.gantry.gantry.Mill")
    def test_set_work_coordinates_sends_g10_l20(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        gantry = Gantry(config=self.config)
        gantry.set_work_coordinates(400.0, 300.0, 100.0)
        mock_mill.execute_command.assert_called_with("G10 L20 P1 X400 Y300 Z100")

    @patch("cubos.gantry.gantry.Mill")
    def test_set_work_coordinates_can_assign_partial_axes(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        gantry = Gantry(config=self.config)
        gantry.set_work_coordinates(x=0.0, y=0.0)
        mock_mill.execute_command.assert_called_with("G10 L20 P1 X0 Y0")

        gantry.set_work_coordinates(z=14.5)
        mock_mill.execute_command.assert_called_with("G10 L20 P1 Z14.5")

    @patch("cubos.gantry.gantry.Mill")
    def test_set_serial_timeout_updates_serial_object(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        gantry = Gantry(config=self.config)
        gantry.set_serial_timeout(0.5)
        mock_mill.set_read_timeout.assert_called_once_with(0.5)

    @patch("cubos.gantry.gantry.Mill")
    def test_connected_port_comes_from_low_level_driver(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.connected_port.return_value = "/dev/tty.usbserial-130"
        gantry = Gantry(config=self.config)
        self.assertEqual(gantry.connected_port(), "/dev/tty.usbserial-130")

    @patch("cubos.gantry.gantry.Mill")
    def test_soft_limits_enabled_reads_setting_semantically(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.read_grbl_settings.return_value = {"$20": "1"}
        gantry = Gantry(config=self.config)
        self.assertTrue(gantry.soft_limits_enabled())

    @patch("cubos.gantry.gantry.Mill")
    def test_set_soft_limits_enabled_delegates_to_grbl_setting(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        gantry = Gantry(config=self.config)
        gantry.set_soft_limits_enabled(False)
        mock_mill.set_grbl_setting.assert_called_once_with("20", "0")

    @patch("cubos.gantry.gantry.Mill")
    def test_hard_limits_enabled_reads_dollar21(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.read_grbl_settings.return_value = {"$21": "0"}
        gantry = Gantry(config=self.config)
        self.assertFalse(gantry.hard_limits_enabled())
        mock_mill.read_grbl_settings.return_value = {}
        self.assertIsNone(gantry.hard_limits_enabled())
        mock_mill.read_grbl_settings.return_value = {"$21": "not-a-number"}
        self.assertIsNone(gantry.hard_limits_enabled())

    @patch("cubos.gantry.gantry.Mill")
    def test_set_hard_limits_enabled_delegates_to_grbl_setting(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        gantry = Gantry(config=self.config)
        gantry.set_hard_limits_enabled(True)
        mock_mill.set_grbl_setting.assert_called_once_with("21", "1")

    @patch("cubos.gantry.gantry.Mill")
    def test_configure_soft_limits_writes_and_verifies_settings(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.read_grbl_settings.return_value = {
            "$10": "0",
            "$20": "1",
            "$21": "1",
            "$22": "1",
            "$27": "10.000",
            "$130": "306.000",
            "$131": "306.000",
            "$132": "113.000",
        }
        gantry = Gantry(config=self.config)
        gantry.configure_soft_limits_from_spans(
            max_travel_x=306.0,
            max_travel_y=306.0,
            max_travel_z=113.0,
            status_report=0,
            homing_pull_off=10.0,
            hard_limits=True,
        )
        self.assertEqual(
            mock_mill.set_grbl_setting.call_args_list,
            [
                unittest.mock.call("10", "0"),
                unittest.mock.call("27", "10"),
                unittest.mock.call("21", "1"),
                unittest.mock.call("20", "0"),
                unittest.mock.call("130", "306"),
                unittest.mock.call("131", "306"),
                unittest.mock.call("132", "113"),
                unittest.mock.call("22", "1"),
                unittest.mock.call("20", "1"),
            ],
        )

    @patch("cubos.gantry.gantry.Mill")
    def test_configure_soft_limits_reenables_soft_limits_on_write_failure(
        self, mock_mill_cls
    ):
        mock_mill = mock_mill_cls.return_value
        mock_mill.set_grbl_setting.side_effect = [
            None,
            CommandExecutionError("write failed"),
            None,
        ]
        gantry = Gantry(config=self.config)

        # No pre-writes: explicit None ensures side_effect ordering stays stable
        # regardless of future default changes for status_report/homing_pull_off.
        with self.assertRaises(CommandExecutionError):
            gantry.configure_soft_limits_from_spans(
                max_travel_x=306.0,
                max_travel_y=306.0,
                max_travel_z=113.0,
                status_report=None,
                homing_pull_off=None,
            )

        self.assertEqual(
            mock_mill.set_grbl_setting.call_args_list,
            [
                unittest.mock.call("20", "0"),
                unittest.mock.call("130", "306"),
                unittest.mock.call("20", "1"),
            ],
        )


class TestGantryFinalizeDeckOriginCalibration(unittest.TestCase):
    def setUp(self):
        self.config = {"serial_port": "/dev/tty.usbserial"}

    @patch("cubos.gantry.gantry.Mill")
    def test_finalize_programs_controller_spans_with_homing_pull_off(
        self, mock_mill_cls
    ):
        mock_mill = mock_mill_cls.return_value
        mock_mill.read_grbl_settings.side_effect = [
            {"$27": "10.000"},
            {
                "$10": "0",
                "$20": "1",
                "$21": "1",
                "$22": "1",
                "$27": "10.000",
                "$130": "396.000",
                "$131": "260.500",
                "$132": "101.000",
            },
        ]
        mock_mill.current_coordinates.side_effect = [
            Coordinates(386.0, 250.5, 91.0),
            Coordinates(386.0, 250.5, 91.0),
        ]

        gantry = Gantry(config=self.config)
        result = gantry.finalize_deck_origin_calibration(
            home_z=91.0,
            block_touch_z=10.0,
            block_height=10.0,
            total_z_range=100.0,
            hard_limits=True,
        )

        self.assertEqual(
            result["measured_volume"],
            {"x": 386.0, "y": 250.5, "z": 91.0},
        )
        self.assertEqual(
            result["max_travel"],
            {"x": 396.0, "y": 260.5, "z": 101.0},
        )
        self.assertEqual(result["homing_pull_off_mm"], 10.0)
        self.assertEqual(mock_mill.home.call_count, 1)
        mock_mill.execute_command.assert_any_call(
            "G10 L20 P1 X386 Y250.5 Z91"
        )
        self.assertEqual(
            mock_mill.set_grbl_setting.call_args_list,
            [
                unittest.mock.call("10", "0"),
                unittest.mock.call("10", "0"),
                unittest.mock.call("27", "10"),
                unittest.mock.call("21", "1"),
                unittest.mock.call("20", "0"),
                unittest.mock.call("130", "396"),
                unittest.mock.call("131", "260.5"),
                unittest.mock.call("132", "101"),
                unittest.mock.call("22", "1"),
                unittest.mock.call("20", "1"),
            ],
        )

    @patch("cubos.gantry.gantry.Mill")
    def test_finalize_adds_pull_off_to_usable_z_span_scenarios(
        self, mock_mill_cls
    ):
        scenarios = [
            {
                "name": "reachable bottom",
                "home_z": 110.0,
                "block_touch_z": 60.0,
                "block_height": 35.0,
                "total_z_range": 110.0,
                "homed_z": 85.0,
                "z_min": 0.0,
                "z_max": 85.0,
                "max_travel_z": 95.0,
            },
            {
                "name": "unreachable bottom",
                "home_z": 110.0,
                "block_touch_z": 10.0,
                "block_height": 35.0,
                "total_z_range": 110.0,
                "homed_z": 135.0,
                "z_min": 25.0,
                "z_max": 135.0,
                "max_travel_z": 120.0,
            },
        ]
        for scenario in scenarios:
            with self.subTest(scenario["name"]):
                mock_mill = mock_mill_cls.return_value
                mock_mill.reset_mock()
                mock_mill.read_grbl_settings.side_effect = [
                    {"$27": "10.000"},
                    {
                        "$10": "0",
                        "$20": "1",
                        "$22": "1",
                        "$27": "10.000",
                        "$130": "408.000",
                        "$131": "309.000",
                        "$132": str(scenario["max_travel_z"]),
                    },
                ]
                mock_mill.current_coordinates.side_effect = [
                    Coordinates(398.0, 299.0, scenario["homed_z"]),
                    Coordinates(398.0, 299.0, scenario["homed_z"]),
                ]

                gantry = Gantry(config=self.config)
                result = gantry.finalize_deck_origin_calibration(
                    home_z=scenario["home_z"],
                    block_touch_z=scenario["block_touch_z"],
                    block_height=scenario["block_height"],
                    total_z_range=scenario["total_z_range"],
                )

                self.assertEqual(
                    result["z_calibration"]["z_min"],
                    scenario["z_min"],
                )
                self.assertEqual(
                    result["z_calibration"]["z_max"],
                    scenario["z_max"],
                )
                self.assertEqual(
                    result["max_travel"]["z"],
                    scenario["max_travel_z"],
                )
                self.assertEqual(result["measured_volume"]["z"], scenario["z_max"])

    @patch("cubos.gantry.gantry.Mill")
    def test_finalize_fails_when_live_homing_pull_off_is_missing_or_invalid(
        self, mock_mill_cls
    ):
        for settings in ({}, {"$27": "nan"}, {"$27": "-1"}, {"$27": "bad"}):
            with self.subTest(settings=settings):
                mock_mill = mock_mill_cls.return_value
                mock_mill.reset_mock()
                mock_mill.read_grbl_settings.return_value = settings

                gantry = Gantry(config=self.config)
                with self.assertRaisesRegex(MillConnectionError, r"\$27"):
                    gantry.finalize_deck_origin_calibration(
                        home_z=91.0,
                        block_touch_z=10.0,
                        block_height=10.0,
                        total_z_range=100.0,
                    )

                mock_mill.home.assert_not_called()
                mock_mill.set_grbl_setting.assert_not_called()


    @patch("cubos.gantry.gantry.Mill")
    def test_finalize_raises_mill_connection_error_when_homed_z_mismatches_expected(
        self, mock_mill_cls
    ):
        mock_mill = mock_mill_cls.return_value
        mock_mill.read_grbl_settings.return_value = {"$27": "10.000"}
        # z=92.5 but expected z_max=91.0 — exceeds tolerance_mm=0.001
        mock_mill.current_coordinates.return_value = Coordinates(386.0, 250.5, 92.5)

        gantry = Gantry(config=self.config)
        with self.assertRaises(MillConnectionError):
            gantry.finalize_deck_origin_calibration(
                home_z=91.0,
                block_touch_z=10.0,
                block_height=10.0,
                total_z_range=100.0,
            )

        # Travel spans must not have been programmed
        programmed = [c[0][0] for c in mock_mill.set_grbl_setting.call_args_list]
        self.assertNotIn("130", programmed)
        self.assertNotIn("131", programmed)
        self.assertNotIn("132", programmed)

    @patch("cubos.gantry.gantry.Mill")
    def test_finalize_raises_value_error_when_usable_span_is_non_positive(
        self, mock_mill_cls
    ):
        mock_mill = mock_mill_cls.return_value
        mock_mill.read_grbl_settings.return_value = {"$27": "10.000"}
        # x=0.0 → usable x span is 0.0 ≤ tolerance_mm, so ValueError
        mock_mill.current_coordinates.return_value = Coordinates(0.0, 250.5, 91.0)

        gantry = Gantry(config=self.config)
        with self.assertRaisesRegex(ValueError, "non-positive"):
            gantry.finalize_deck_origin_calibration(
                home_z=91.0,
                block_touch_z=10.0,
                block_height=10.0,
                total_z_range=100.0,
            )


class TestGrblSettingsValidation(unittest.TestCase):
    @patch("cubos.gantry.gantry.Mill")
    def test_validate_passes_when_settings_match(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.read_grbl_settings.return_value = {
            "$3": "2",
            "$10": "1",
            "$130": "300.000",
            "$131": "200.000",
        }
        gantry = Gantry(
            config={
                "grbl_settings": {
                    "dir_invert_mask": 2,
                    "status_report": 1,
                    "max_travel_x": 300.0,
                    "max_travel_y": 200.0,
                },
            }
        )
        gantry._validate_grbl_settings()

    @patch("cubos.gantry.gantry.Mill")
    def test_board_expected_settings_override_gantry_settings(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.read_grbl_settings.return_value = {"$3": "1", "$130": "306.000"}
        gantry = Gantry(
            config={
                "grbl_settings": {
                    "dir_invert_mask": 2,
                    "max_travel_x": 300.0,
                },
            }
        )
        gantry.set_expected_grbl_settings({"$3": 1.0, "$130": 306.0})
        gantry._validate_grbl_settings()

    @patch("cubos.gantry.gantry.Mill")
    def test_validate_raises_on_critical_mismatch(self, mock_mill_cls):
        mock_mill = mock_mill_cls.return_value
        mock_mill.read_grbl_settings.return_value = {"$3": "0", "$130": "300.000"}
        gantry = Gantry(config={"grbl_settings": {"dir_invert_mask": 2}})
        with self.assertRaises(MillConnectionError):
            gantry._validate_grbl_settings()

    @patch("cubos.gantry.gantry.Mill")
    def test_validate_skipped_when_no_grbl_settings(self, mock_mill_cls):
        gantry = Gantry(config={})
        gantry._validate_grbl_settings()


if __name__ == "__main__":
    unittest.main()
