"""Pure motion-planner geometry, routing, and serialization tests."""

from __future__ import annotations

import math

import pytest

import cubos.motion_planning.planner as planner_module
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


def _box(
    name: str,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    z_min: float = 0.0,
    z_max: float = 10.0,
) -> AABB:
    return AABB(
        name=name,
        minimum=Point3D(x_min, y_min, z_min),
        maximum=Point3D(x_max, y_max, z_max),
    )


def _tool(
    name: str = "pipette",
    *,
    z_min: float = -0.25,
    z_max: float = 0.25,
    radius: float | None = None,
) -> ToolEnvelope:
    return ToolEnvelope(
        name=name,
        box=_box(f"{name}-body", -0.25, 0.25, -0.25, 0.25, z_min, z_max),
        attached_tip_radius_mm=radius,
    )


def _scene(
    *fixtures: AABB,
    tools: tuple[ToolEnvelope, ...] | None = None,
    clearance_mm: float = 0.0,
    corridors: tuple[Corridor, ...] = (),
):
    return build_scene(
        bounds=_box("working-volume", 0, 10, 0, 10, 0, 20),
        fixtures=fixtures,
        tool_envelopes=tools or (_tool(),),
        clearance_mm=clearance_mm,
        corridors=corridors,
    )


def test_clear_unscoped_xy_transit_uses_one_coordinated_segment():
    plan = plan_motion(
        _scene(),
        Point3D(1, 1, 5),
        Point3D(9, 9, 5),
    )

    assert plan.strategy == "coordinated_xy"
    assert len(plan.segments) == 1
    assert plan.segments[0].kind == "coordinated_xy"
    assert plan.segments[0].axis == "xy"
    assert plan.segments[0].length_mm == pytest.approx(math.sqrt(128))


def test_tool_attribution_without_fixture_scope_allows_coordinated_xy():
    plan = plan_motion(
        _scene(),
        Point3D(1, 1, 5),
        Point3D(9, 9, 5),
        access=AccessScope(allowed_tool_names=("pipette",)),
    )

    assert plan.strategy == "coordinated_xy"
    assert len(plan.segments) == 1
    assert plan.segments[0].axis == "xy"
    assert plan.segments[0].access.allowed_tool_names == ("pipette",)


def test_y_first_is_selected_when_x_first_crosses_a_fixture():
    scene = _scene(_box("rack", 4, 6, 0, 2, 4, 6))

    plan = plan_motion(
        scene,
        Point3D(1, 1, 5),
        Point3D(9, 9, 5),
        command="drop_tip",
        operation="transit-to-waste",
    )

    assert plan.strategy == "y_first"
    assert all(segment.axis != "xy" for segment in plan.segments)
    assert [segment.axis for segment in plan.segments] == ["y", "x"]
    assert plan.segments[0].start == Point3D(1, 1, 5)
    assert plan.segments[-1].end == Point3D(9, 9, 5)


def test_scoped_access_keeps_axis_aligned_approach():
    fixture = _box("rack", 8, 9, 8, 9, 4, 6)
    corridor = Corridor(
        "rack-access",
        _box("rack-access-box", 0, 10, 0, 10, 4, 6),
        ("rack",),
    )
    plan = plan_motion(
        _scene(fixture, corridors=(corridor,)),
        Point3D(1, 1, 5),
        Point3D(9, 9, 5),
        access=AccessScope(
            allowed_fixture_names=("rack",),
            allowed_corridor_names=("rack-access",),
            allowed_tool_names=("pipette",),
        ),
    )

    assert plan.strategy in {"x_first", "y_first"}
    assert all(segment.axis in {"x", "y"} for segment in plan.segments)


def test_obstacle_edge_detour_is_selected_when_both_direct_orders_are_blocked():
    scene = _scene(
        _box("x-first-blocker", 4, 6, 0, 2, 4, 6),
        _box("y-first-blocker", 0, 2, 4, 6, 4, 6),
    )

    plan = plan_motion(scene, Point3D(1, 1, 5), Point3D(9, 9, 5))

    assert plan.strategy == "detour:x-first-blocker:pipette:x_min"
    assert [segment.axis for segment in plan.segments] == ["x", "y", "x"]
    assert plan.segments[0].end.x < 3.75
    assert plan.segments[-1].end == Point3D(9, 9, 5)


def test_tool_attribution_alone_still_allows_obstacle_edge_detours():
    scene = _scene(
        _box("x-first-blocker", 4, 6, 0, 2, 4, 6),
        _box("y-first-blocker", 0, 2, 4, 6, 4, 6),
    )

    plan = plan_motion(
        scene,
        Point3D(1, 1, 5),
        Point3D(9, 9, 5),
        access=AccessScope(allowed_tool_names=("pipette",)),
    )

    assert plan.strategy == "detour:x-first-blocker:pipette:x_min"
    assert [segment.axis for segment in plan.segments] == ["x", "y", "x"]
    assert all(
        segment.access.allowed_tool_names == ("pipette",)
        for segment in plan.segments
    )


def test_full_width_barrier_fails_closed_when_no_bounded_route_exists():
    scene = _scene(_box("barrier", 4, 6, 0, 10, 0, 20))

    with pytest.raises(NoRouteError, match="No collision-free bounded route"):
        plan_motion(scene, Point3D(1, 5, 10), Point3D(9, 5, 10))


def test_motion_plan_serialization_is_stable_and_includes_tool_state_change():
    state = ToolState(instrument="pipette", attached_tip_extension_mm=0)
    change = ToolStateChange(
        position=Point3D(2, 3, 4),
        instrument="pipette",
        attached_tip_extension_mm=70,
    )
    segment = MotionSegment(
        kind="axis_aligned",
        start=Point3D(1, 3, 4),
        end=Point3D(2, 3, 4),
        phase="tip-engagement",
        tool_state=state,
        access=AccessScope(
            allowed_fixture_names=("tips",),
            allowed_corridor_names=("a1-exit",),
            allowed_tool_names=("pipette",),
        ),
    )

    tips = _box("tips", 8, 9, 8, 9, 8, 9)
    corridor = Corridor(
        name="a1-exit",
        box=_box("a1-exit-box", 7, 10, 7, 10, 7, 10),
        fixture_names=("tips",),
    )
    plan = plan_motion(
        _scene(tips, corridors=(corridor,)),
        segment.start,
        segment.end,
        tool_state=state,
        access=segment.access,
        command="pick_up_tip",
        operation="engage",
        phase="tip-engagement",
        state_changes=(change,),
    )

    assert plan.to_dict() == {
        "version": 1,
        "command": "pick_up_tip",
        "operation": "engage",
        "start": {"x": 1.0, "y": 3.0, "z": 4.0},
        "end": {"x": 2.0, "y": 3.0, "z": 4.0},
        "strategy": "x_first",
        "segments": [{
            "kind": "axis_aligned",
            "start": {"x": 1.0, "y": 3.0, "z": 4.0},
            "end": {"x": 2.0, "y": 3.0, "z": 4.0},
            "axis": "x",
            "phase": "tip-engagement",
            "tool_state": {
                "instrument": "pipette",
                "attached_tip_extension_mm": 0.0,
            },
            "access": {
                "allowed_fixture_names": ["tips"],
                "allowed_corridor_names": ["a1-exit"],
                "allowed_tool_names": ["pipette"],
            },
        }],
        "state_changes": [{
            "position": {"x": 2.0, "y": 3.0, "z": 4.0},
            "instrument": "pipette",
            "attached_tip_extension_mm": 70.0,
        }],
    }


def test_attached_tip_radius_and_extension_participate_in_collision_checking():
    fixture = _box("low-fixture", 0, 10, 0, 10, 4, 6)
    scene = _scene(
        fixture,
        tools=(_tool(z_min=3, z_max=5, radius=2.5),),
        clearance_mm=1.0,
    )
    start = Point3D(3, 3, 10)
    end = Point3D(7, 3, 10)

    assert plan_motion(scene, start, end).segments
    with pytest.raises(NoRouteError):
        plan_motion(
            scene,
            start,
            end,
            tool_state=ToolState(
                instrument="pipette",
                attached_tip_extension_mm=5,
            ),
        )


def test_every_mounted_tool_envelope_is_checked_from_the_same_carriage_path():
    scene = _scene(
        _box("camera-obstacle", 4, 6, 7, 9, 4, 6),
        tools=(
            _tool("pipette"),
            ToolEnvelope(
                name="camera",
                box=_box("camera-body", -0.25, 0.25, 5.75, 6.25, -0.25, 0.25),
            ),
        ),
    )

    plan = plan_motion(scene, Point3D(1, 1, 5), Point3D(9, 1, 5))

    assert plan.strategy.startswith("detour:")
    assert any(segment.axis == "y" for segment in plan.segments)


def test_fixture_access_requires_matching_fixture_and_corridor_names():
    fixture = _box("tips", 8, 10, 4, 6, 4, 6)
    corridor = Corridor(
        name="a1-exit",
        box=_box("a1-exit-box", 7, 10, 3, 7, 3, 7),
        fixture_names=("tips",),
    )
    scene = _scene(fixture, corridors=(corridor,))
    start = Point3D(6, 5, 5)
    end = Point3D(9, 5, 5)

    for scope in (
        AccessScope(allowed_fixture_names=("tips",)),
        AccessScope(allowed_corridor_names=("a1-exit",)),
        AccessScope(
            allowed_fixture_names=("tips",),
            allowed_corridor_names=("a1-exit",),
        ),
    ):
        with pytest.raises(NoRouteError):
            plan_motion(scene, start, end, access=scope)

    with pytest.raises(InvalidGeometryError, match="unknown fixtures"):
        plan_motion(
            scene,
            start,
            end,
            access=AccessScope(
                allowed_fixture_names=("other",),
                allowed_corridor_names=("a1-exit",),
            ),
        )

    plan = plan_motion(
        scene,
        start,
        end,
        access=AccessScope(
            allowed_fixture_names=("tips",),
            allowed_corridor_names=("a1-exit",),
            allowed_tool_names=("pipette",),
        ),
        operation="side-exit",
    )

    assert plan.segments[-1].end == end
    assert plan.segments[-1].access.allowed_fixture_names == ("tips",)


def test_access_scope_never_ignores_a_different_mounted_tool():
    fixture = _box("tips", 8, 10, 4, 6, 4, 6)
    corridor = Corridor(
        name="a1-exit",
        box=_box("a1-exit-box", 7, 10, 3, 7, 3, 7),
        fixture_names=("tips",),
    )
    scene = _scene(
        fixture,
        tools=(
            _tool("pipette"),
            ToolEnvelope(
                "camera",
                _box("camera-body", -0.25, 0.25, -0.25, 0.25, -0.25, 0.25),
            ),
        ),
        corridors=(corridor,),
    )

    with pytest.raises(NoRouteError):
        plan_motion(
            scene,
            Point3D(6, 5, 5),
            Point3D(9, 5, 5),
            access=AccessScope(
                allowed_fixture_names=("tips",),
                allowed_corridor_names=("a1-exit",),
                allowed_tool_names=("pipette",),
            ),
        )


def test_base_tool_access_authorizes_its_dynamic_attached_tip_in_same_corridor():
    fixture = _box("tips", 8, 10, 4, 6, 4, 6)
    corridor = Corridor(
        name="a1-exit",
        box=_box("a1-exit-box", 7, 10, 3, 7, 3, 11),
        fixture_names=("tips",),
    )
    scene = _scene(
        fixture,
        tools=(_tool(z_min=3, z_max=5, radius=2.5),),
        corridors=(corridor,),
    )

    plan = plan_motion(
        scene,
        Point3D(6, 5, 10),
        Point3D(9, 5, 10),
        tool_state=ToolState("pipette", attached_tip_extension_mm=5),
        access=AccessScope(
            allowed_fixture_names=("tips",),
            allowed_corridor_names=("a1-exit",),
            allowed_tool_names=("pipette",),
        ),
    )

    assert plan.end == Point3D(9, 5, 10)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_geometry_fails_closed(value: float):
    with pytest.raises(InvalidGeometryError, match="finite"):
        Point3D(value, 0, 0)


def test_touching_fixture_boundary_counts_as_collision():
    scene = _scene(_box("fixture", 4, 6, 4, 6, 4, 6))

    with pytest.raises(NoRouteError):
        plan_motion(scene, Point3D(3.75, 5, 5), Point3D(3.75, 5, 5))


def test_bounds_constrain_carriage_points_not_relative_tool_solids_or_corridors():
    corridor = Corridor(
        name="outside-body",
        box=_box("outside-body-box", -5, 2, -5, 2, -5, 2),
        fixture_names=("outside-fixture",),
    )
    scene = _scene(
        _box("outside-fixture", -4, -3, -4, -3, -4, -3),
        tools=(ToolEnvelope("wide-tool", _box("wide-tool-box", -2, 2, -2, 2, -2, 2)),),
        corridors=(corridor,),
    )

    plan = plan_motion(scene, Point3D(0, 0, 1), Point3D(1, 0, 1))

    assert plan.start == Point3D(0, 0, 1)
    assert plan.end == Point3D(1, 0, 1)


def test_tool_envelope_serializes_explicit_tcp():
    additional = _box("pipette-motor", -15, 15, -12, 12, -20, 10)
    envelope = ToolEnvelope(
        "pipette",
        _box("pipette-body", -10, 10, -10, 10, -70, 0),
        Point3D(3, -4, -70),
        2.5,
        (additional,),
    )

    assert envelope.to_dict()["tcp"] == {"x": 3.0, "y": -4.0, "z": -70.0}
    assert envelope.to_dict()["attached_tip_radius_mm"] == 2.5
    assert envelope.to_dict()["additional_boxes"] == [additional.to_dict()]


def test_additional_tool_box_participates_in_collision_and_detour_selection():
    tool = ToolEnvelope(
        "pipette",
        _box("high-primary", -0.25, 0.25, -0.25, 0.25, 10, 12),
        additional_boxes=(
            _box("low-motor", -0.25, 0.25, -0.25, 0.25, -0.25, 0.25),
        ),
    )
    scene = _scene(
        _box("blocking-rack", 4, 6, 4, 6, 4, 6),
        tools=(tool,),
    )

    plan = plan_motion(scene, Point3D(1, 5, 5), Point3D(9, 5, 5))

    assert plan.strategy.startswith(
        "detour:blocking-rack:pipette:additional:0:"
    )
    assert any(segment.axis == "y" for segment in plan.segments)


def test_additional_boxes_share_base_tool_access_identity():
    fixture = _box("tips", 8, 10, 4, 6, 4, 6)
    corridor = Corridor(
        name="a1-exit",
        box=_box("a1-exit-box", 7, 10, 3, 7, 3, 7),
        fixture_names=("tips",),
    )
    tool = ToolEnvelope(
        "pipette",
        _box("high-primary", -0.25, 0.25, -0.25, 0.25, 10, 12),
        additional_boxes=(
            _box("low-motor", -0.25, 0.25, -0.25, 0.25, -0.25, 0.25),
        ),
    )
    scene = _scene(fixture, tools=(tool,), corridors=(corridor,))
    access = AccessScope(
        allowed_fixture_names=("tips",),
        allowed_corridor_names=("a1-exit",),
        allowed_tool_names=("pipette",),
    )

    plan = plan_motion(
        scene,
        Point3D(6, 5, 5),
        Point3D(9, 5, 5),
        access=access,
    )

    assert plan.end == Point3D(9, 5, 5)


def test_irrelevant_fixture_population_does_not_create_detour_candidates(monkeypatch):
    irrelevant = tuple(
        _box(f"irrelevant-{index:03d}", 20 + index, 21 + index, 20, 21, 4, 6)
        for index in range(100)
    )
    scene = _scene(
        _box("blocking-rack", 4, 6, 4, 6, 4, 6),
        *irrelevant,
        tools=(
            _tool("pipette"),
            _tool("camera", z_min=10, z_max=12),
        ),
    )
    checks = 0
    original = planner_module._path_check

    def counting_path_check(*args, **kwargs):
        nonlocal checks
        checks += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(planner_module, "_path_check", counting_path_check)

    plan = plan_motion(scene, Point3D(1, 5, 5), Point3D(9, 5, 5))

    assert plan.strategy.startswith("detour:blocking-rack:pipette:")
    assert checks <= 5


@pytest.mark.parametrize("value", [True, "1", None])
def test_point_rejects_non_numeric_coordinates(value):
    with pytest.raises(InvalidGeometryError, match="finite number"):
        Point3D(value, 0, 0)


@pytest.mark.parametrize(
    ("name", "minimum", "maximum", "message"),
    [
        ("", Point3D(0, 0, 0), Point3D(1, 1, 1), "non-empty"),
        ("box", (0, 0, 0), Point3D(1, 1, 1), "Point3D"),
        ("box", Point3D(0, 0, 0), Point3D(0, 1, 1), "minimum.x"),
        ("box", Point3D(0, 0, 0), Point3D(1, 0, 1), "minimum.y"),
        ("box", Point3D(0, 0, 0), Point3D(1, 1, 0), "minimum.z"),
    ],
)
def test_aabb_rejects_unusable_collision_geometry(name, minimum, maximum, message):
    with pytest.raises(InvalidGeometryError, match=message):
        AABB(name, minimum, maximum)


def test_aabb_closed_geometry_operations_preserve_axis_contracts():
    box = _box("box", 1, 3, 2, 5, 4, 8)
    overlap = _box("overlap", 2, 4, 3, 6, 5, 9)
    separate = _box("separate", 6, 7, 6, 7, 6, 7)

    assert (box.x_min, box.x_max, box.y_min, box.y_max, box.z_min, box.z_max) == (
        1,
        3,
        2,
        5,
        4,
        8,
    )
    assert box.intersection(overlap) == _box("intersection", 2, 3, 3, 5, 5, 8)
    assert box.intersection(separate) is None
    assert box.intersection(_box("touch", 3, 4, 2, 5, 4, 8)) is None
    assert box.contains_box(_box("inside", 1.5, 2.5, 2.5, 4.5, 4.5, 7.5))
    with pytest.raises(InvalidGeometryError, match="non-negative"):
        box.expanded(-0.1)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"name": ""}, "non-empty"),
        ({"box": object()}, "must be an AABB"),
        ({"tcp": object()}, "must be a Point3D"),
        ({"attached_tip_radius_mm": 0}, "greater than zero"),
        ({"attached_tip_radius_mm": math.nan}, "finite"),
        ({"additional_boxes": (object(),)}, "additional_boxes"),
    ],
)
def test_tool_envelope_rejects_incomplete_collision_metadata(kwargs, message):
    values = {
        "name": "pipette",
        "box": _box("body", -1, 1, -1, 1, -1, 1),
        "tcp": Point3D(0, 0, 0),
    }
    values.update(kwargs)
    with pytest.raises(InvalidGeometryError, match=message):
        ToolEnvelope(**values)


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: Corridor("", _box("corridor", 0, 1, 0, 1), ("rack",)),
            "non-empty",
        ),
        (lambda: Corridor("entry", object(), ("rack",)), "must be an AABB"),
        (lambda: Corridor("entry", _box("corridor", 0, 1, 0, 1), ()), "at least one"),
        (
            lambda: AccessScope(allowed_fixture_names="rack"),
            "not one string",
        ),
        (
            lambda: AccessScope(allowed_tool_names=("",)),
            "non-empty",
        ),
        (
            lambda: AccessScope(allowed_corridor_names=None),
            "iterable of names",
        ),
    ],
)
def test_access_models_reject_ambiguous_or_unscoped_names(factory, message):
    with pytest.raises(InvalidGeometryError, match=message):
        factory()


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: ToolState("pipette", -1), "non-negative"),
        (lambda: ToolState(None, 5), "requires an instrument"),
        (lambda: ToolState("", 0), "non-empty"),
        (lambda: ToolState("pipette", math.inf), "finite"),
        (lambda: ToolStateChange((0, 0, 0), "pipette", 0), "Point3D"),
        (lambda: ToolStateChange(Point3D(0, 0, 0), "", 0), "non-empty"),
        (lambda: ToolStateChange(Point3D(0, 0, 0), "pipette", -1), "non-negative"),
    ],
)
def test_tool_state_models_reject_impossible_state(factory, message):
    with pytest.raises(InvalidGeometryError, match=message):
        factory()


def _valid_segment(
    start: Point3D = Point3D(0, 0, 0),
    end: Point3D = Point3D(1, 0, 0),
    *,
    state: ToolState | None = None,
) -> MotionSegment:
    return MotionSegment(
        "axis_aligned",
        start,
        end,
        "transit",
        tool_state=state or ToolState(),
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"kind": ""}, "segment kind"),
        ({"phase": ""}, "segment phase"),
        ({"start": (0, 0, 0)}, "endpoints"),
        ({"tool_state": object()}, "tool_state"),
        ({"access": object()}, "access"),
        ({"end": Point3D(0, 0, 0)}, "one axis or coordinated XY"),
        ({"end": Point3D(1, 0, 1)}, "one axis or coordinated XY"),
    ],
)
def test_motion_segment_rejects_non_executable_moves(kwargs, message):
    values = {
        "kind": "axis_aligned",
        "start": Point3D(0, 0, 0),
        "end": Point3D(1, 0, 0),
        "phase": "transit",
        "tool_state": ToolState(),
        "access": AccessScope(),
    }
    values.update(kwargs)
    with pytest.raises(InvalidGeometryError, match=message):
        MotionSegment(**values)


def test_motion_segment_accepts_coordinated_xy_but_rejects_xy_with_z():
    segment = MotionSegment(
        kind="coordinated_xy",
        start=Point3D(0, 0, 5),
        end=Point3D(3, 4, 5),
        phase="transit",
    )

    assert segment.axis == "xy"
    assert segment.length_mm == 5.0
    with pytest.raises(InvalidGeometryError, match="one axis or coordinated XY"):
        MotionSegment(
            kind="coordinated_xy",
            start=Point3D(0, 0, 5),
            end=Point3D(3, 4, 6),
            phase="transit",
        )


def _plan(**overrides) -> MotionPlan:
    values = {
        "command": "move",
        "operation": "transit",
        "start": Point3D(0, 0, 0),
        "end": Point3D(1, 0, 0),
        "strategy": "x_first",
        "segments": (_valid_segment(),),
    }
    values.update(overrides)
    return MotionPlan(**values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"version": 2}, "version 1"),
        ({"start": (0, 0, 0)}, "endpoints"),
        ({"segments": (object(),)}, "MotionSegment"),
        ({"state_changes": (object(),)}, "ToolStateChange"),
        ({"end": Point3D(2, 0, 0)}, "exact start and end"),
        ({"segments": ()}, "requires at least one segment"),
    ],
)
def test_motion_plan_rejects_invalid_topology(overrides, message):
    with pytest.raises(InvalidGeometryError, match=message):
        _plan(**overrides)


def test_motion_plan_rejects_discontinuity_and_off_route_state_change():
    first = _valid_segment(end=Point3D(1, 0, 0))
    disconnected = _valid_segment(Point3D(2, 0, 0), Point3D(3, 0, 0))
    with pytest.raises(InvalidGeometryError, match="continuous path"):
        _plan(end=Point3D(3, 0, 0), segments=(first, disconnected))

    with pytest.raises(InvalidGeometryError, match="exact segment boundary"):
        _plan(
            state_changes=(
                ToolStateChange(Point3D(0.5, 0, 0), "pipette", 5),
            ),
        )


def test_motion_plan_requires_exact_state_change_at_segment_transition():
    bare = ToolState("pipette", 0)
    tipped = ToolState("pipette", 5)
    first = _valid_segment(end=Point3D(1, 0, 0), state=bare)
    second = _valid_segment(Point3D(1, 0, 0), Point3D(2, 0, 0), state=tipped)

    with pytest.raises(InvalidGeometryError, match="requires exactly one"):
        _plan(end=Point3D(2, 0, 0), segments=(first, second))
    with pytest.raises(InvalidGeometryError, match="cannot appear"):
        _plan(
            end=Point3D(2, 0, 0),
            segments=(first, _valid_segment(Point3D(1, 0, 0), Point3D(2, 0, 0), state=bare)),
            state_changes=(ToolStateChange(Point3D(1, 0, 0), "pipette", 5),),
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"bounds": object()}, "bounds"),
        ({"clearance_mm": -1}, "non-negative"),
        ({"tool_envelopes": ()}, "At least one"),
        ({"fixtures": (object(),)}, "fixtures"),
        ({"corridors": (object(),)}, "corridors"),
        ({"tool_envelopes": (object(),)}, "tool_envelopes"),
    ],
)
def test_build_scene_rejects_missing_or_invalid_geometry(kwargs, message):
    values = {
        "bounds": _box("bounds", 0, 10, 0, 10, 0, 10),
        "fixtures": (),
        "tool_envelopes": (_tool(),),
        "corridors": (),
    }
    values.update(kwargs)
    with pytest.raises(InvalidGeometryError, match=message):
        build_scene(**values)


def test_build_scene_rejects_duplicate_and_unresolved_names():
    fixture = _box("rack", 1, 2, 1, 2)
    corridor = Corridor("entry", _box("entry-box", 0, 3, 0, 3), ("missing",))
    with pytest.raises(InvalidGeometryError, match="Duplicate fixture"):
        build_scene(
            bounds=_box("bounds", 0, 10, 0, 10),
            fixtures=(fixture, fixture),
            tool_envelopes=(_tool(),),
        )
    with pytest.raises(InvalidGeometryError, match="unknown fixtures"):
        build_scene(
            bounds=_box("bounds", 0, 10, 0, 10),
            fixtures=(fixture,),
            tool_envelopes=(_tool(),),
            corridors=(corridor,),
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"scene": object()}, "scene"),
        ({"start": (0, 0, 0)}, "start and end"),
        ({"tool_state": "bad"}, "tool_state"),
        ({"access": "bad"}, "access"),
        ({"state_changes": (object(),)}, "state_changes"),
    ],
)
def test_plan_motion_rejects_invalid_domain_values(kwargs, message):
    values = {
        "scene": _scene(),
        "start": Point3D(1, 1, 1),
        "end": Point3D(2, 1, 1),
    }
    values.update(kwargs)
    with pytest.raises(InvalidGeometryError, match=message):
        plan_motion(**values)


def test_plan_motion_rejects_unknown_access_and_incomplete_tip_envelopes():
    scene = _scene()
    start = Point3D(1, 1, 5)
    end = Point3D(2, 1, 5)
    for scope, message in (
        (AccessScope(allowed_corridor_names=("missing",)), "unknown corridors"),
        (AccessScope(allowed_tool_names=("missing",)), "unknown tools"),
    ):
        with pytest.raises(InvalidGeometryError, match=message):
            plan_motion(scene, start, end, access=scope)

    with pytest.raises(InvalidGeometryError, match="no mounted tool envelope"):
        plan_motion(
            scene,
            start,
            end,
            tool_state=ToolState("camera", 5),
        )
    with pytest.raises(InvalidGeometryError, match="no attached_tip_radius_mm"):
        plan_motion(
            scene,
            start,
            end,
            tool_state=ToolState("pipette", 5),
        )
