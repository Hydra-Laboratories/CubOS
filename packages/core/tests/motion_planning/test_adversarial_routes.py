"""Adversarial invariants for collision-aware route planning."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cubos.gantry.instrument_mount import InstrumentedGantry
from cubos.motion_planning import (
    AABB,
    AccessScope,
    Corridor,
    InvalidGeometryError,
    MotionPlan,
    MotionSegment,
    NoRouteError,
    Point3D,
    ToolEnvelope,
    ToolState,
    ToolStateChange,
    build_scene,
    plan_motion,
)
from cubos.protocol_engine.setup import run_on_hardware


def _box(
    name: str,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    z_min: float,
    z_max: float,
) -> AABB:
    return AABB(
        name=name,
        minimum=Point3D(x_min, y_min, z_min),
        maximum=Point3D(x_max, y_max, z_max),
    )


def _tool(name: str = "pipette", *, tip_radius: float | None = None) -> ToolEnvelope:
    return ToolEnvelope(
        name=name,
        box=_box(f"{name}-body", -0.25, 0.25, -0.25, 0.25, -0.25, 0.25),
        attached_tip_radius_mm=tip_radius,
    )


def _swept(relative: AABB, start: Point3D, end: Point3D, *, name: str) -> AABB:
    return AABB(
        name=name,
        minimum=Point3D(
            min(start.x, end.x) + relative.x_min,
            min(start.y, end.y) + relative.y_min,
            min(start.z, end.z) + relative.z_min,
        ),
        maximum=Point3D(
            max(start.x, end.x) + relative.x_max,
            max(start.y, end.y) + relative.y_max,
            max(start.z, end.z) + relative.z_max,
        ),
    )


class _RecordingController:
    config = SimpleNamespace(working_volume=None)

    def __init__(self, coordinates: object) -> None:
        self.coordinates = coordinates
        self.calls: list[tuple[float, float, float, object]] = []

    def get_coordinates(self):
        return self.coordinates

    def move_to(self, x: float, y: float, z: float, *, travel_z=None) -> None:
        self.calls.append((x, y, z, travel_z))
        self.coordinates = {"x": x, "y": y, "z": z}


def test_saved_deck_low_z_transit_selects_y_first_around_tall_rack() -> None:
    rack = _box("tips", 139.456, 263.456, -2.965, 81.035, 28.5, 91.5)
    pipette = _tool(tip_radius=2.5)
    scene = build_scene(
        bounds=_box("calibrated-carriage", 0, 258.205, 0, 144.645, 10.5, 66.5),
        fixtures=(rack,),
        tool_envelopes=(pipette,),
        clearance_mm=2,
    )
    start = Point3D(25.803, 70.035, 66.5)
    end = Point3D(230, 129, 66.5)
    attached = ToolState(instrument="pipette", attached_tip_extension_mm=70)

    plan = plan_motion(
        scene,
        start,
        end,
        tool_state=attached,
        command="drop_tip",
        operation="transit-to-waste",
    )

    naive_x_end = Point3D(end.x, start.y, start.z)
    attached_tip = _box("attached-tip", -2.5, 2.5, -2.5, 2.5, -70, 0)
    assert _swept(
        attached_tip, start, naive_x_end, name="naive-x-first",
    ).intersects(rack.expanded(scene.clearance_mm))
    assert plan.strategy == "y_first"
    assert [segment.axis for segment in plan.segments] == ["y", "x"]
    for index, segment in enumerate(plan.segments):
        assert not _swept(
            pipette.box, segment.start, segment.end, name=f"body-{index}",
        ).intersects(rack.expanded(scene.clearance_mm))
        assert not _swept(
            attached_tip, segment.start, segment.end, name=f"tip-{index}",
        ).intersects(rack.expanded(scene.clearance_mm))


def test_saved_rack_legacy_exit_is_inside_conservative_collision_clearance() -> None:
    rack_x_min = 139.456
    scene = build_scene(
        bounds=_box("calibrated-carriage", 0, 258.205, 0, 144.645, 10.5, 66.5),
        fixtures=(
            _box("tips", rack_x_min, 263.456, -2.965, 81.035, 28.5, 91.5),
        ),
        tool_envelopes=(_tool(tip_radius=2.5),),
        clearance_mm=2,
    )
    attached = ToolState(instrument="pipette", attached_tip_extension_mm=70)

    with pytest.raises(NoRouteError, match="stationary carriage pose is in collision"):
        plan_motion(scene, Point3D(140, 75.035, 58.5), Point3D(140, 75.035, 58.5), tool_state=attached)

    derived_exit_x = rack_x_min - scene.clearance_mm - 2.5 - 1e-6
    plan = plan_motion(
        scene,
        Point3D(derived_exit_x, 75.035, 58.5),
        Point3D(derived_exit_x, 75.035, 58.5),
        tool_state=attached,
    )
    assert plan.strategy == "stationary"


def test_target_corridor_never_authorizes_overlap_with_another_fixture() -> None:
    target = _box("tips", 4, 6, 4, 6, 4, 6)
    non_target_barrier = _box("neighbor", 4, 6, 0, 10, 0, 20)
    corridor = Corridor(
        name="tips-a1-exit",
        box=_box("tips-a1-exit-box", 0, 10, 0, 10, 0, 20),
        fixture_names=("tips",),
    )
    scene = build_scene(
        bounds=_box("working-volume", 0, 10, 0, 10, 0, 20),
        fixtures=(target, non_target_barrier),
        tool_envelopes=(_tool(),),
        corridors=(corridor,),
    )
    target_only = AccessScope(
        allowed_fixture_names=("tips",),
        allowed_corridor_names=("tips-a1-exit",),
        allowed_tool_names=("pipette",),
    )

    with pytest.raises(NoRouteError):
        plan_motion(
            scene,
            Point3D(2, 5, 5),
            Point3D(8, 5, 5),
            operation="side-exit",
            access=target_only,
        )


def test_plan_state_change_must_match_the_exact_segment_boundary() -> None:
    bare = ToolState(instrument="pipette")
    attached = ToolState(instrument="pipette", attached_tip_extension_mm=70)
    engage = Point3D(2, 2, 2)
    lifted = Point3D(2, 2, 3)
    bare_segment = MotionSegment(
        kind="axis_aligned",
        start=Point3D(2, 2, 3),
        end=engage,
        phase="engage",
        tool_state=bare,
    )
    attached_segment = MotionSegment(
        kind="axis_aligned",
        start=engage,
        end=lifted,
        phase="lift",
        tool_state=attached,
    )

    plan = MotionPlan(
        command="pick_up_tip",
        operation="side_exit",
        start=bare_segment.start,
        end=attached_segment.end,
        strategy="vertical-then-side-exit",
        segments=(bare_segment, attached_segment),
        state_changes=(ToolStateChange(engage, "pipette", 70),),
    )
    assert plan.state_changes[0].position == bare_segment.end == attached_segment.start

    with pytest.raises(InvalidGeometryError, match="ToolStateChange|tool-state|state change"):
        MotionPlan(
            command="pick_up_tip",
            operation="side_exit",
            start=bare_segment.start,
            end=attached_segment.end,
            strategy="vertical-then-side-exit",
            segments=(bare_segment, attached_segment),
        )

    with pytest.raises(InvalidGeometryError, match="ToolStateChange|tool-state|state change"):
        MotionPlan(
            command="pick_up_tip",
            operation="side_exit",
            start=bare_segment.start,
            end=attached_segment.end,
            strategy="vertical-then-side-exit",
            segments=(bare_segment, attached_segment),
            state_changes=(ToolStateChange(engage, "pipette", 50),),
        )

    with pytest.raises(InvalidGeometryError, match="state change"):
        MotionPlan(
            command="pick_up_tip",
            operation="side_exit",
            start=bare_segment.start,
            end=attached_segment.end,
            strategy="vertical-then-side-exit",
            segments=(bare_segment, attached_segment),
            state_changes=(
                ToolStateChange(Point3D(9, 9, 9), "pipette", 70),
            ),
        )


def test_planner_is_deterministic_under_scene_input_permutation() -> None:
    bounds = _box("working-volume", 0, 10, 0, 10, 0, 20)
    fixtures = (
        _box("x-first-blocker", 4, 6, 0, 2, 4, 6),
        _box("y-first-blocker", 0, 2, 4, 6, 4, 6),
    )
    tools = (
        _tool("pipette"),
        _tool("camera"),
    )
    forward = build_scene(bounds=bounds, fixtures=fixtures, tool_envelopes=tools)
    reversed_scene = build_scene(
        bounds=bounds,
        fixtures=reversed(fixtures),
        tool_envelopes=reversed(tools),
    )

    args = (Point3D(1, 1, 5), Point3D(9, 9, 5))
    assert plan_motion(forward, *args).to_dict() == plan_motion(reversed_scene, *args).to_dict()


def test_exact_segment_execution_rejects_controller_pose_drift_before_motion() -> None:
    controller = _RecordingController({"x": 0.0, "y": 0.0, "z": 0.0})
    gantry = InstrumentedGantry(controller)

    with pytest.raises(ValueError, match="start|current|drift"):
        gantry.move_carriage_exact((1, 1, 1), (2, 1, 1))
    assert controller.calls == []


@pytest.mark.parametrize(
    "coordinates",
    [
        {"x": float("nan"), "y": 1.0, "z": 1.0},
        {"x": float("inf"), "y": 1.0, "z": 1.0},
        {"x": 1.0, "y": 1.0},
        None,
    ],
)
def test_exact_segment_execution_rejects_invalid_readback_before_motion(
    coordinates: object,
) -> None:
    controller = _RecordingController(coordinates)
    gantry = InstrumentedGantry(controller)

    with pytest.raises(ValueError, match="current|verify|finite|match|controller"):
        gantry.move_carriage_exact((1, 1, 1), (2, 1, 1))
    assert controller.calls == []


def test_exact_segment_execution_emits_only_the_planned_axis_endpoint() -> None:
    controller = _RecordingController({"x": 1.0, "y": 1.0, "z": 1.0})
    gantry = InstrumentedGantry(controller)

    gantry.move_carriage_exact((1, 1, 1), (2, 1, 1))

    assert controller.calls == [(2.0, 1.0, 1.0, None)]


def test_executed_endpoints_equal_serialized_plan_without_hidden_moves() -> None:
    scene = build_scene(
        bounds=_box("working-volume", 0, 10, 0, 10, 0, 10),
        fixtures=(),
        tool_envelopes=(_tool(),),
    )
    plan = plan_motion(scene, Point3D(1, 1, 1), Point3D(3, 4, 2))
    controller = _RecordingController({"x": 1.0, "y": 1.0, "z": 1.0})
    gantry = InstrumentedGantry(controller)

    for segment in plan.segments:
        gantry.move_carriage_exact(segment.start, segment.end)

    assert controller.calls == [
        (segment.end.x, segment.end.y, segment.end.z, None)
        for segment in plan.segments
    ]


def test_clear_xy_plan_executes_as_one_exact_controller_move() -> None:
    scene = build_scene(
        bounds=_box("working-volume", 0, 10, 0, 10, 0, 10),
        fixtures=(),
        tool_envelopes=(_tool(),),
    )
    plan = plan_motion(scene, Point3D(1, 1, 5), Point3D(3, 4, 5))
    controller = _RecordingController({"x": 1.0, "y": 1.0, "z": 5.0})
    gantry = InstrumentedGantry(controller)

    assert [segment.axis for segment in plan.segments] == ["xy"]
    gantry.move_carriage_exact(plan.segments[0].start, plan.segments[0].end)

    assert controller.calls == [(3.0, 4.0, 5.0, None)]


def test_exact_segment_execution_accepts_coordinated_xy() -> None:
    controller = _RecordingController({"x": 1.0, "y": 1.0, "z": 1.0})
    gantry = InstrumentedGantry(controller)

    gantry.move_carriage_exact((1, 1, 1), (2, 2, 1))

    assert controller.calls == [(2.0, 2.0, 1.0, None)]


def test_exact_segment_execution_rejects_xy_plus_z_before_motion() -> None:
    controller = _RecordingController({"x": 1.0, "y": 1.0, "z": 1.0})
    gantry = InstrumentedGantry(controller)

    with pytest.raises(ValueError, match="one axis or coordinated XY"):
        gantry.move_carriage_exact((1, 1, 1), (2, 2, 2))
    assert controller.calls == []


def test_planning_failure_never_triggers_legacy_automatic_retract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cubos.protocol_engine import routing as routing_module
    from cubos.protocol_engine import setup as setup_module

    events: list[str] = []

    class ControllerGantry:
        def connect(self) -> None:
            events.append("connect")

        def get_status(self) -> str:
            return "Idle"

        def is_healthy(self) -> bool:
            return True

        def disconnect(self) -> None:
            events.append("disconnect")

    class PlannedGantry:
        def connect_instruments(self) -> None:
            events.append("connect-instruments")

        def disconnect_instruments(self) -> None:
            events.append("disconnect-instruments")

    class Store:
        def __init__(self) -> None:
            self.finished: list[tuple[int, str]] = []

        def get_campaign_fluid_state_id(self, campaign_id: int):
            return None

        def finish_campaign(self, campaign_id: int, status: str) -> None:
            self.finished.append((campaign_id, status))

    context = SimpleNamespace(
        deck=SimpleNamespace(planning_enabled=True),
        gantry=PlannedGantry(),
        data_store=None,
        campaign_id=None,
        fluid_state_id=None,
    )

    class Protocol:
        def execute(self, runtime_context) -> None:
            assert runtime_context is context
            raise RuntimeError("planned segment failed")

    protocol = Protocol()
    store = Store()
    controller = ControllerGantry()
    monkeypatch.setattr(
        setup_module,
        "setup_protocol",
        lambda *args, **kwargs: (protocol, context),
    )
    monkeypatch.setattr(
        routing_module,
        "prepare_planning_context",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        setup_module,
        "_best_effort_retract_to_safe_z",
        lambda *args, **kwargs: events.append("retract"),
    )

    with pytest.raises(RuntimeError, match="planned segment failed"):
        run_on_hardware(
            "unused-gantry.yaml",
            "unused-deck.yaml",
            protocol,
            gantry=controller,
            data_store=store,
            campaign_id=7,
        )

    assert "retract" not in events
    assert events == [
        "connect",
        "connect-instruments",
        "disconnect-instruments",
        "disconnect",
    ]
    assert store.finished == [(7, "failed")]
