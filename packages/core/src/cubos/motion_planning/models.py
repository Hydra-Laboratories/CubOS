"""Immutable geometry and motion-plan value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable


class MotionPlanningError(ValueError):
    """Base error for planner inputs and route failures."""


class InvalidGeometryError(MotionPlanningError):
    """Raised when collision-critical geometry is missing or invalid."""


class NoRouteError(MotionPlanningError):
    """Raised when the bounded planner cannot find a collision-free route."""


def _finite(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidGeometryError(f"{label} must be a finite number, got {value!r}.")
    value = float(value)
    if not math.isfinite(value):
        raise InvalidGeometryError(f"{label} must be finite, got {value!r}.")
    return value


def _name(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidGeometryError(f"{label} must be a non-empty string.")
    return value


def _names(values: Iterable[str], label: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise InvalidGeometryError(f"{label} must be an iterable of names, not one string.")
    try:
        normalized = tuple(sorted({_name(value, label) for value in values}))
    except TypeError as exc:
        raise InvalidGeometryError(f"{label} must be an iterable of names.") from exc
    return normalized


@dataclass(frozen=True)
class Point3D:
    """One finite carriage or geometry point in deck-frame millimeters."""

    x: float
    y: float
    z: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _finite(self.x, "x"))
        object.__setattr__(self, "y", _finite(self.y, "y"))
        object.__setattr__(self, "z", _finite(self.z, "z"))

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "z": self.z}


@dataclass(frozen=True)
class AABB:
    """A finite, non-degenerate axis-aligned bounding box."""

    name: str
    minimum: Point3D
    maximum: Point3D

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name, "AABB name"))
        if not isinstance(self.minimum, Point3D) or not isinstance(self.maximum, Point3D):
            raise InvalidGeometryError("AABB minimum and maximum must be Point3D values.")
        for axis in ("x", "y", "z"):
            lower = getattr(self.minimum, axis)
            upper = getattr(self.maximum, axis)
            if lower >= upper:
                raise InvalidGeometryError(
                    f"AABB {self.name!r} requires minimum.{axis} < maximum.{axis}; "
                    f"got {lower} >= {upper}."
                )

    @property
    def x_min(self) -> float:
        return self.minimum.x

    @property
    def x_max(self) -> float:
        return self.maximum.x

    @property
    def y_min(self) -> float:
        return self.minimum.y

    @property
    def y_max(self) -> float:
        return self.maximum.y

    @property
    def z_min(self) -> float:
        return self.minimum.z

    @property
    def z_max(self) -> float:
        return self.maximum.z

    def translated(self, offset: Point3D, *, name: str | None = None) -> "AABB":
        return AABB(
            name=name or self.name,
            minimum=Point3D(
                self.minimum.x + offset.x,
                self.minimum.y + offset.y,
                self.minimum.z + offset.z,
            ),
            maximum=Point3D(
                self.maximum.x + offset.x,
                self.maximum.y + offset.y,
                self.maximum.z + offset.z,
            ),
        )

    def expanded(self, distance: float, *, name: str | None = None) -> "AABB":
        distance = _finite(distance, "AABB expansion")
        if distance < 0:
            raise InvalidGeometryError("AABB expansion must be non-negative.")
        return AABB(
            name=name or self.name,
            minimum=Point3D(
                self.minimum.x - distance,
                self.minimum.y - distance,
                self.minimum.z - distance,
            ),
            maximum=Point3D(
                self.maximum.x + distance,
                self.maximum.y + distance,
                self.maximum.z + distance,
            ),
        )

    def intersects(self, other: "AABB") -> bool:
        """Return whether two closed boxes touch or overlap."""
        return (
            self.minimum.x <= other.maximum.x
            and self.maximum.x >= other.minimum.x
            and self.minimum.y <= other.maximum.y
            and self.maximum.y >= other.minimum.y
            and self.minimum.z <= other.maximum.z
            and self.maximum.z >= other.minimum.z
        )

    def intersection(self, other: "AABB", *, name: str = "intersection") -> "AABB | None":
        """Return the positive-volume overlap, or ``None`` for no overlap.

        Touching remains a collision even though its intersection is a plane.
        Callers that need the degenerate contact coordinates should compare the
        closed bounds directly rather than constructing an ``AABB``.
        """
        if not self.intersects(other):
            return None
        minimum = Point3D(
            max(self.minimum.x, other.minimum.x),
            max(self.minimum.y, other.minimum.y),
            max(self.minimum.z, other.minimum.z),
        )
        maximum = Point3D(
            min(self.maximum.x, other.maximum.x),
            min(self.maximum.y, other.maximum.y),
            min(self.maximum.z, other.maximum.z),
        )
        if any(
            getattr(minimum, axis) >= getattr(maximum, axis)
            for axis in ("x", "y", "z")
        ):
            return None
        return AABB(name=name, minimum=minimum, maximum=maximum)

    def contains_point(self, point: Point3D) -> bool:
        return (
            self.minimum.x <= point.x <= self.maximum.x
            and self.minimum.y <= point.y <= self.maximum.y
            and self.minimum.z <= point.z <= self.maximum.z
        )

    def contains_box(self, other: "AABB") -> bool:
        return (
            self.minimum.x <= other.minimum.x
            and self.maximum.x >= other.maximum.x
            and self.minimum.y <= other.minimum.y
            and self.maximum.y >= other.maximum.y
            and self.minimum.z <= other.minimum.z
            and self.maximum.z >= other.maximum.z
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "minimum": self.minimum.to_dict(),
            "maximum": self.maximum.to_dict(),
        }


@dataclass(frozen=True)
class ToolEnvelope:
    """One mounted tool's collision envelope relative to the carriage."""

    name: str
    box: AABB
    tcp: Point3D = field(default_factory=lambda: Point3D(0, 0, 0))
    attached_tip_radius_mm: float | None = None
    additional_boxes: tuple[AABB, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name, "tool envelope name"))
        if not isinstance(self.box, AABB):
            raise InvalidGeometryError("ToolEnvelope.box must be an AABB.")
        if not isinstance(self.tcp, Point3D):
            raise InvalidGeometryError("ToolEnvelope.tcp must be a Point3D.")
        if self.attached_tip_radius_mm is not None:
            radius = _finite(self.attached_tip_radius_mm, "attached tip radius")
            if radius <= 0:
                raise InvalidGeometryError("attached tip radius must be greater than zero.")
            object.__setattr__(self, "attached_tip_radius_mm", radius)
        additional_boxes = tuple(self.additional_boxes)
        if not all(isinstance(box, AABB) for box in additional_boxes):
            raise InvalidGeometryError(
                "ToolEnvelope.additional_boxes must contain only AABB values."
            )
        object.__setattr__(self, "additional_boxes", additional_boxes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "box": self.box.to_dict(),
            "tcp": self.tcp.to_dict(),
            "attached_tip_radius_mm": self.attached_tip_radius_mm,
            "additional_boxes": [box.to_dict() for box in self.additional_boxes],
        }


@dataclass(frozen=True)
class Corridor:
    """A named physical access region associated with specific fixtures."""

    name: str
    box: AABB
    fixture_names: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name, "corridor name"))
        if not isinstance(self.box, AABB):
            raise InvalidGeometryError("Corridor.box must be an AABB.")
        normalized = _names(self.fixture_names, "corridor fixture name")
        if not normalized:
            raise InvalidGeometryError("A corridor must name at least one fixture.")
        object.__setattr__(self, "fixture_names", normalized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "box": self.box.to_dict(),
            "fixture_names": list(self.fixture_names),
        }


@dataclass(frozen=True)
class AccessScope:
    """Per-operation fixture/corridor authorization.

    Both names must match scene geometry before an overlap is permitted.
    """

    allowed_fixture_names: tuple[str, ...] = ()
    allowed_corridor_names: tuple[str, ...] = ()
    allowed_tool_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_fixture_names",
            _names(self.allowed_fixture_names, "allowed fixture name"),
        )
        object.__setattr__(
            self,
            "allowed_corridor_names",
            _names(self.allowed_corridor_names, "allowed corridor name"),
        )
        object.__setattr__(
            self,
            "allowed_tool_names",
            _names(self.allowed_tool_names, "allowed tool name"),
        )

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "allowed_fixture_names": list(self.allowed_fixture_names),
            "allowed_corridor_names": list(self.allowed_corridor_names),
            "allowed_tool_names": list(self.allowed_tool_names),
        }


@dataclass(frozen=True)
class ToolState:
    """Dynamic collision state for a mounted tool."""

    instrument: str | None = None
    attached_tip_extension_mm: float = 0.0

    def __post_init__(self) -> None:
        extension = _finite(self.attached_tip_extension_mm, "attached tip extension")
        if extension < 0:
            raise InvalidGeometryError("attached tip extension must be non-negative.")
        if self.instrument is not None:
            object.__setattr__(self, "instrument", _name(self.instrument, "instrument"))
        if extension > 0 and self.instrument is None:
            raise InvalidGeometryError(
                "A positive attached tip extension requires an instrument name."
            )
        object.__setattr__(self, "attached_tip_extension_mm", extension)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "attached_tip_extension_mm": self.attached_tip_extension_mm,
        }


@dataclass(frozen=True)
class ToolStateChange:
    """A dynamic tool-state change at an exact carriage position."""

    position: Point3D
    instrument: str
    attached_tip_extension_mm: float

    def __post_init__(self) -> None:
        if not isinstance(self.position, Point3D):
            raise InvalidGeometryError("ToolStateChange.position must be a Point3D.")
        object.__setattr__(self, "instrument", _name(self.instrument, "instrument"))
        extension = _finite(self.attached_tip_extension_mm, "attached tip extension")
        if extension < 0:
            raise InvalidGeometryError("attached tip extension must be non-negative.")
        object.__setattr__(self, "attached_tip_extension_mm", extension)

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position.to_dict(),
            "instrument": self.instrument,
            "attached_tip_extension_mm": self.attached_tip_extension_mm,
        }


@dataclass(frozen=True)
class MotionSegment:
    """One collision-checked single-axis or coordinated-XY movement."""

    kind: str
    start: Point3D
    end: Point3D
    phase: str
    tool_state: ToolState = field(default_factory=ToolState)
    access: AccessScope = field(default_factory=AccessScope)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _name(self.kind, "segment kind"))
        object.__setattr__(self, "phase", _name(self.phase, "segment phase"))
        if not isinstance(self.start, Point3D) or not isinstance(self.end, Point3D):
            raise InvalidGeometryError("MotionSegment endpoints must be Point3D values.")
        if not isinstance(self.tool_state, ToolState):
            raise InvalidGeometryError("MotionSegment.tool_state must be a ToolState.")
        if not isinstance(self.access, AccessScope):
            raise InvalidGeometryError("MotionSegment.access must be an AccessScope.")
        changed_axes = tuple(
            axis for axis in ("x", "y", "z")
            if getattr(self.start, axis) != getattr(self.end, axis)
        )
        if len(changed_axes) != 1 and changed_axes != ("x", "y"):
            raise InvalidGeometryError(
                "MotionSegment must move along one axis or coordinated XY "
                "with distinct endpoints."
            )

    @property
    def axis(self) -> str:
        changed = "".join(
            axis for axis in ("x", "y", "z")
            if getattr(self.start, axis) != getattr(self.end, axis)
        )
        return changed

    @property
    def length_mm(self) -> float:
        return math.sqrt(sum(
            (getattr(self.end, axis) - getattr(self.start, axis)) ** 2
            for axis in self.axis
        ))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
            "axis": self.axis,
            "phase": self.phase,
            "tool_state": self.tool_state.to_dict(),
            "access": self.access.to_dict(),
        }


@dataclass(frozen=True)
class MotionPlan:
    """A deterministic, serializable sequence of exact carriage moves."""

    command: str
    operation: str
    start: Point3D
    end: Point3D
    strategy: str
    segments: tuple[MotionSegment, ...]
    state_changes: tuple[ToolStateChange, ...] = ()
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", _name(self.command, "command"))
        object.__setattr__(self, "operation", _name(self.operation, "operation"))
        object.__setattr__(self, "strategy", _name(self.strategy, "strategy"))
        if type(self.version) is not int or self.version != 1:
            raise InvalidGeometryError("Only motion-plan version 1 is supported.")
        if not isinstance(self.start, Point3D) or not isinstance(self.end, Point3D):
            raise InvalidGeometryError("MotionPlan endpoints must be Point3D values.")
        object.__setattr__(self, "segments", tuple(self.segments))
        object.__setattr__(self, "state_changes", tuple(self.state_changes))
        if not all(isinstance(segment, MotionSegment) for segment in self.segments):
            raise InvalidGeometryError("MotionPlan.segments must contain MotionSegment values.")
        if not all(isinstance(change, ToolStateChange) for change in self.state_changes):
            raise InvalidGeometryError(
                "MotionPlan.state_changes must contain ToolStateChange values."
            )
        if self.segments:
            if self.segments[0].start != self.start or self.segments[-1].end != self.end:
                raise InvalidGeometryError(
                    "MotionPlan segment endpoints must match its exact start and end."
                )
            for previous, current in zip(self.segments, self.segments[1:]):
                if previous.end != current.start:
                    raise InvalidGeometryError("MotionPlan segments must form a continuous path.")
        elif self.start != self.end:
            raise InvalidGeometryError(
                "A non-stationary MotionPlan requires at least one segment."
            )
        boundaries = {self.start, self.end}
        boundaries.update(segment.start for segment in self.segments)
        boundaries.update(segment.end for segment in self.segments)
        off_route = [
            change.position for change in self.state_changes
            if change.position not in boundaries
        ]
        if off_route:
            raise InvalidGeometryError(
                "MotionPlan state changes must occur at an exact segment boundary; "
                f"off-route positions: {[point.to_dict() for point in off_route]}."
            )
        changes_by_position = {
            boundary: [change for change in self.state_changes if change.position == boundary]
            for boundary in boundaries
        }

        def _matches(change: ToolStateChange, state: ToolState) -> bool:
            instrument_matches = (
                state.instrument is None or change.instrument == state.instrument
            )
            return instrument_matches and (
                change.attached_tip_extension_mm == state.attached_tip_extension_mm
            )

        if self.segments and changes_by_position.get(self.start):
            start_changes = changes_by_position[self.start]
            if len(start_changes) != 1 or not _matches(start_changes[0], self.segments[0].tool_state):
                raise InvalidGeometryError(
                    "MotionPlan start state change must match the first segment tool state."
                )
        for previous, current in zip(self.segments, self.segments[1:]):
            changes = changes_by_position.get(current.start, [])
            state_changed = previous.tool_state != current.tool_state
            if state_changed:
                if len(changes) != 1 or not _matches(changes[0], current.tool_state):
                    raise InvalidGeometryError(
                        "Every segment tool-state transition requires exactly one "
                        "matching ToolStateChange at the shared boundary."
                    )
            elif changes:
                raise InvalidGeometryError(
                    "ToolStateChange metadata cannot appear where adjacent segment "
                    "tool states are unchanged."
                )

    @property
    def length_mm(self) -> float:
        return sum(segment.length_mm for segment in self.segments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "command": self.command,
            "operation": self.operation,
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
            "strategy": self.strategy,
            "segments": [segment.to_dict() for segment in self.segments],
            "state_changes": [change.to_dict() for change in self.state_changes],
        }
