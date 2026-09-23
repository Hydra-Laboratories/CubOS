from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import pytest

from cubos.data import DataStore
from cubos.deck.loader import _build_deck_from_raw
from cubos.gantry.gantry_config import GantryConfig, GantryType, WorkingVolume
from cubos.gantry.gantry import Gantry
from cubos.gantry.instrument_mount import InstrumentedGantry
from cubos.instruments.camera.interface import CameraInstrument
from cubos.protocol_engine.compiler import CommandCall, compile_protocol
from cubos.protocol_engine.errors import ProtocolExecutionError
from cubos.protocol_engine.runtime import ProtocolContext
from cubos.protocol_engine.routing import RoutingSession, prepare_planning_context
from cubos.protocol_engine.setup import run_on_hardware, setup_protocol
from cubos.protocol_engine.setup_validator import run_setup_validation


class FakeController:
    def __init__(self) -> None:
        self.coords = {"x": 110.0, "y": 90.0, "z": 80.0}
        self.moves: list[tuple[float, float, float, float | None]] = []

    def get_coordinates(self):
        return dict(self.coords)

    def move_to(self, x, y, z, travel_z=None):
        self.moves.append((x, y, z, travel_z))
        self.coords = {"x": x, "y": y, "z": z}


class FakePipette:
    name = "pipette"
    offset_x = 0.0
    offset_y = 0.0
    depth = -20.0

    def __init__(self) -> None:
        self.attached_tip_extension = 0.0
        self.actions: list[str] = []

    @property
    def effective_depth(self):
        return self.depth + self.attached_tip_extension

    def set_attached_tip_extension(self, value):
        self.attached_tip_extension = float(value)

    def clear_attached_tip_extension(self):
        self.attached_tip_extension = 0.0

    def pick_up_tip(self, speed=50):
        self.actions.append("pick_up_tip")

    def drop_tip(self, speed=50):
        self.actions.append("drop_tip")

    def aspirate(self, volume_ul, speed=50):
        self.actions.append(f"aspirate:{volume_ul}")

    def dispense(self, volume_ul, speed=50):
        self.actions.append(f"dispense:{volume_ul}")


class FakeCamera(CameraInstrument):
    def __init__(self) -> None:
        super().__init__(
            name="camera", offset_x=0.0, offset_y=-10.0, depth=-20.0,
            offline=True,
        )
        self.captures: list[str] = []

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def health_check(self) -> bool:
        return True

    def capture(self, *args, **kwargs) -> str:
        path = str(kwargs["save_path"])
        Path(path).write_bytes(b"fake-image")
        self.captures.append(path)
        return path

    def control_fingerprint(self):
        return {"fingerprint": "offline-camera-profile"}

    def last_frame_metadata(self):
        return {
            "frame_id": len(self.captures),
            "capture_profile": {"fingerprint": "offline-camera-profile"},
        }


def _vial(name: str, x: float, y: float, *, size: float = 10) -> dict:
    return {
        "type": "vial",
        "name": name,
        "height": 20,
        "diameter": size,
        "location": {"x": x, "y": y, "z": 40},
        "capacity_ul": 1000,
        "working_volume_ul": 900,
        "motion": {
            "box": {
                "anchor": "location",
                "offset": {"x": -size / 2, "y": -size / 2, "z": -20},
                "size": {"x": size, "y": size, "z": 20},
            }
        },
    }


def _context(
    *, blocker: bool = False, with_camera: bool = False,
) -> tuple[ProtocolContext, FakeController, FakePipette]:
    labware = {
        "tips": {
            "type": "tip_rack",
            "name": "tips",
            "rows": 1,
            "columns": 1,
            "pickup_z": 50,
            "tip_length": 20,
            "calibration": {
                "a1": {"x": 20, "y": 20, "z": 50},
                "a2": {"x": 30, "y": 20},
            },
            "x_offset": 10,
            "y_offset": 10,
            "tip_present": {"A1": True},
            "motion": {
                "box": {
                    "anchor": "A1",
                    "offset": {"x": -5, "y": -5, "z": -20},
                    "size": {"x": 20, "y": 10, "z": 18},
                },
                "occupied_tip_radius_mm": 1,
                "access": {
                    "pick_up_tip": {
                        "strategy": "side_exit",
                        "lift_mm": 30,
                        "exit_edge": "x_min",
                        "clearance_mm": 5,
                    }
                },
            },
        },
        "source": _vial("source", 60, 60),
        "destination": _vial("destination", 80, 60),
        "waste": _vial("waste", 100, 60),
    }
    if blocker:
        labware["blocker"] = _vial("blocker", 80, 60, size=16)
    deck = _build_deck_from_raw({
        "motion_planning": {"clearance_mm": 1},
        "labware": labware,
    })
    instruments = {"pipette": FakePipette()}
    instrument_configs = {"pipette": {"type": "pipette", "vendor": "fake"}}
    motion_envelopes = {
        "pipette": {
            "box": {
                "offset": {"x": -2, "y": -2, "z": 0},
                "size": {"x": 4, "y": 4, "z": 20},
            },
            "attached_tip_radius_mm": 1,
        }
    }
    if with_camera:
        instruments["camera"] = FakeCamera()
        instrument_configs["camera"] = {"type": "camera", "vendor": "fake"}
        motion_envelopes["camera"] = {
            "box": {
                "offset": {"x": -2, "y": -2, "z": 0},
                "size": {"x": 4, "y": 4, "z": 20},
            },
        }
    config = GantryConfig(
        serial_port="offline",
        gantry_type=GantryType.CUB,
        factory_z_travel_mm=80,
        working_volume=WorkingVolume(0, 120, 0, 100, 0, 80),
        safe_z=80,
        instruments=instrument_configs,
        motion_envelopes=motion_envelopes,
    )
    controller = FakeController()
    pipette = instruments["pipette"]
    gantry = InstrumentedGantry(
        controller,
        instruments,
        safe_z=80,
        motion_envelopes=config.motion_envelopes,
    )
    return ProtocolContext(gantry=gantry, deck=deck, gantry_config=config), controller, pipette


def _native_protocol():
    return compile_protocol([
        CommandCall("pick_up_tip", {"position": "tips.A1"}),
        CommandCall("transfer", {
            "source": "source",
            "destination": "destination",
            "volume_ul": 100,
        }),
        CommandCall("mix", {"position": "destination", "volume_ul": 50, "cycles": 1}),
        CommandCall("drop_tip", {"position": "waste"}),
    ])


def _camera_capture_protocol():
    calls = [CommandCall("pick_up_tip", {"position": "tips.A1"})]
    for index in range(1, 4):
        calls.extend([
            CommandCall("transfer", {
                "source": "source",
                "destination": "destination",
                "volume_ul": 100,
            }),
            CommandCall("move", {
                "instrument": "camera",
                "position": "destination",
            }),
            CommandCall("capture", {
                "instrument": "camera",
                "position": "destination",
                "label": f"destination_after_dispense_{index}",
            }),
        ])
    calls.append(CommandCall("drop_tip", {"position": "waste"}))
    return compile_protocol(calls)


def test_native_planned_commands_execute_preflighted_exact_segments() -> None:
    context, controller, pipette = _context()

    _native_protocol().execute(context)

    assert pipette.actions == [
        "pick_up_tip",
        "aspirate:100.0",
        "dispense:100.0",
        "aspirate:50.0",
        "dispense:50.0",
        "aspirate:50.0",
        "dispense:50.0",
        "drop_tip",
    ]
    assert controller.moves
    assert all(travel_z is None for *_xyz, travel_z in controller.moves)
    serialized = context.serialized_motion_plans()
    assert serialized
    assert any(plan["state_changes"] for plan in serialized)
    assert all(
        segment["axis"] in {"x", "y", "z", "xy"}
        for plan in serialized
        for segment in plan["segments"]
    )


def test_transfer_destination_no_route_rejects_before_pickup_or_aspirate() -> None:
    context, controller, pipette = _context(blocker=True)

    with pytest.raises(ProtocolExecutionError, match="preflight failed"):
        _native_protocol().execute(context)

    assert controller.moves == []
    assert pipette.actions == []
    assert pipette.attached_tip_extension == 0


def test_unsupported_command_rejects_before_earlier_supported_movement() -> None:
    context, controller, pipette = _context()
    protocol = compile_protocol([
        CommandCall("pick_up_tip", {"position": "tips.A1"}),
        CommandCall("home", {}),
    ])

    with pytest.raises(ProtocolExecutionError, match=r"step 1 \(home\)"):
        protocol.execute(context)

    assert controller.moves == []
    assert pipette.actions == []


def test_measure_remains_unsupported_for_planning_before_prior_movement() -> None:
    context, controller, pipette = _context()
    protocol = compile_protocol([
        CommandCall("pick_up_tip", {"position": "tips.A1"}),
        CommandCall("measure", {
            "instrument": "pipette",
            "position": "destination",
            "measurement_height": 0.0,
        }),
    ])

    with pytest.raises(ProtocolExecutionError, match=r"step 1 \(measure\)"):
        protocol.execute(context)

    assert controller.moves == []
    assert pipette.actions == []


@pytest.mark.parametrize("instrument", ["pipette", "missing"])
def test_invalid_planned_capture_rejects_before_prior_movement(instrument) -> None:
    context, controller, pipette = _context(with_camera=True)
    protocol = compile_protocol([
        CommandCall("pick_up_tip", {"position": "tips.A1"}),
        CommandCall("capture", {"instrument": instrument}),
    ])

    with pytest.raises(ProtocolExecutionError, match="capture instrument"):
        protocol.execute(context)

    assert controller.moves == []
    assert pipette.actions == []


def test_invalid_capture_position_rejects_before_prior_movement() -> None:
    context, controller, pipette = _context(with_camera=True)
    protocol = compile_protocol([
        CommandCall("pick_up_tip", {"position": "tips.A1"}),
        CommandCall("capture", {
            "instrument": "camera",
            "position": "missing.A1",
        }),
    ])

    with pytest.raises(ProtocolExecutionError, match="missing.A1"):
        protocol.execute(context)

    assert controller.moves == []
    assert pipette.actions == []


def test_transfer_camera_capture_cycles_use_safe_tool_handoffs(
    tmp_path,
) -> None:
    context, controller, pipette = _context(with_camera=True)
    context.image_output_dir = tmp_path / "images"
    protocol = _camera_capture_protocol()

    results = protocol.execute(context)

    camera = context.gantry.instruments["camera"]
    assert len(camera.captures) == 3
    assert len(set(camera.captures)) == 3
    assert all(Path(path).is_file() for path in camera.captures)
    assert [results[index] for index in (3, 6, 9)] == camera.captures
    for capture_index in (3, 6, 9):
        assert context.planned_motion_steps[capture_index].plans == ()
    for move_index in (2, 5, 8):
        segments = context.planned_motion_steps[move_index].plans[0].segments
        departure = segments[0]
        assert departure.axis == "z"
        assert departure.access.allowed_fixture_names == ("destination",)
        assert departure.access.allowed_tool_names == ("pipette",)
        assert departure.tool_state.instrument == "pipette"
        assert departure.tool_state.attached_tip_extension_mm == 20
        assert any(
            segment.access.allowed_tool_names == ("camera",)
            for segment in segments[1:]
        )
    assert len(controller.moves) == sum(
        len(plan.segments) for plan in context.motion_plans
    )
    assert pipette.actions.count("dispense:100.0") == 3


def test_later_unroutable_camera_move_rejects_before_pickup() -> None:
    context, controller, pipette = _context(with_camera=True)
    context.gantry.instruments["camera"].offset_x = -100.0

    with pytest.raises(ProtocolExecutionError, match="preflight failed"):
        _camera_capture_protocol().execute(context)

    assert controller.moves == []
    assert pipette.actions == []


def test_cached_bare_plan_rejects_unexpected_attached_tip_before_motion() -> None:
    context, controller, pipette = _context()
    protocol = _native_protocol()
    prepare_planning_context(protocol, context)
    pipette.set_attached_tip_extension(20)

    with pytest.raises(ProtocolExecutionError, match="tool state does not match"):
        context.routing_session.execute(context.motion_plans[0])

    assert controller.moves == []


def test_planner_owned_move_order_rejects_travel_z_before_prior_motion() -> None:
    context, controller, pipette = _context()
    protocol = compile_protocol([
        CommandCall("pick_up_tip", {"position": "tips.A1"}),
        CommandCall("move", {
            "instrument": "pipette",
            "position": [50, 50, 50],
            "travel_z": 70,
        }),
    ])

    with pytest.raises(ProtocolExecutionError, match="rejects travel_z"):
        protocol.execute(context)

    assert controller.moves == []
    assert pipette.actions == []


def test_require_uncapped_rejects_before_prior_planned_pickup() -> None:
    context, controller, pipette = _context()
    protocol = compile_protocol([
        CommandCall("pick_up_tip", {"position": "tips.A1"}),
        CommandCall("transfer", {
            "source": "source",
            "destination": "destination",
            "volume_ul": 100,
            "require_uncapped": ["source"],
        }),
    ])

    with pytest.raises(ProtocolExecutionError, match="does not support require_uncapped"):
        protocol.execute(context)

    assert controller.moves == []
    assert pipette.actions == []


def test_repeated_explicit_tip_rejects_whole_protocol_before_motion() -> None:
    context, controller, pipette = _context()
    protocol = compile_protocol([
        CommandCall("pick_up_tip", {"position": "tips.A1"}),
        CommandCall("drop_tip", {"position": "waste"}),
        CommandCall("pick_up_tip", {"position": "tips.A1"}),
    ])

    with pytest.raises(ProtocolExecutionError, match="tips.A1.*unavailable"):
        protocol.execute(context)

    assert controller.moves == []
    assert pipette.actions == []


def test_consumed_tip_is_absent_from_later_unscoped_transit_scene(monkeypatch) -> None:
    context, _controller, _pipette = _context()
    observed_fixture_sets = []
    original = RoutingSession._scene_and_access

    def recording_scene(self, instrument, targets, consumed, start, end, state):
        scene, access = original(
            self, instrument, targets, consumed, start, end, state,
        )
        if "tips.tip.A1" in consumed and not targets:
            observed_fixture_sets.append({fixture.name for fixture in scene.fixtures})
        return scene, access

    monkeypatch.setattr(RoutingSession, "_scene_and_access", recording_scene)

    prepare_planning_context(_native_protocol(), context)

    assert observed_fixture_sets
    assert all("tips.tip.A1" not in names for names in observed_fixture_sets)


def test_durable_tip_state_removes_consumed_side_exit_blockers() -> None:
    context, controller, _pipette = _context()
    rack = context.deck["tips"]
    coordinate_type = type(rack.tips["A1"])
    rack.tips["A2"] = coordinate_type(x=30, y=20, z=50)
    rack.tip_present["A2"] = True
    context.fluid_state_id = 17
    context.data_store = Mock()
    context.data_store.get_tip_snapshot.return_value = {
        "containers": [
            {"rack_key": "tips", "slot_id": "A1", "status": "consumed"},
            {"rack_key": "tips", "slot_id": "A2", "status": "available"},
        ],
    }
    protocol = compile_protocol([
        CommandCall("pick_up_tip", {"position": "tips.A2"}),
        CommandCall("drop_tip", {"position": "waste"}),
    ])

    prepare_planning_context(protocol, context)

    assert context.planned_motion_steps[0].data["position"] == "tips.A2"
    assert controller.moves == []


def test_durable_tip_state_rejects_consumed_explicit_target_before_motion() -> None:
    context, controller, _pipette = _context()
    context.fluid_state_id = 17
    context.data_store = Mock()
    context.data_store.get_tip_snapshot.return_value = {
        "containers": [
            {"rack_key": "tips", "slot_id": "A1", "status": "consumed"},
        ],
    }

    with pytest.raises(ProtocolExecutionError, match="tips.A1.*unavailable"):
        prepare_planning_context(_native_protocol(), context)

    assert controller.moves == []


def test_two_routed_trials_deplete_shared_state_and_measure_unique_wells(
    tmp_path, monkeypatch,
) -> None:
    from cubos.protocol_engine.commands import camera as camera_commands

    def analyze(path, **kwargs):
        target = Path(path).stem
        return {
            "image_path": str(path),
            "measurement_status": "accepted",
            "comparison_status": "not_requested",
            "quality": {"accepted": True, "flags": []},
            "lab": [40.0 if "first" in target else 41.0, 1.0, 2.0],
            "processing_profile": {
                "schema": "cubos.camera-well-cielab.v1",
                "id": "offline-profile",
            },
        }

    monkeypatch.setattr(camera_commands, "analyze_color_image", analyze)
    deck_path = tmp_path / "deck.yaml"
    deck_path.write_text("labware: {}\n")
    store = DataStore(tmp_path / "state.db")
    try:
        first_context, _, _ = _context(with_camera=True)
        first_context.image_output_dir = tmp_path / "images"
        first_rack = first_context.deck["tips"]
        coordinate_type = type(first_rack.tips["A1"])
        first_rack.tips["A2"] = coordinate_type(x=30, y=20, z=50)
        first_rack.tip_present["A2"] = True
        state_id = store.create_fluid_state(deck_path, first_context.deck)
        first_campaign = store.create_campaign("trial 1", fluid_state_id=state_id)
        first_context.data_store = store
        first_context.fluid_state_id = state_id
        first_context.campaign_id = first_campaign
        first_results = compile_protocol([
            CommandCall("pick_up_tip", {"position": "tips.A1"}),
            CommandCall("drop_tip", {"position": "waste"}),
            CommandCall("move", {"instrument": "camera", "position": "destination"}),
            CommandCall("measure_color", {
                "instrument": "camera", "position": "destination",
                "label": "first", "image_height": 10.0,
                "expected_center": (0.5, 0.5),
                "expected_center_source": "operator_selected",
            }),
        ]).execute(first_context)

        second_context, _, _ = _context(with_camera=True)
        second_context.image_output_dir = tmp_path / "images"
        second_rack = second_context.deck["tips"]
        second_rack.tips["A2"] = coordinate_type(x=30, y=20, z=50)
        second_rack.tip_present["A2"] = True
        store.resume_fluid_state(state_id, deck_path, second_context.deck)
        second_campaign = store.create_campaign("trial 2", fluid_state_id=state_id)
        second_context.data_store = store
        second_context.fluid_state_id = state_id
        second_context.campaign_id = second_campaign
        second_results = compile_protocol([
            CommandCall("pick_up_tip", {"position": "tips.A2"}),
            CommandCall("drop_tip", {"position": "waste"}),
            CommandCall("move", {"instrument": "camera", "position": "source"}),
            CommandCall("measure_color", {
                "instrument": "camera", "position": "source",
                "label": "second", "image_height": 10.0,
                "expected_center": (0.5, 0.5),
                "expected_center_source": "operator_selected",
            }),
        ]).execute(second_context)

        statuses = {
            item["slot_id"]: item["status"]
            for item in store.get_tip_snapshot(state_id)["containers"]
        }
        assert statuses == {"A1": "consumed", "A2": "consumed"}
        assert first_results[-1]["measurement_status"] == "accepted"
        assert second_results[-1]["measurement_status"] == "accepted"
        assert first_results[-1]["lab"] != second_results[-1]["lab"]
    finally:
        store.close()


def test_measure_color_image_height_preplans_descent_and_safe_retract() -> None:
    context, controller, _pipette = _context(with_camera=True)
    protocol = compile_protocol([
        CommandCall("move", {"instrument": "camera", "position": "destination"}),
        CommandCall("measure_color", {
            "instrument": "camera",
            "position": "destination",
            "image_height": 10.0,
        }),
    ])

    prepare_planning_context(protocol, context)

    measured = context.planned_motion_steps[1]
    assert measured.data == {
        "instrument": "camera",
        "capture_after_plan": 0,
        "image_height": 10.0,
        "position": "destination",
    }
    assert len(measured.plans) == 2
    assert measured.plans[0].end.z < context.gantry_config.working_volume.z_max
    assert measured.plans[1].end.z == context.gantry_config.working_volume.z_max
    assert controller.moves == []


def test_measure_color_height_checks_all_mounted_tool_state_before_z_motion() -> None:
    context, controller, pipette = _context(with_camera=True)
    protocol = compile_protocol([
        CommandCall("move", {"instrument": "camera", "position": "destination"}),
        CommandCall("measure_color", {
            "instrument": "camera",
            "position": "destination",
            "image_height": 10.0,
        }),
    ])
    prepare_planning_context(protocol, context)
    pipette.set_attached_tip_extension(20)

    with pytest.raises(ProtocolExecutionError, match="tool state does not match"):
        context.routing_session.execute(context.planned_motion_steps[1].plans[0])

    assert controller.moves == []


def test_native_move_executes_cached_intermediate_detour_when_both_direct_orders_block() -> None:
    def tall_blocker(name, x, y):
        value = _vial(name, x, y)
        value["motion"]["box"] = {
            "anchor": "location",
            "offset": {"x": -5, "y": -5, "z": -40},
            "size": {"x": 10, "y": 10, "z": 140},
        }
        return value

    deck = _build_deck_from_raw({
        "motion_planning": {"clearance_mm": 1},
        "labware": {
            "blocks_x_first": tall_blocker("blocks x first", 60, 20),
            "blocks_y_first": tall_blocker("blocks y first", 20, 60),
        },
    })
    config = GantryConfig(
        serial_port="offline",
        gantry_type=GantryType.CUB,
        factory_z_travel_mm=100,
        working_volume=WorkingVolume(0, 100, 0, 100, 0, 80),
        safe_z=80,
        instruments={"pipette": {"type": "pipette", "vendor": "fake"}},
        motion_envelopes={
            "pipette": {
                "box": {
                    "offset": {"x": -2, "y": -2, "z": 0},
                    "size": {"x": 4, "y": 4, "z": 20},
                },
                "attached_tip_radius_mm": 1,
            }
        },
    )
    controller = FakeController()
    controller.coords = {"x": 20.0, "y": 20.0, "z": 80.0}
    pipette = FakePipette()
    pipette.depth = 0.0
    gantry = InstrumentedGantry(
        controller,
        {"pipette": pipette},
        safe_z=80,
        motion_envelopes=config.motion_envelopes,
    )
    context = ProtocolContext(gantry=gantry, deck=deck, gantry_config=config)
    protocol = compile_protocol([
        CommandCall("move", {
            "instrument": "pipette",
            "position": [80, 80, 80],
        }),
    ])

    protocol.execute(context)

    plan = context.motion_plans[0]
    assert "detour:" in plan.strategy
    assert len(plan.segments) == 3
    assert [move[:3] for move in controller.moves] == [
        (segment.end.x, segment.end.y, segment.end.z)
        for segment in plan.segments
    ]
    assert all(move[3] is None for move in controller.moves)
    assert all(
        segment.access.allowed_tool_names == ("pipette",)
        and not segment.access.allowed_fixture_names
        and not segment.access.allowed_corridor_names
        for segment in plan.segments
    )


def _write_planned_move_files(tmp_path):
    gantry = tmp_path / "gantry.yaml"
    gantry.write_text("""
serial_port: offline
gantry_type: cub
cnc:
  factory_z_travel_mm: 100
working_volume:
  x_min: 0
  x_max: 120
  y_min: 0
  y_max: 100
  z_min: 0
  z_max: 80
instruments:
  pipette:
    type: pipette
    vendor: opentrons
    depth: 0
    motion_envelope:
      box:
        offset: {x: -2, y: -2, z: 0}
        size: {x: 4, y: 4, z: 20}
      attached_tip_radius_mm: 1
""")
    deck = tmp_path / "deck.yaml"
    deck.write_text("motion_planning: {clearance_mm: 1}\nlabware: {}\n")
    protocol = tmp_path / "protocol.yaml"
    protocol.write_text("""
positions:
  park: [50, 50, 40]
protocol:
  - move:
      instrument: pipette
      position: park
""")
    return gantry, deck, protocol


def test_offline_setup_validation_returns_the_shared_serialized_plans(tmp_path) -> None:
    gantry, deck, protocol = _write_planned_move_files(tmp_path)

    result = run_setup_validation(gantry, deck, protocol)

    assert result.passed
    assert result.motion_plans
    assert "immutable plan" in result.output


def test_setup_protocol_prepares_plans_from_nominal_offline_pose(tmp_path) -> None:
    gantry, deck, protocol = _write_planned_move_files(tmp_path)

    loaded, context = setup_protocol(gantry, deck, protocol, mock_mode=True)

    assert len(loaded) == 1
    assert context.routing_session is not None
    assert context.motion_plans


def test_hardware_orchestrator_plans_before_offline_instrument_connect(tmp_path) -> None:
    gantry_path, deck, protocol = _write_planned_move_files(tmp_path)

    class Store:
        def __init__(self):
            self.finished = []

        def get_campaign_fluid_state_id(self, campaign_id):
            return None

        def finish_campaign(self, campaign_id, status):
            self.finished.append((campaign_id, status))

    store = Store()
    results = run_on_hardware(
        gantry_path,
        deck,
        protocol,
        gantry=Gantry(offline=True),
        mock_mode=True,
        data_store=store,
        campaign_id=7,
    )

    assert results == [None]
    assert store.finished == [(7, "completed")]
