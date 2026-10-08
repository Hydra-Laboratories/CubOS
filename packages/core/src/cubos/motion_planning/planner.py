"""Bounded, deterministic motion planning."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence

from .models import (
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
)


_EDGE_EPSILON_MM = 1e-6


@dataclass(frozen=True)
class Scene:
    """Resolved geometry consumed by the pure planner."""

    bounds: AABB
    fixtures: tuple[AABB, ...]
    tool_envelopes: tuple[ToolEnvelope, ...]
    corridors: tuple[Corridor, ...]
    clearance_mm: float


def build_scene(
    *,
    fixtures: Iterable[AABB],
    tool_envelopes: Iterable[ToolEnvelope | AABB],
    bounds: AABB,
    clearance_mm: float = 0.0,
    corridors: Iterable[Corridor] = (),
) -> Scene:
    """Build a validated scene from already-resolved deck-frame geometry."""
    if not isinstance(bounds, AABB):
        raise InvalidGeometryError("bounds must be an AABB.")
    clearance = _finite_non_negative(clearance_mm, "clearance_mm")
    fixture_values = tuple(fixtures)
    corridor_values = tuple(corridors)
    tool_values = tuple(
        value if isinstance(value, ToolEnvelope) else ToolEnvelope(value.name, value)
        if isinstance(value, AABB)
        else _invalid_tool_envelope(value)
        for value in tool_envelopes
    )
    if not tool_values:
        raise InvalidGeometryError("At least one mounted tool envelope is required.")
    if not all(isinstance(value, AABB) for value in fixture_values):
        raise InvalidGeometryError("fixtures must contain only AABB values.")
    if not all(isinstance(value, Corridor) for value in corridor_values):
        raise InvalidGeometryError("corridors must contain only Corridor values.")
    _require_unique((fixture.name for fixture in fixture_values), "fixture")
    _require_unique((tool.name for tool in tool_values), "tool envelope")
    _require_unique((corridor.name for corridor in corridor_values), "corridor")
    fixture_names = {fixture.name for fixture in fixture_values}
    for corridor in corridor_values:
        unknown = sorted(set(corridor.fixture_names) - fixture_names)
        if unknown:
            raise InvalidGeometryError(
                f"Corridor {corridor.name!r} names unknown fixtures {unknown}."
            )
    return Scene(
        bounds=bounds,
        fixtures=tuple(sorted(fixture_values, key=_box_sort_key)),
        tool_envelopes=tuple(sorted(tool_values, key=lambda value: value.name)),
        corridors=tuple(sorted(corridor_values, key=lambda value: value.name)),
        clearance_mm=clearance,
    )


def plan_motion(
    scene: Scene,
    start: Point3D,
    end: Point3D,
    *,
    tool_state: ToolState | None = None,
    access: AccessScope | None = None,
    command: str = "move",
    operation: str = "transit",
    phase: str = "transit",
    state_changes: Iterable[ToolStateChange] = (),
) -> MotionPlan:
    """Plan one bounded move or raise ``NoRouteError``.

    The search is intentionally finite: a coordinated XY line when its
    conservative swept volume is clear, X-first, Y-first, then routes around
    each forbidden carriage-box edge. Equal candidates retain that order.
    """
    if not isinstance(scene, Scene):
        raise InvalidGeometryError("scene must be built with build_scene().")
    if not isinstance(start, Point3D) or not isinstance(end, Point3D):
        raise InvalidGeometryError("start and end must be Point3D values.")
    state = tool_state or ToolState()
    scope = access or AccessScope()
    if not isinstance(state, ToolState):
        raise InvalidGeometryError("tool_state must be a ToolState.")
    if not isinstance(scope, AccessScope):
        raise InvalidGeometryError("access must be an AccessScope.")
    _validate_access_scope(scene, scope)
    changes = tuple(state_changes)
    if not all(isinstance(change, ToolStateChange) for change in changes):
        raise InvalidGeometryError("state_changes must contain ToolStateChange values.")
    relative_boxes = _effective_tool_boxes(scene, state)
    expanded_fixtures = tuple(
        (fixture.name, fixture.expanded(scene.clearance_mm))
        for fixture in scene.fixtures
    )

    if start == end:
        if not _point_is_clear(
            scene, start, relative_boxes, scope, expanded_fixtures,
        ):
            raise NoRouteError("The stationary carriage pose is in collision.")
        return MotionPlan(
            command=command,
            operation=operation,
            start=start,
            end=end,
            strategy="stationary",
            segments=(),
            state_changes=changes,
        )

    direct_candidates: list[tuple[int, str, tuple[Point3D, ...]]] = []
    if (
        start.z == end.z
        and start.x != end.x
        and start.y != end.y
        and not scope.allowed_fixture_names
        and not scope.allowed_corridor_names
    ):
        direct_candidates.append((0, "coordinated_xy", (start, end)))
    direct_candidates.extend([
        (1, "x_first", _direct_points(start, end, x_first=True)),
        (2, "y_first", _direct_points(start, end, x_first=False)),
    ])
    seen_paths: set[tuple[Point3D, ...]] = set()
    direct_blockers: set[tuple[str, str]] = set()
    for candidate_rank, strategy, points in direct_candidates:
        if points in seen_paths:
            continue
        seen_paths.add(points)
        clear, blockers = _path_check(
            scene, points, relative_boxes, scope, expanded_fixtures,
        )
        if clear:
            return _motion_plan(
                command=command,
                operation=operation,
                start=start,
                end=end,
                strategy=strategy,
                points=points,
                phase=phase,
                state=state,
                scope=scope,
                changes=changes,
            )
        direct_blockers.update(blockers)

    if scope.allowed_fixture_names or scope.allowed_corridor_names:
        raise NoRouteError(
            "No collision-free direct axis order exists for the scoped access move."
        )

    blockers = _forbidden_carriage_boxes(
        relative_boxes, expanded_fixtures, direct_blockers,
    )
    candidates: list[tuple[int, str, tuple[Point3D, ...]]] = []
    rank = 3
    for fixture_name, tool_name, forbidden in blockers:
        for edge, coordinate in (
            ("x_min", forbidden.minimum.x - _EDGE_EPSILON_MM),
            ("x_max", forbidden.maximum.x + _EDGE_EPSILON_MM),
            ("y_min", forbidden.minimum.y - _EDGE_EPSILON_MM),
            ("y_max", forbidden.maximum.y + _EDGE_EPSILON_MM),
        ):
            points = _detour_points(start, end, edge=edge, coordinate=coordinate)
            if points in seen_paths:
                continue
            seen_paths.add(points)
            candidates.append(
                (rank, f"detour:{fixture_name}:{tool_name}:{edge}", points)
            )
            rank += 1

    valid: list[tuple[float, int, str, tuple[Point3D, ...]]] = []
    for candidate_rank, strategy, points in candidates:
        if _path_is_clear(
            scene, points, relative_boxes, scope, expanded_fixtures,
        ):
            valid.append((_path_length(points), candidate_rank, strategy, points))
    if not valid:
        raise NoRouteError(
            f"No collision-free bounded route from {start.to_dict()} to {end.to_dict()}."
        )
    _, _, strategy, points = min(valid, key=lambda value: (value[0], value[1], value[2]))
    return _motion_plan(
        command=command,
        operation=operation,
        start=start,
        end=end,
        strategy=strategy,
        points=points,
        phase=phase,
        state=state,
        scope=scope,
        changes=changes,
    )


def _motion_plan(
    *,
    command: str,
    operation: str,
    start: Point3D,
    end: Point3D,
    strategy: str,
    points: Sequence[Point3D],
    phase: str,
    state: ToolState,
    scope: AccessScope,
    changes: tuple[ToolStateChange, ...],
) -> MotionPlan:
    segments = tuple(
        MotionSegment(
            kind=(
                "coordinated_xy"
                if segment_start.x != segment_end.x
                and segment_start.y != segment_end.y
                else "axis_aligned"
            ),
            start=segment_start,
            end=segment_end,
            phase=phase,
            tool_state=state,
            access=scope,
        )
        for segment_start, segment_end in zip(points, points[1:])
    )
    return MotionPlan(
        command=command,
        operation=operation,
        start=start,
        end=end,
        strategy=strategy,
        segments=segments,
        state_changes=changes,
    )


def _finite_non_negative(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidGeometryError(f"{label} must be a finite non-negative number.")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise InvalidGeometryError(f"{label} must be a finite non-negative number.")
    return value


def _invalid_tool_envelope(value: object) -> ToolEnvelope:
    raise InvalidGeometryError(
        f"tool_envelopes must contain ToolEnvelope or AABB values, got {value!r}."
    )


def _require_unique(names: Iterable[str], label: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in names:
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    if duplicates:
        raise InvalidGeometryError(f"Duplicate {label} names: {sorted(duplicates)}.")


def _validate_access_scope(scene: Scene, access: AccessScope) -> None:
    fixture_names = {fixture.name for fixture in scene.fixtures}
    corridor_names = {corridor.name for corridor in scene.corridors}
    tool_names = {tool.name for tool in scene.tool_envelopes}
    unknown_fixtures = sorted(set(access.allowed_fixture_names) - fixture_names)
    unknown_corridors = sorted(set(access.allowed_corridor_names) - corridor_names)
    unknown_tools = sorted(set(access.allowed_tool_names) - tool_names)
    if unknown_fixtures:
        raise InvalidGeometryError(
            f"Access scope names unknown fixtures {unknown_fixtures}."
        )
    if unknown_corridors:
        raise InvalidGeometryError(
            f"Access scope names unknown corridors {unknown_corridors}."
        )
    if unknown_tools:
        raise InvalidGeometryError(
            f"Access scope names unknown tools {unknown_tools}."
        )


def _box_sort_key(box: AABB) -> tuple[object, ...]:
    return (
        box.name,
        box.minimum.x,
        box.minimum.y,
        box.minimum.z,
        box.maximum.x,
        box.maximum.y,
        box.maximum.z,
    )


def _effective_tool_boxes(
    scene: Scene,
    state: ToolState,
) -> tuple[tuple[str, str, AABB], ...]:
    boxes: list[tuple[str, str, AABB]] = []
    for tool in scene.tool_envelopes:
        boxes.append((tool.name, tool.name, tool.box))
        boxes.extend(
            (f"{tool.name}:additional:{index}", tool.name, box)
            for index, box in enumerate(tool.additional_boxes)
        )
    if state.attached_tip_extension_mm <= 0:
        return tuple(boxes)
    matching = [tool for tool in scene.tool_envelopes if tool.name == state.instrument]
    if not matching:
        raise InvalidGeometryError(
            f"Attached-tip instrument {state.instrument!r} has no mounted tool envelope."
        )
    tool = matching[0]
    if tool.attached_tip_radius_mm is None:
        raise InvalidGeometryError(
            f"Attached-tip instrument {tool.name!r} has no attached_tip_radius_mm."
        )
    radius = tool.attached_tip_radius_mm
    tip = AABB(
        name=f"{tool.name}:attached_tip",
        minimum=Point3D(
            tool.tcp.x - radius,
            tool.tcp.y - radius,
            tool.tcp.z - state.attached_tip_extension_mm,
        ),
        maximum=Point3D(
            tool.tcp.x + radius,
            tool.tcp.y + radius,
            tool.tcp.z,
        ),
    )
    boxes.append((tip.name, tool.name, tip))
    return tuple(boxes)


def _direct_points(start: Point3D, end: Point3D, *, x_first: bool) -> tuple[Point3D, ...]:
    travel_z = max(start.z, end.z)
    points = [start, Point3D(start.x, start.y, travel_z)]
    if x_first:
        points.extend((Point3D(end.x, start.y, travel_z), Point3D(end.x, end.y, travel_z)))
    else:
        points.extend((Point3D(start.x, end.y, travel_z), Point3D(end.x, end.y, travel_z)))
    points.append(end)
    return _deduplicate(points)


def _detour_points(
    start: Point3D,
    end: Point3D,
    *,
    edge: str,
    coordinate: float,
) -> tuple[Point3D, ...]:
    travel_z = max(start.z, end.z)
    points = [start, Point3D(start.x, start.y, travel_z)]
    if edge.startswith("x_"):
        points.extend((
            Point3D(coordinate, start.y, travel_z),
            Point3D(coordinate, end.y, travel_z),
            Point3D(end.x, end.y, travel_z),
        ))
    else:
        points.extend((
            Point3D(start.x, coordinate, travel_z),
            Point3D(end.x, coordinate, travel_z),
            Point3D(end.x, end.y, travel_z),
        ))
    points.append(end)
    return _deduplicate(points)


def _deduplicate(points: Sequence[Point3D]) -> tuple[Point3D, ...]:
    result: list[Point3D] = []
    for point in points:
        if not result or result[-1] != point:
            result.append(point)
    return tuple(result)


def _path_length(points: Sequence[Point3D]) -> float:
    return sum(
        abs(end.x - start.x) + abs(end.y - start.y) + abs(end.z - start.z)
        for start, end in zip(points, points[1:])
    )


def _path_is_clear(
    scene: Scene,
    points: Sequence[Point3D],
    relative_boxes: tuple[tuple[str, str, AABB], ...],
    access: AccessScope,
    expanded_fixtures: tuple[tuple[str, AABB], ...],
) -> bool:
    clear, _ = _path_check(
        scene, points, relative_boxes, access, expanded_fixtures,
    )
    return clear


def _path_check(
    scene: Scene,
    points: Sequence[Point3D],
    relative_boxes: tuple[tuple[str, str, AABB], ...],
    access: AccessScope,
    expanded_fixtures: tuple[tuple[str, AABB], ...],
) -> tuple[bool, set[tuple[str, str]]]:
    if not points or not scene.bounds.contains_point(points[0]):
        return False, set()
    blockers = _point_blockers(
        scene, points[0], relative_boxes, access, expanded_fixtures,
    )
    for start, end in zip(points, points[1:]):
        in_bounds, segment_blockers = _segment_check(
            scene, start, end, relative_boxes, access, expanded_fixtures,
        )
        if not in_bounds:
            return False, blockers
        blockers.update(segment_blockers)
    return not blockers, blockers


def _point_is_clear(
    scene: Scene,
    point: Point3D,
    relative_boxes: tuple[tuple[str, str, AABB], ...],
    access: AccessScope,
    expanded_fixtures: tuple[tuple[str, AABB], ...],
) -> bool:
    return (
        scene.bounds.contains_point(point)
        and not _point_blockers(
            scene, point, relative_boxes, access, expanded_fixtures,
        )
    )


def _point_blockers(
    scene: Scene,
    point: Point3D,
    relative_boxes: tuple[tuple[str, str, AABB], ...],
    access: AccessScope,
    expanded_fixtures: tuple[tuple[str, AABB], ...],
) -> set[tuple[str, str]]:
    blockers: set[tuple[str, str]] = set()
    for geometry_name, tool_name, box in relative_boxes:
        blockers.update(
            _physical_box_blockers(
                scene,
                box.translated(point),
                access,
                geometry_name,
                tool_name,
                expanded_fixtures,
            )
        )
    return blockers


def _segment_check(
    scene: Scene,
    start: Point3D,
    end: Point3D,
    relative_boxes: tuple[tuple[str, str, AABB], ...],
    access: AccessScope,
    expanded_fixtures: tuple[tuple[str, AABB], ...],
) -> tuple[bool, set[tuple[str, str]]]:
    changed_axes = tuple(
        axis for axis in ("x", "y", "z")
        if getattr(start, axis) != getattr(end, axis)
    )
    if len(changed_axes) != 1 and changed_axes != ("x", "y"):
        return False, set()
    if not scene.bounds.contains_point(start) or not scene.bounds.contains_point(end):
        return False, set()
    blockers: set[tuple[str, str]] = set()
    for geometry_name, tool_name, relative in relative_boxes:
        swept = _swept_box(relative, start, end)
        blockers.update(
            _physical_box_blockers(
                scene,
                swept,
                access,
                geometry_name,
                tool_name,
                expanded_fixtures,
            )
        )
    return True, blockers


def _swept_box(relative: AABB, start: Point3D, end: Point3D) -> AABB:
    return AABB(
        name=f"{relative.name}:swept",
        minimum=Point3D(
            min(start.x, end.x) + relative.minimum.x,
            min(start.y, end.y) + relative.minimum.y,
            min(start.z, end.z) + relative.minimum.z,
        ),
        maximum=Point3D(
            max(start.x, end.x) + relative.maximum.x,
            max(start.y, end.y) + relative.maximum.y,
            max(start.z, end.z) + relative.maximum.z,
        ),
    )


def _physical_box_blockers(
    scene: Scene,
    physical: AABB,
    access: AccessScope,
    geometry_name: str,
    tool_name: str,
    expanded_fixtures: tuple[tuple[str, AABB], ...],
) -> set[tuple[str, str]]:
    blockers: set[tuple[str, str]] = set()
    for fixture_name, expanded in expanded_fixtures:
        if not physical.intersects(expanded):
            continue
        if not _overlap_is_allowed(
            scene, physical, expanded, fixture_name, tool_name, access,
        ):
            blockers.add((fixture_name, geometry_name))
    return blockers


def _overlap_is_allowed(
    scene: Scene,
    physical: AABB,
    expanded_fixture: AABB,
    fixture_name: str,
    tool_name: str,
    access: AccessScope,
) -> bool:
    if fixture_name not in access.allowed_fixture_names:
        return False
    if tool_name not in access.allowed_tool_names:
        return False
    for corridor in scene.corridors:
        if corridor.name not in access.allowed_corridor_names:
            continue
        if fixture_name not in corridor.fixture_names:
            continue
        if _closed_overlap_is_contained(physical, expanded_fixture, corridor.box):
            return True
    return False


def _closed_overlap_is_contained(first: AABB, second: AABB, container: AABB) -> bool:
    lower = Point3D(
        max(first.minimum.x, second.minimum.x),
        max(first.minimum.y, second.minimum.y),
        max(first.minimum.z, second.minimum.z),
    )
    upper = Point3D(
        min(first.maximum.x, second.maximum.x),
        min(first.maximum.y, second.maximum.y),
        min(first.maximum.z, second.maximum.z),
    )
    return (
        container.minimum.x <= lower.x <= upper.x <= container.maximum.x
        and container.minimum.y <= lower.y <= upper.y <= container.maximum.y
        and container.minimum.z <= lower.z <= upper.z <= container.maximum.z
    )


def _forbidden_carriage_boxes(
    relative_boxes: tuple[tuple[str, str, AABB], ...],
    expanded_fixtures: tuple[tuple[str, AABB], ...],
    blocking_pairs: set[tuple[str, str]],
) -> tuple[tuple[str, str, AABB], ...]:
    forbidden: list[tuple[str, str, AABB]] = []
    fixtures_by_name = dict(expanded_fixtures)
    tools_by_name = {
        geometry_name: box for geometry_name, _, box in relative_boxes
    }
    for fixture_name, geometry_name in sorted(blocking_pairs):
        expanded = fixtures_by_name[fixture_name]
        relative = tools_by_name[geometry_name]
        forbidden.append((
                fixture_name,
                geometry_name,
                AABB(
                    name=f"{fixture_name}:{geometry_name}:forbidden_carriage",
                    minimum=Point3D(
                        expanded.minimum.x - relative.maximum.x,
                        expanded.minimum.y - relative.maximum.y,
                        expanded.minimum.z - relative.maximum.z,
                    ),
                    maximum=Point3D(
                        expanded.maximum.x - relative.minimum.x,
                        expanded.maximum.y - relative.minimum.y,
                        expanded.maximum.z - relative.minimum.z,
                    ),
                ),
            ))
    return tuple(forbidden)
