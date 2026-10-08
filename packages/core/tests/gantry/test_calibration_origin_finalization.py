from copy import deepcopy
from unittest.mock import Mock

import pytest
import yaml

from cubos.gantry.gantry import Gantry
from cubos.gantry.gantry_driver.exceptions import MillConnectionError
from cubos.gantry.origin import calibrated_origin_frame
from cubos.gantry.session import CalibrationBlockedError, GantrySession


PARAMS = dict(home_z=80, block_touch_z=35, block_height=35, total_z_range=80, homing_pull_off=1)
SESSION_PARAMS = dict(home_z=80, block_touch_z=35, block_height=35, factory_z_travel=80)


def config(policy):
    return {
        "serial_port": "/dev/offline", "gantry_type": "cub_xl", "origin_policy": policy,
        "cnc": {"factory_z_travel_mm": 80, "safe_z": 0 if policy == "home_origin" else 78},
        "working_volume": calibrated_origin_frame(policy, {"x": 298, "y": 198}, z_min=0, z_max=78)[0],
        "grbl_settings": {"homing_pull_off": 1},
        "instruments": {"pipette": {"type": "pipette", "vendor": "sartorius", "offset_x": 0, "offset_y": 0, "depth": -42}, "camera": {"type": "camera", "vendor": "usb", "offset_x": 2.5, "offset_y": -28.75, "depth": -75}},
    }


def mocked_gantry(policy, final_position=None):
    gantry = Gantry(config(policy), offline=True)
    gantry._offline = False
    gantry._mill = Mock()
    gantry.home = Mock()
    gantry.configure_soft_limits_from_spans = Mock()
    gantry.activate_work_coordinate_system = Mock()
    gantry.clear_g92_offsets = Mock()
    target = {"x": 0, "y": 0, "z": 0} if policy == "home_origin" else {"x": 258, "y": 145, "z": 80}
    gantry.get_coordinates = Mock(side_effect=[{"x": 258, "y": 145, "z": 80}, target if final_position is None else final_position])
    return gantry


@pytest.mark.parametrize("policy", ["deck_origin", "home_origin"])
def test_finalization_persists_selected_frame_after_programming_and_rehoming(policy):
    gantry = mocked_gantry(policy)
    events = Mock()
    events.attach_mock(gantry.home, "home")
    events.attach_mock(gantry.configure_soft_limits_from_spans, "limits")
    events.attach_mock(gantry._mill.execute_command, "command")
    original_instruments = deepcopy(gantry.config["instruments"])
    result = gantry.finalize_deck_origin_calibration(**PARAMS)
    assert result["origin_policy"] == policy
    assert result["measured_volume"] == {"x": 258, "y": 145, "z": 80}
    assert result["max_travel"] == {"x": 259, "y": 146, "z": 81}
    assert result["working_volume"] == calibrated_origin_frame(policy, result["measured_volume"], z_min=0, z_max=80)[0]
    command = "G10 L20 P1 X0 Y0 Z0" if policy == "home_origin" else "G10 L20 P1 X258 Y145 Z80"
    gantry._mill.execute_command.assert_any_call(command)
    order = [call[0] for call in events.mock_calls]
    assert order.index("limits") < max(index for index, name in enumerate(order) if name == "home") < max(index for index, name in enumerate(order) if name == "command")
    assert gantry.home.call_count == 2
    assert gantry.config["instruments"] == original_instruments


@pytest.mark.parametrize("policy", ["deck_origin", "home_origin"])
@pytest.mark.parametrize("bad", [1.0, float("nan"), float("inf")])
def test_final_wpos_mismatch_or_nonfinite_fails_without_retry(policy, bad):
    target = {"x": 0, "y": 0, "z": 0} if policy == "home_origin" else {"x": 258, "y": 145, "z": 80}
    target["x"] += bad
    gantry = mocked_gantry(policy, target)
    with pytest.raises(MillConnectionError, match="WPos did not verify"):
        gantry.finalize_deck_origin_calibration(**PARAMS)
    g10 = [call for call in gantry._mill.execute_command.call_args_list if call.args[0].startswith("G10")]
    assert len(g10) == 1


@pytest.mark.parametrize("operation", ["home", "configure_soft_limits_from_spans", "set_work_coordinates", "get_coordinates"])
def test_controller_failures_propagate_unchanged(operation):
    gantry = mocked_gantry("home_origin")
    failure = MillConnectionError("mock serial lost")
    setattr(gantry, operation, Mock(side_effect=failure))
    with pytest.raises(MillConnectionError) as raised:
        gantry.finalize_deck_origin_calibration(**PARAMS)
    assert raised.value is failure


@pytest.mark.parametrize("policy", ["deck_origin", "home_origin"])
def test_runtime_finalization_uses_connected_policy_and_immediately_updates_bounds(tmp_path, policy):
    body = config(policy)
    path = tmp_path / "gantry.yaml"
    path.write_text(yaml.safe_dump(body))
    session = GantrySession(gantry_factory=lambda config: Gantry(config, offline=True))
    session.connect(path, filename="gantry.yaml")
    session._gantry.set_work_coordinates(x=258, y=145, z=80)
    result = session.finalize_calibration_origin(**SESSION_PARAMS)
    assert result.origin_policy == policy
    assert session.position().work_x == result.position["x"]
    assert session.connected_gantry_config["working_volume"] == result.working_volume
    assert session._gantry.config["working_volume"] == result.working_volume
    assert session.connected_gantry_config["instruments"] == body["instruments"]
    assert session.connected_gantry_config["grbl_settings"]["max_travel_z"] == 81
    assert session.calibration_active
    session.refresh_connected_config("gantry.yaml", session.connected_gantry_config)
    assert not session.calibration_active


def test_failed_frame_blocks_motion_and_protocol_even_after_soft_limit_restore(tmp_path):
    path = tmp_path / "gantry.yaml"
    path.write_text(yaml.safe_dump(config("home_origin")))
    session = GantrySession(gantry_factory=lambda config: Gantry(config, offline=True))
    session.connect(path, filename="gantry.yaml")
    session._gantry.soft_limits_enabled = Mock(return_value=True)
    original = session.connected_gantry_config
    failure = MillConnectionError("Origin readback mismatch")
    session._gantry.finalize_deck_origin_calibration = Mock(side_effect=failure)
    with pytest.raises(MillConnectionError) as raised:
        session.finalize_calibration_origin(**SESSION_PARAMS)
    assert raised.value is failure
    session.restore_calibration_soft_limits()
    assert session.calibration_active
    assert session.connected_gantry_config == original
    with pytest.raises(CalibrationBlockedError):
        session.jog(x=-1)
    with pytest.raises(CalibrationBlockedError):
        session.move_to_blocking(x=-10, y=-10, z=-10)
    with pytest.raises(CalibrationBlockedError):
        session.run_protocol(gantry_path=path, deck_path=path, protocol_path=path, gantry_file="gantry.yaml", deck_file="deck.yaml", protocol_file="protocol.yaml")
    session.prepare_calibration_origin()
    assert not session._calibration_frame_unverified


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0, -1])
def test_origin_frame_rejects_invalid_measurements(bad):
    with pytest.raises(ValueError):
        calibrated_origin_frame("home_origin", {"x": bad, "y": 145}, z_min=0, z_max=80)


def test_origin_frame_keeps_only_usable_span_when_bottom_is_unreachable():
    volume, home = calibrated_origin_frame("home_origin", {"x": 258, "y": 145}, z_min=25, z_max=135)
    assert volume["z_min"] == -110
    assert volume["z_max"] == 0
    assert home == {"x": 0, "y": 0, "z": 0}


@pytest.mark.parametrize("previous,selected,safe,expected", [
    ("deck_origin", "home_origin", 76, -5),
    ("home_origin", "deck_origin", -5, 76),
    ("deck_origin", "deck_origin", 76, 76),
    ("home_origin", "home_origin", -5, -5),
    ("home_origin", "deck_origin", None, 81),
])
def test_safe_z_translation_preserves_physical_plane(previous, selected, safe, expected):
    from cubos.gantry.origin import translate_calibrated_safe_z
    volume, _ = calibrated_origin_frame(selected, {"x": 258, "y": 145}, z_min=1, z_max=81)
    assert translate_calibrated_safe_z(safe, previous, selected, 81, volume) == expected


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_safe_z_translation_rejects_nonfinite_values(bad):
    from cubos.gantry.origin import translate_calibrated_safe_z
    volume, _ = calibrated_origin_frame("deck_origin", {"x": 258, "y": 145}, z_min=1, z_max=81)
    with pytest.raises(ValueError, match="finite"):
        translate_calibrated_safe_z(bad, "deck_origin", "home_origin", 81, volume)


def test_pending_save_cannot_be_cleared_by_restore_or_unmatched_refresh_or_old_profile(tmp_path):
    path = tmp_path / "gantry.yaml"
    path.write_text(yaml.safe_dump(config("home_origin")))
    session = GantrySession(gantry_factory=lambda config: Gantry(config, offline=True))
    session.connect(path, filename="gantry.yaml")
    session._gantry.set_work_coordinates(x=258, y=145, z=80)
    session.finalize_calibration_origin(**SESSION_PARAMS, origin_policy="deck_origin")
    finalized = session.connected_gantry_config
    controller = Mock()
    session._gantry.finalize_deck_origin_calibration = controller
    with pytest.raises(CalibrationBlockedError):
        session.finalize_calibration_origin(**SESSION_PARAMS)
    controller.assert_not_called()
    session.restore_calibration_soft_limits()
    session.refresh_connected_config("other.yaml", finalized)
    session.refresh_connected_config("gantry.yaml", config("home_origin"))
    assert session.calibration_active
    assert session.connected_gantry_config == finalized
    with pytest.raises(CalibrationBlockedError, match="Save the finalized"):
        session.jog(x=-1)
    session.disconnect()
    with pytest.raises(CalibrationBlockedError, match="Save the finalized"):
        session.connect(path, filename="gantry.yaml")
    saved = tmp_path / "saved.yaml"
    saved.write_text(yaml.safe_dump(finalized))
    assert session.connect(saved, filename="saved.yaml").connected
    assert not session.calibration_active


def test_selected_policy_validation_happens_before_controller_operations(tmp_path):
    path = tmp_path / "gantry.yaml"
    path.write_text(yaml.safe_dump(config("deck_origin")))
    session = GantrySession(gantry_factory=lambda config: Gantry(config, offline=True))
    session.connect(path, filename="gantry.yaml")
    controller = Mock()
    session._gantry.finalize_deck_origin_calibration = controller
    with pytest.raises(ValueError):
        session.finalize_calibration_origin(**SESSION_PARAMS, origin_policy="unknown")
    controller.assert_not_called()
    assert not session.calibration_active


def test_session_switch_translates_safe_z_and_preserves_mounted_offsets(tmp_path):
    body = config("home_origin")
    body["cnc"]["safe_z"] = -5
    path = tmp_path / "gantry.yaml"
    path.write_text(yaml.safe_dump(body))
    session = GantrySession(gantry_factory=lambda config: Gantry(config, offline=True))
    session.connect(path, filename="gantry.yaml")
    session._gantry.set_work_coordinates(x=258, y=145, z=80)
    result = session.finalize_calibration_origin(**SESSION_PARAMS, origin_policy="deck_origin")
    assert result.safe_z == 75
    assert session.connected_gantry_config["cnc"]["safe_z"] == 75
    assert session.connected_gantry_config["instruments"] == body["instruments"]


def test_final_wpos_cannot_exceed_signed_bounds_even_with_loose_tolerance():
    gantry = mocked_gantry("home_origin", {"x": 0.1, "y": 0, "z": 0})
    with pytest.raises(MillConnectionError, match="WPos did not verify"):
        gantry.finalize_deck_origin_calibration(**PARAMS, tolerance_mm=0.25)


def test_failed_frame_survives_disconnect_and_reconnect_until_explicit_calibration_prepare(tmp_path):
    path = tmp_path / "gantry.yaml"
    path.write_text(yaml.safe_dump(config("deck_origin")))
    session = GantrySession(gantry_factory=lambda config: Gantry(config, offline=True))
    session.connect(path, filename="gantry.yaml")
    session._gantry.soft_limits_enabled = Mock(return_value=True)
    session._gantry.finalize_deck_origin_calibration = Mock(side_effect=MillConnectionError("WPos did not verify"))
    with pytest.raises(MillConnectionError):
        session.finalize_calibration_origin(**SESSION_PARAMS)
    session.disconnect()
    assert session.calibration_active
    assert "origin did not verify" in session.calibration_warning
    reconnected = session.connect(path, filename="gantry.yaml")
    assert reconnected.connected
    assert "origin did not verify" in reconnected.calibration_warning
    assert session.calibration_active
    with pytest.raises(CalibrationBlockedError):
        session.jog(x=1)
    with pytest.raises(CalibrationBlockedError):
        session.move_to_blocking(x=1, y=1, z=1)
    with pytest.raises(CalibrationBlockedError):
        session.run_protocol(gantry_path=path, deck_path=path, protocol_path=path, gantry_file="gantry.yaml", deck_file="deck.yaml", protocol_file="protocol.yaml")
    session.prepare_calibration_origin()
    assert not session._calibration_frame_unverified
    assert "origin did not verify" not in (session.calibration_warning or "")
    session.jog(x=1)
    assert session.position().work_x == 1
