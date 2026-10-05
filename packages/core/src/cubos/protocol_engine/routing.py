"""Central planning bridge shared by protocol runtime and offline previews."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Mapping

from cubos.deck.labware.tip_rack import TipRack, resolve_tip_rack_slot
from cubos.instruments.camera.interface import CameraInstrument
from cubos.motion_planning import (
    AABB,
    AccessScope,
    Corridor,
    InvalidGeometryError,
    MotionPlan,
    MotionPlanningError,
    NoRouteError,
    Point3D,
    Scene,
    ToolEnvelope,
    ToolState,
    ToolStateChange,
    build_scene,
    plan_motion,
)

from .errors import ProtocolExecutionError


SUPPORTED_PLANNED_COMMANDS = frozenset({
    "move", "pick_up_tip", "transfer", "mix", "drop_tip", "capture",
    "measure_color", "photo_pause",
})


@dataclass(frozen=True)
class PreparedMotionStep:
    command: str
    plans: tuple[MotionPlan, ...]
    data: Mapping[str, Any]


def validate_planning_configuration(protocol: Any, context: Any) -> None:
    """Run position-independent planning checks before hardware connection."""
    unsupported = [
        f"step {step.index} ({step.command_name})"
        for step in protocol.steps
        if step.command_name not in SUPPORTED_PLANNED_COMMANDS
    ]
    if unsupported:
        raise ProtocolExecutionError(
            "motion planning is enabled, but routing v1 does not support "
            f"{', '.join(unsupported)}. No protocol movement was attempted."
        )
    if context.gantry_config is None:
        raise ProtocolExecutionError(
            "motion planning requires gantry_config to validate carriage bounds."
        )
    if context.fluid_state_id is not None and context.data_store is None:
        raise ProtocolExecutionError(
            "durable routed execution requires a data store so persisted tip "
            "occupancy can be checked before movement."
        )
    try:
        _build_scene_from_context(context)
        for step in protocol.steps:
            args = step.args
            if step.command_name == "transfer" and args.get("require_uncapped"):
                raise InvalidGeometryError(
                    "routing v1 transfer does not support require_uncapped because "
                    "that option requires durable cap/fluid state."
                )
            if step.command_name == "move":
                position = args["position"]
                if not isinstance(position, (list, tuple)) and not (
                    isinstance(position, str) and position in context.positions
                ):
                    _motion_for(context.deck, _fixture_key(context.deck, position))
            elif step.command_name == "pick_up_tip":
                rack, _ = resolve_tip_rack_slot(context.deck, args["position"])
                rack_key = (
                    args["position"].rsplit(".", 1)[0]
                    if "." in args["position"] else args["position"]
                )
                motion = _motion_for(context.deck, rack_key)
                policy = motion.get("access", {}).get(
                    "pick_up_tip", {"strategy": "vertical"},
                )
                if policy["strategy"] == "side_exit":
                    if motion.get("occupied_tip_radius_mm") is None:
                        raise InvalidGeometryError(
                            f"side-exit rack {rack_key!r} requires occupied_tip_radius_mm."
                        )
                    envelope = context.gantry.motion_envelopes.get("pipette", {})
                    if envelope.get("attached_tip_radius_mm") is None:
                        raise InvalidGeometryError(
                            "side-exit pickup requires pipette "
                            "motion_envelope.attached_tip_radius_mm."
                        )
                del rack
            elif step.command_name == "transfer":
                _motion_for(context.deck, _fixture_key(context.deck, args["source"]))
                _motion_for(context.deck, _fixture_key(context.deck, args["destination"]))
            elif step.command_name in {"mix", "drop_tip"}:
                _motion_for(context.deck, _fixture_key(context.deck, args["position"]))
            elif step.command_name in {"capture", "measure_color"}:
                command = step.command_name
                instrument_name = args["instrument"]
                try:
                    instrument = context.gantry.instruments[instrument_name]
                except KeyError as exc:
                    raise InvalidGeometryError(
                        f"Unknown planned {command} instrument {instrument_name!r}."
                    ) from exc
                if not isinstance(instrument, CameraInstrument):
                    raise InvalidGeometryError(
                        f"Planned {command} instrument {instrument_name!r} is a "
                        f"{type(instrument).__name__}, not a CameraInstrument."
                    )
                if args.get("position") is not None:
                    position = args["position"]
                    try:
                        context.deck.resolve_coordinate(position)
                    except (KeyError, ValueError, TypeError, AttributeError) as exc:
                        raise InvalidGeometryError(
                            f"Planned camera position {position!r} cannot be "
                            f"resolved: {exc}"
                        ) from exc
    except (KeyError, ValueError, TypeError) as exc:
        if isinstance(exc, ProtocolExecutionError):
            raise
        raise ProtocolExecutionError(
            f"motion-planning configuration preflight failed: {type(exc).__name__}: {exc}. "
            "No hardware connection or movement was attempted."
        ) from exc


def prepare_planning_context(protocol: Any, context: Any) -> None:
    """Preflight every planned step before the protocol sends any movement."""
    validate_planning_configuration(protocol, context)
    try:
        session = RoutingSession(context)
        prepared = session.preview_protocol(protocol)
    except MotionPlanningError as exc:
        raise ProtocolExecutionError(
            f"motion-planning preflight failed: {exc}. No protocol movement was attempted."
        ) from exc
    context.routing_session = session
    context.planned_motion_steps = {step_index: value for step_index, value in prepared.items()}
    context.motion_plans = [
        plan
        for step_index in sorted(prepared)
        for plan in prepared[step_index].plans
    ]


def prepared_step(context: Any, command: str) -> PreparedMotionStep | None:
    """Return the preflighted active step, or ``None`` on a legacy deck."""
    if not context.deck.planning_enabled:
        return None
    if context.routing_session is None or context.active_step_index is None:
        raise ProtocolExecutionError(
            f"planned {command} requires whole-protocol preflight through Protocol.execute()."
        )
    try:
        result = context.planned_motion_steps[context.active_step_index]
    except KeyError as exc:
        raise ProtocolExecutionError(
            f"planned {command} has no preflighted motion for step {context.active_step_index}."
        ) from exc
    if result.command != command:
        raise ProtocolExecutionError(
            f"planned step mismatch: expected {command!r}, found {result.command!r}."
        )
    return result


class RoutingSession:
    """Resolved immutable scene plus exact plan execution."""

    def __init__(self, context: Any) -> None:
        self.context = context
        self.tip_status = _durable_tip_status(context)
        self.base_scene = _build_scene_from_context(context, self.tip_status)

    def current_carriage(self) -> Point3D:
        try:
            current = self.context.gantry.controller.get_coordinates()
            return Point3D(current["x"], current["y"], current["z"])
        except Exception as exc:
            raise InvalidGeometryError(
                f"Cannot read a finite carriage start pose: {type(exc).__name__}: {exc}"
            ) from exc

    def execute(self, plan: MotionPlan) -> None:
        for segment in plan.segments:
            self._validate_actual_tool_state(segment.tool_state)
            active_tools = segment.access.allowed_tool_names
            instrument = active_tools[0] if len(active_tools) == 1 else None
            self.context.gantry.move_carriage_exact(
                segment.start, segment.end, instrument=instrument,
            )

    def _validate_actual_tool_state(self, planned: ToolState) -> None:
        attached = []
        for name, instrument in self.context.gantry.instruments.items():
            extension = float(getattr(instrument, "attached_tip_extension", 0.0) or 0.0)
            if extension > 0:
                attached.append((name, extension))
        expected = (
            [] if planned.attached_tip_extension_mm == 0
            else [(planned.instrument, planned.attached_tip_extension_mm)]
        )
        if attached != expected:
            raise ProtocolExecutionError(
                "Cached motion-plan tool state does not match the mounted state: "
                f"planned {expected}, observed {attached}. No motion was sent."
            )

    def preview_protocol(self, protocol: Any) -> dict[int, PreparedMotionStep]:
        current = self.current_carriage()
        extension = float(
            getattr(self.context.gantry.instruments.get("pipette"), "attached_tip_extension", 0.0)
            or 0.0
        )
        state = ToolState("pipette" if extension else None, extension)
        current_target: str | None = None
        current_instrument: str | None = None
        consumed: set[str] = {
            f"{rack_key}.tip.{slot_id}"
            for (rack_key, slot_id), status in self.tip_status.items()
            if status != "available"
        }
        self._preview_consumed: set[str] = set()
        prepared: dict[int, PreparedMotionStep] = {}

        for step in protocol.steps:
            command = step.command_name
            args = step.args
            if command == "move":
                plan, current = self._plan_move(
                    current,
                    current_target=current_target,
                    current_instrument=current_instrument,
                    tool_state=state,
                    **args,
                )
                value = PreparedMotionStep(command, (plan,), {})
                current_target = None
                current_instrument = args["instrument"]
            elif command == "photo_pause":
                value = PreparedMotionStep(command, (), {})
            elif command == "pick_up_tip":
                value, current, state, current_target, consumed = self._plan_pickup(
                    current,
                    position=args["position"],
                    tool_state=state,
                    current_target=current_target,
                    current_instrument=current_instrument,
                    consumed=consumed,
                )
                current_instrument = "pipette"
            elif command == "transfer":
                stroke_count = _transfer_stroke_count(self.context, args)
                value, current, current_target = self._plan_transfer(
                    current,
                    source=args["source"],
                    destination=args["destination"],
                    source_height=float(args.get("source_height") or 0.0),
                    destination_height=float(args.get("destination_height") or 0.0),
                    stroke_count=stroke_count,
                    tool_state=state,
                    current_target=current_target,
                    current_instrument=current_instrument,
                )
                current_instrument = "pipette"
            elif command == "mix":
                value, current, current_target = self._plan_mix(
                    current,
                    position=args["position"],
                    height=float(args.get("height", 0.0)),
                    cycles=int(args.get("cycles", 3)),
                    tool_state=state,
                    current_target=current_target,
                    current_instrument=current_instrument,
                )
                current_instrument = "pipette"
            elif command == "drop_tip":
                value, current, state, current_target = self._plan_drop(
                    current,
                    position=args["position"],
                    tool_state=state,
                    current_target=current_target,
                    current_instrument=current_instrument,
                )
                current_instrument = "pipette"
            elif command == "capture":
                value = PreparedMotionStep(
                    command, (), {"instrument": args["instrument"]},
                )
            elif command == "measure_color":
                value, current, current_target = self._plan_measure_color(
                    current,
                    instrument=args["instrument"],
                    position=args.get("position"),
                    image_height=args.get("image_height"),
                    tool_state=state,
                    current_target=current_target,
                    current_instrument=current_instrument,
                )
                current_instrument = args["instrument"]
            else:
                raise InvalidGeometryError(f"Unsupported planned command {command!r}.")
            prepared[step.index] = value
            self._preview_consumed = set(consumed)
        return prepared

    def _plan_move(
        self,
        current: Point3D,
        *,
        instrument: str,
        position: Any,
        travel_z: float | None = None,
        current_target: str | None,
        current_instrument: str | None,
        tool_state: ToolState,
    ) -> tuple[MotionPlan, Point3D]:
        if travel_z is not None:
            raise InvalidGeometryError(
                "planned move rejects travel_z because exact segment order is planner-owned."
            )
        if isinstance(position, (list, tuple)):
            if len(position) != 3:
                raise InvalidGeometryError("planned move position must contain exactly XYZ.")
            action = Point3D(*position)
            target_fixture = None
        elif isinstance(position, str) and position in self.context.positions:
            action = Point3D(*self.context.positions[position])
            target_fixture = None
        else:
            coordinate = self.context.deck.resolve_coordinate(position)
            target_fixture = _fixture_key(self.context.deck, position)
            mounted = self.context.gantry.instruments[instrument]
            end = Point3D(
                coordinate.x - mounted.offset_x,
                coordinate.y - mounted.offset_y,
                self.context.gantry_config.working_volume.z_max,
            )
        if target_fixture is None:
            end = _carriage_for(self.context, instrument, action, tool_state)
        plan = self._approach(
            current, end, instrument, tool_state,
            previous_target=current_target,
            previous_instrument=current_instrument,
            command="move", phase="transit", final_target=target_fixture,
        )
        return plan, end

    def _plan_pickup(
        self,
        current: Point3D,
        *,
        position: str,
        tool_state: ToolState,
        current_target: str | None,
        current_instrument: str | None,
        consumed: set[str],
    ) -> tuple[PreparedMotionStep, Point3D, ToolState, str | None, set[str]]:
        if tool_state.attached_tip_extension_mm:
            raise InvalidGeometryError("pick_up_tip requires a bare pipette.")
        rack, tip_id = resolve_tip_rack_slot(self.context.deck, position)
        rack_key = position.rsplit(".", 1)[0] if "." in position else position
        if tip_id is None:
            tip_id = next(
                (
                    key for key in rack.tips
                    if self._tip_is_available(rack_key, key, rack)
                    and f"{rack_key}.tip.{key}" not in consumed
                ),
                None,
            )
        if tip_id is None:
            raise InvalidGeometryError(f"pick_up_tip target {position!r} is unavailable.")
        target_name = f"{rack_key}.{tip_id}"
        target_tip_fixture = f"{rack_key}.tip.{tip_id}"
        if (
            target_tip_fixture in consumed
            or not self._tip_is_available(rack_key, tip_id, rack)
        ):
            raise InvalidGeometryError(f"pick_up_tip target {target_name!r} is unavailable.")
        coordinate = rack.get_tip_location(tip_id)
        target = Point3D(coordinate.x, coordinate.y, coordinate.z)
        bare = ToolState()
        attached = ToolState("pipette", rack.tip_length)
        fixture = _motion_for(self.context.deck, rack_key)
        policy = fixture.get("access", {}).get("pick_up_tip", {"strategy": "vertical"})
        engage = _carriage_for(self.context, "pipette", target, bare)

        if policy["strategy"] == "side_exit":
            world_edge = _world_edge(fixture, policy["exit_edge"])
            if fixture.get("occupied_tip_radius_mm") is None:
                raise InvalidGeometryError(
                    f"side-exit rack {rack_key!r} requires occupied_tip_radius_mm."
                )
            envelope = self.context.gantry.motion_envelopes.get("pipette", {})
            if envelope.get("attached_tip_radius_mm") is None:
                raise InvalidGeometryError(
                    "side-exit pickup requires pipette motion_envelope.attached_tip_radius_mm."
                )
            blockers = _side_exit_blockers(
                rack, tip_id, world_edge, consumed, rack_key,
                durable_status=self.tip_status,
            )
            if blockers:
                raise InvalidGeometryError(
                    f"side-exit lane for {target_name!r} is blocked by loaded tips {blockers}."
                )
            lift = float(policy["lift_mm"])
            approach = Point3D(engage.x, engage.y, engage.z + lift)
            entrance = self._approach(
                current, approach, "pipette", bare,
                previous_target=current_target,
                previous_instrument=current_instrument,
                command="pick_up_tip", phase="approach", consumed=consumed,
            )
            ingress = self._plan(
                approach, engage, "pipette", bare, (rack_key, target_tip_fixture),
                "pick_up_tip", "engage", consumed=consumed,
            )
            lifted = Point3D(engage.x, engage.y, engage.z + lift)
            body = _box_from_mapping(rack_key, fixture["resolved_box"])
            clearance = float(policy.get("clearance_mm", 0.0))
            edge = world_edge
            exit_point = _side_exit_point(
                lifted, body, edge, clearance, self.base_scene, attached,
            )
            lift_plan = self._plan(
                engage, lifted, "pipette", attached, (rack_key, target_tip_fixture),
                "pick_up_tip", "lift", consumed=consumed,
            )
            exit_plan = self._plan(
                lifted, exit_point, "pipette", attached, (rack_key, target_tip_fixture),
                "pick_up_tip", "exit", consumed=consumed,
            )
            if any(segment.axis != edge[0] for segment in exit_plan.segments):
                raise InvalidGeometryError("side-exit route is not a single exact exit-axis withdrawal.")
            clear_scene, _ = self._scene_and_access(
                "pipette", (), consumed, exit_point, exit_point, attached,
            )
            plan_motion(
                clear_scene, exit_point, exit_point,
                tool_state=attached,
                command="pick_up_tip",
                operation="post_exit_clearance",
                phase="post_exit_clearance",
            )
            plans = (entrance, ingress, lift_plan, exit_plan)
            end = exit_point
        else:
            hover = Point3D(engage.x, engage.y, self.context.gantry_config.working_volume.z_max)
            entrance = self._approach(
                current, hover, "pipette", bare,
                previous_target=current_target,
                previous_instrument=current_instrument,
                command="pick_up_tip", phase="approach", consumed=consumed,
            )
            ingress = self._plan(
                hover, engage, "pipette", bare, (rack_key, target_tip_fixture),
                "pick_up_tip", "engage", consumed=consumed,
            )
            departure = self._plan(
                engage, hover, "pipette", attached, (rack_key, target_tip_fixture),
                "pick_up_tip", "departure", consumed=consumed,
            )
            clear_scene, _ = self._scene_and_access(
                "pipette", (), consumed, hover, hover, attached,
            )
            plan_motion(
                clear_scene, hover, hover,
                tool_state=attached,
                command="pick_up_tip",
                operation="post_pickup_clearance",
                phase="post_pickup_clearance",
            )
            plans = (entrance, ingress, departure)
            end = hover

        change = ToolStateChange(engage, "pipette", rack.tip_length)
        departure_index = 2
        changed_plan = plans[departure_index]
        plans = plans[:departure_index] + (
            MotionPlan(
                command=changed_plan.command,
                operation=changed_plan.operation,
                start=changed_plan.start,
                end=changed_plan.end,
                strategy=changed_plan.strategy,
                segments=changed_plan.segments,
                state_changes=(change,),
            ),
        ) + plans[departure_index + 1:]
        consumed = set(consumed)
        consumed.add(target_tip_fixture)
        return (
            PreparedMotionStep(
                "pick_up_tip", plans,
                {"rack_key": rack_key, "tip_id": tip_id, "position": target_name, "attach_after_plan": 1},
            ),
            end, attached, None, consumed,
        )

    def _plan_transfer(
        self,
        current: Point3D,
        *,
        source: str,
        destination: str,
        source_height: float,
        destination_height: float,
        stroke_count: int,
        tool_state: ToolState,
        current_target: str | None,
        current_instrument: str | None,
    ) -> tuple[PreparedMotionStep, Point3D, str]:
        if tool_state.attached_tip_extension_mm <= 0:
            raise InvalidGeometryError("transfer requires an attached pipette tip.")
        plans: list[MotionPlan] = []
        previous_target = current_target
        for _ in range(stroke_count):
            source_plan, current = self._plan_engage(
                current, source, source_height, "pipette", tool_state,
                command="transfer", operation="aspirate", previous_target=previous_target,
                previous_instrument=current_instrument,
            )
            plans.append(source_plan)
            source_key = _fixture_key(self.context.deck, source)
            destination_plan, current = self._plan_engage(
                current, destination, destination_height, "pipette", tool_state,
                command="transfer", operation="dispense", previous_target=source_key,
                previous_instrument="pipette",
            )
            plans.append(destination_plan)
            previous_target = _fixture_key(self.context.deck, destination)
            current_instrument = "pipette"
        return PreparedMotionStep("transfer", tuple(plans), {"stroke_count": stroke_count}), current, previous_target

    def _plan_mix(
        self,
        current: Point3D,
        *,
        position: str,
        height: float,
        cycles: int,
        tool_state: ToolState,
        current_target: str | None,
        current_instrument: str | None,
    ) -> tuple[PreparedMotionStep, Point3D, str]:
        if tool_state.attached_tip_extension_mm <= 0:
            raise InvalidGeometryError("mix requires an attached pipette tip.")
        engage, current = self._plan_engage(
            current, position, height, "pipette", tool_state,
            command="mix", operation="engage", previous_target=current_target,
            previous_instrument=current_instrument,
        )
        fixture_key = _fixture_key(self.context.deck, position)
        plans = [engage]
        low = current
        high = Point3D(low.x, low.y, low.z + 1.0)
        for _ in range(cycles):
            plans.append(self._plan(low, high, "pipette", tool_state, (fixture_key,), "mix", "lift"))
            plans.append(self._plan(high, low, "pipette", tool_state, (fixture_key,), "mix", "lower"))
        return PreparedMotionStep("mix", tuple(plans), {"cycles": cycles}), low, fixture_key

    def _plan_drop(
        self,
        current: Point3D,
        *,
        position: str,
        tool_state: ToolState,
        current_target: str | None,
        current_instrument: str | None,
    ) -> tuple[PreparedMotionStep, Point3D, ToolState, None]:
        if tool_state.attached_tip_extension_mm <= 0:
            raise InvalidGeometryError("drop_tip requires an attached pipette tip.")
        ingress, engage = self._plan_engage(
            current, position, 0.0, "pipette", tool_state,
            command="drop_tip", operation="engage", previous_target=current_target,
            previous_instrument=current_instrument,
        )
        bare = ToolState()
        fixture_key = _fixture_key(self.context.deck, position)
        hover = Point3D(engage.x, engage.y, self.context.gantry_config.working_volume.z_max)
        departure = self._plan(
            engage, hover, "pipette", bare, (fixture_key,), "drop_tip", "departure",
        )
        clear_scene, _ = self._scene_and_access(
            "pipette", (), set(), hover, hover, bare,
        )
        plan_motion(
            clear_scene, hover, hover,
            tool_state=bare,
            command="drop_tip",
            operation="post_drop_clearance",
            phase="post_drop_clearance",
        )
        departure = MotionPlan(
            command=departure.command,
            operation=departure.operation,
            start=departure.start,
            end=departure.end,
            strategy=departure.strategy,
            segments=departure.segments,
            state_changes=(ToolStateChange(engage, "pipette", 0.0),),
        )
        return PreparedMotionStep("drop_tip", (ingress, departure), {"drop_after_plan": 0}), hover, bare, None

    def _plan_measure_color(
        self,
        current: Point3D,
        *,
        instrument: str,
        position: str | None,
        image_height: float | None,
        tool_state: ToolState,
        current_target: str | None,
        current_instrument: str | None,
    ) -> tuple[PreparedMotionStep, Point3D, None]:
        data = {"instrument": instrument, "capture_after_plan": -1}
        if image_height is None:
            return PreparedMotionStep("measure_color", (), data), current, None
        if position is None:
            raise InvalidGeometryError(
                "planned measure_color image_height requires a deck position."
            )
        try:
            height = float(image_height)
        except (TypeError, ValueError) as exc:
            raise InvalidGeometryError(
                "planned measure_color image_height must be a finite number."
            ) from exc
        if not math.isfinite(height):
            raise InvalidGeometryError(
                "planned measure_color image_height must be a finite number."
            )
        approach, capture_point = self._plan_engage(
            current,
            position,
            height,
            instrument,
            tool_state,
            command="measure_color",
            operation="capture",
            previous_target=current_target,
            previous_instrument=current_instrument,
        )
        fixture_key = _fixture_key(self.context.deck, position)
        ceiling = self.context.gantry_config.working_volume.z_max
        retracted = Point3D(capture_point.x, capture_point.y, ceiling)
        retract = self._plan(
            capture_point,
            retracted,
            instrument,
            tool_state,
            (fixture_key,),
            "measure_color",
            "retract",
        )
        data.update({
            "capture_after_plan": 0,
            "image_height": height,
            "position": position,
        })
        return PreparedMotionStep("measure_color", (approach, retract), data), retracted, None

    def _tip_is_available(self, rack_key: str, tip_id: str, rack: TipRack) -> bool:
        status = self.tip_status.get((rack_key, tip_id))
        if status is not None:
            return status == "available"
        return rack.is_tip_present(tip_id)

    def _plan_engage(
        self,
        current: Point3D,
        position: str,
        height: float,
        instrument: str,
        tool_state: ToolState,
        *,
        command: str,
        operation: str,
        previous_target: str | None,
        previous_instrument: str | None,
    ) -> tuple[MotionPlan, Point3D]:
        coordinate = self.context.deck.resolve_coordinate(position)
        fixture_key = _fixture_key(self.context.deck, position)
        _motion_for(self.context.deck, fixture_key)
        action = Point3D(coordinate.x, coordinate.y, coordinate.z + height)
        end = _carriage_for(self.context, instrument, action, tool_state)
        hover = Point3D(end.x, end.y, self.context.gantry_config.working_volume.z_max)
        approach = self._approach(
            current, hover, instrument, tool_state,
            previous_target=previous_target,
            previous_instrument=previous_instrument,
            command=command, phase=f"{operation}_approach",
        )
        ingress = self._plan(hover, end, instrument, tool_state, (fixture_key,), command, operation)
        return _combine_plans(command, operation, approach, ingress), end

    def _approach(
        self,
        current: Point3D,
        end: Point3D,
        instrument: str,
        tool_state: ToolState,
        *,
        previous_target: str | None,
        previous_instrument: str | None,
        command: str,
        phase: str,
        final_target: str | None = None,
        consumed: set[str] | None = None,
    ) -> MotionPlan:
        consumed = (
            set(self._preview_consumed)
            if consumed is None else set(consumed)
        )
        ceiling = self.context.gantry_config.working_volume.z_max
        lifted = Point3D(current.x, current.y, ceiling)
        hover = Point3D(end.x, end.y, ceiling)
        pieces = [
            self._plan(
                current, lifted, previous_instrument or instrument, tool_state,
                (previous_target,) if previous_target else (),
                command, f"{phase}_departure", consumed=consumed,
            ),
            self._plan(
                lifted, hover, instrument, tool_state, (),
                command, f"{phase}_transit", consumed=consumed,
            ),
        ]
        if hover != end:
            pieces.append(self._plan(
                hover, end, instrument, tool_state,
                (final_target,) if final_target else (),
                command, f"{phase}_arrival", consumed=consumed,
            ))
        return _combine_plans(command, phase, *pieces)

    def _plan(
        self,
        start: Point3D,
        end: Point3D,
        instrument: str,
        tool_state: ToolState,
        target_fixtures: Iterable[str],
        command: str,
        phase: str,
        *,
        consumed: set[str] | None = None,
    ) -> MotionPlan:
        effective_consumed = (
            set(self._preview_consumed)
            if consumed is None else set(consumed)
        )
        scene, access = self._scene_and_access(
            instrument, tuple(dict.fromkeys(target_fixtures)), effective_consumed,
            start, end, tool_state,
        )
        result = plan_motion(
            scene, start, end,
            tool_state=tool_state,
            access=access,
            command=command,
            operation=phase,
            phase=phase,
        )
        changed_axes = [
            axis for axis in ("x", "y", "z")
            if getattr(start, axis) != getattr(end, axis)
        ]
        if target_fixtures and len(changed_axes) == 1:
            if len(result.segments) != 1 or result.segments[0].axis != changed_axes[0]:
                raise NoRouteError(
                    f"Scoped access phase {phase!r} requires one direct "
                    f"{changed_axes[0].upper()} segment; the intended corridor is blocked."
                )
        return result

    def _scene_and_access(
        self,
        instrument: str,
        target_fixtures: tuple[str, ...],
        consumed: set[str],
        start: Point3D,
        end: Point3D,
        tool_state: ToolState,
    ) -> tuple[Scene, AccessScope]:
        fixtures = tuple(
            fixture for fixture in self.base_scene.fixtures
            if fixture.name not in consumed
        )
        fixture_names = {fixture.name for fixture in fixtures}
        corridors: list[Corridor] = []
        allowed: list[str] = []
        corridor_box = _active_tool_swept_box(
            self.base_scene, instrument, start, end, tool_state,
        ).expanded(1e-6, name="active_access_sweep")
        for fixture_key in target_fixtures:
            if fixture_key not in fixture_names:
                if ".tip." in fixture_key and fixture_key in consumed:
                    continue
                raise InvalidGeometryError(
                    f"Target fixture {fixture_key!r} has no registered motion box."
                )
            fixture = next(item for item in fixtures if item.name == fixture_key)
            corridor_name = f"access:{fixture_key}"
            corridor = Corridor(
                corridor_name,
                AABB(
                    f"{corridor_name}:box",
                    corridor_box.minimum,
                    corridor_box.maximum,
                ),
                (fixture_key,),
            )
            corridors.append(corridor)
            allowed.append(fixture_key)
        scene = build_scene(
            fixtures=fixtures,
            tool_envelopes=self.base_scene.tool_envelopes,
            bounds=self.base_scene.bounds,
            clearance_mm=self.base_scene.clearance_mm,
            corridors=corridors,
        )
        return scene, AccessScope(tuple(allowed), tuple(c.name for c in corridors), (instrument,))


def _durable_tip_status(context: Any) -> dict[tuple[str, str], str]:
    if context.fluid_state_id is None:
        return {}
    if context.data_store is None:
        raise InvalidGeometryError(
            "Durable routed execution requires a data store for tip-state preflight."
        )
    try:
        snapshot = context.data_store.get_tip_snapshot(context.fluid_state_id)
    except Exception as exc:
        raise InvalidGeometryError(
            "Cannot read durable tip state before route planning: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return {
        (item["rack_key"], item["slot_id"]): item["status"]
        for item in snapshot["containers"]
    }


def _build_scene_from_context(
    context: Any,
    durable_tip_status: Mapping[tuple[str, str], str] | None = None,
) -> Scene:
    bounds = context.gantry_config.working_volume
    carriage_bounds = AABB(
        "carriage_bounds",
        Point3D(bounds.x_min, bounds.y_min, bounds.z_min),
        Point3D(bounds.x_max, bounds.y_max, bounds.z_max),
    )
    fixtures: list[AABB] = []
    missing_fixture_geometry: list[str] = []
    for key, labware in context.deck.labware.items():
        motion = getattr(labware, "motion", None)
        if motion is None:
            missing_fixture_geometry.append(key)
            continue
        fixtures.append(_box_from_mapping(key, motion["resolved_box"]))
        if isinstance(labware, TipRack):
            radius = motion.get("occupied_tip_radius_mm")
            if radius is None and any(labware.tip_present.values()):
                raise InvalidGeometryError(
                    f"Tip rack {key!r} has occupied tips but no occupied_tip_radius_mm."
                )
            if radius is not None:
                for tip_id, coordinate in labware.tips.items():
                    persisted = (durable_tip_status or {}).get((key, tip_id))
                    tip_present = (
                        persisted == "available"
                        if persisted is not None
                        else labware.is_tip_present(tip_id)
                    )
                    if not tip_present:
                        continue
                    fixtures.append(AABB(
                        f"{key}.tip.{tip_id}",
                        Point3D(coordinate.x - radius, coordinate.y - radius, coordinate.z - labware.tip_length),
                        Point3D(coordinate.x + radius, coordinate.y + radius, coordinate.z),
                    ))

    if missing_fixture_geometry:
        raise InvalidGeometryError(
            "Planning-enabled deck is missing `motion.box` for declared fixtures "
            f"{sorted(missing_fixture_geometry)}."
        )

    envelopes: list[ToolEnvelope] = []
    configured = getattr(context.gantry, "motion_envelopes", {})
    missing = sorted(set(context.gantry.instruments) - set(configured))
    if missing:
        raise InvalidGeometryError(
            f"Planning-enabled gantry is missing motion_envelope for mounted tools {missing}."
        )
    for name, instrument in context.gantry.instruments.items():
        raw = configured[name]
        tcp = Point3D(instrument.offset_x, instrument.offset_y, -instrument.depth)
        envelopes.append(ToolEnvelope(
            name=name,
            box=_tcp_relative_box(name, raw["box"], tcp, "body"),
            tcp=tcp,
            attached_tip_radius_mm=raw.get("attached_tip_radius_mm"),
            additional_boxes=tuple(
                _tcp_relative_box(name, box, tcp, f"body:{index}")
                for index, box in enumerate(raw.get("additional_boxes", ()), start=1)
            ),
        ))
    settings = context.deck.motion_planning or {}
    return build_scene(
        fixtures=fixtures,
        tool_envelopes=envelopes,
        bounds=carriage_bounds,
        clearance_mm=float(settings.get("clearance_mm", 2.0)),
    )


def _box_from_mapping(name: str, value: Mapping[str, Any]) -> AABB:
    minimum = value["min"]
    maximum = value["max"]
    return AABB(
        name,
        Point3D(minimum["x"], minimum["y"], minimum["z"]),
        Point3D(maximum["x"], maximum["y"], maximum["z"]),
    )


def _tcp_relative_box(
    instrument: str,
    value: Mapping[str, Any],
    tcp: Point3D,
    suffix: str,
) -> AABB:
    offset = value["offset"]
    size = value["size"]
    minimum = Point3D(
        tcp.x + offset["x"],
        tcp.y + offset["y"],
        tcp.z + offset["z"],
    )
    return AABB(
        f"{instrument}:{suffix}",
        minimum,
        Point3D(
            minimum.x + size["x"],
            minimum.y + size["y"],
            minimum.z + size["z"],
        ),
    )


def _motion_for(deck: Any, fixture_key: str) -> Mapping[str, Any]:
    labware = deck.resolve_labware(fixture_key)
    motion = getattr(labware, "motion", None)
    if motion is None:
        raise InvalidGeometryError(
            f"Planning-enabled target {fixture_key!r} has no `motion.box` registration."
        )
    return motion


def _fixture_key(deck: Any, target: str) -> str:
    return deck.resolve_labware_target(target).labware_key


def _carriage_for(context: Any, instrument_name: str, target: Point3D, state: ToolState) -> Point3D:
    try:
        instrument = context.gantry.instruments[instrument_name]
    except KeyError as exc:
        raise InvalidGeometryError(f"Unknown planned instrument {instrument_name!r}.") from exc
    depth = float(instrument.depth)
    if state.instrument == instrument_name:
        depth += state.attached_tip_extension_mm
    return Point3D(
        target.x - instrument.offset_x,
        target.y - instrument.offset_y,
        target.z + depth,
    )


def _side_exit_point(
    start: Point3D,
    body: AABB,
    edge: str,
    clearance: float,
    scene: Scene,
    tool_state: ToolState,
) -> Point3D:
    values = {"x": start.x, "y": start.y, "z": start.z}
    axis = edge[0]
    boundary = getattr(body, edge)
    relative_min, relative_max = _all_tool_axis_extents(scene, axis, tool_state)
    if edge.endswith("min"):
        requested = boundary - clearance
        required = boundary - scene.clearance_mm - relative_max - 1e-6
        values[axis] = min(requested, required)
    else:
        requested = boundary + clearance
        required = boundary + scene.clearance_mm - relative_min + 1e-6
        values[axis] = max(requested, required)
    return Point3D(**values)


def _world_edge(motion: Mapping[str, Any], local_edge: str) -> str:
    """Rotate a local A1-frame exit edge into deck-frame axis/direction."""
    local_axis = local_edge[0]
    basis = motion["basis"][local_axis]
    component_axis = "x" if abs(float(basis["x"])) > 0.5 else "y"
    component = float(basis[component_axis])
    local_sign = -1.0 if local_edge.endswith("min") else 1.0
    world_sign = local_sign * component
    return f"{component_axis}_{'min' if world_sign < 0 else 'max'}"


def _all_tool_axis_extents(
    scene: Scene,
    axis: str,
    tool_state: ToolState,
) -> tuple[float, float]:
    parts = [
        box
        for tool in scene.tool_envelopes
        for box in (tool.box, *tool.additional_boxes)
    ]
    lows = [getattr(box.minimum, axis) for box in parts]
    highs = [getattr(box.maximum, axis) for box in parts]
    if tool_state.attached_tip_extension_mm > 0:
        tool = next(
            item for item in scene.tool_envelopes
            if item.name == tool_state.instrument
        )
        if tool.attached_tip_radius_mm is None:
            raise InvalidGeometryError(
                f"Attached-tip instrument {tool.name!r} has no attached tip radius."
            )
        if axis in {"x", "y"}:
            center = getattr(tool.tcp, axis)
            lows.append(center - tool.attached_tip_radius_mm)
            highs.append(center + tool.attached_tip_radius_mm)
        else:
            lows.append(tool.tcp.z - tool_state.attached_tip_extension_mm)
            highs.append(tool.tcp.z)
    return min(lows), max(highs)


def _active_tool_swept_box(
    scene: Scene,
    instrument: str,
    start: Point3D,
    end: Point3D,
    tool_state: ToolState,
) -> AABB:
    try:
        tool = next(item for item in scene.tool_envelopes if item.name == instrument)
    except StopIteration as exc:
        raise InvalidGeometryError(
            f"No motion envelope is registered for active tool {instrument!r}."
        ) from exc
    parts = (tool.box, *tool.additional_boxes)
    rel_min = {
        axis: min(getattr(box.minimum, axis) for box in parts)
        for axis in ("x", "y", "z")
    }
    rel_max = {
        axis: max(getattr(box.maximum, axis) for box in parts)
        for axis in ("x", "y", "z")
    }
    if tool_state.instrument == instrument and tool_state.attached_tip_extension_mm > 0:
        if tool.attached_tip_radius_mm is None:
            raise InvalidGeometryError(
                f"Attached-tip instrument {instrument!r} has no attached tip radius."
            )
        radius = tool.attached_tip_radius_mm
        rel_min["x"] = min(rel_min["x"], tool.tcp.x - radius)
        rel_max["x"] = max(rel_max["x"], tool.tcp.x + radius)
        rel_min["y"] = min(rel_min["y"], tool.tcp.y - radius)
        rel_max["y"] = max(rel_max["y"], tool.tcp.y + radius)
        rel_min["z"] = min(
            rel_min["z"], tool.tcp.z - tool_state.attached_tip_extension_mm,
        )
        rel_max["z"] = max(rel_max["z"], tool.tcp.z)
    return AABB(
        f"{instrument}:access_sweep",
        Point3D(
            min(start.x, end.x) + rel_min["x"],
            min(start.y, end.y) + rel_min["y"],
            min(start.z, end.z) + rel_min["z"],
        ),
        Point3D(
            max(start.x, end.x) + rel_max["x"],
            max(start.y, end.y) + rel_max["y"],
            max(start.z, end.z) + rel_max["z"],
        ),
    )


def _side_exit_blockers(
    rack: TipRack,
    tip_id: str,
    edge: str,
    consumed: set[str],
    rack_key: str,
    *,
    durable_status: Mapping[tuple[str, str], str] | None = None,
) -> list[str]:
    target = rack.get_tip_location(tip_id)
    axis = edge[0]
    target_value = getattr(target, axis)
    blockers = []
    for other_id, coordinate in rack.tips.items():
        persisted = (durable_status or {}).get((rack_key, other_id))
        tip_present = (
            persisted == "available"
            if persisted is not None
            else rack.is_tip_present(other_id)
        )
        if other_id == tip_id or not tip_present:
            continue
        if f"{rack_key}.tip.{other_id}" in consumed:
            continue
        cross_axis = "y" if axis == "x" else "x"
        if abs(getattr(coordinate, cross_axis) - getattr(target, cross_axis)) > 1e-6:
            continue
        other_value = getattr(coordinate, axis)
        between = (
            other_value < target_value if edge.endswith("min")
            else other_value > target_value
        )
        if between:
            blockers.append(other_id)
    return sorted(blockers)


def _combine_plans(command: str, operation: str, *plans: MotionPlan) -> MotionPlan:
    nonempty = [plan for plan in plans if plan.segments]
    if not nonempty:
        first = plans[0]
        return MotionPlan(command, operation, first.start, plans[-1].end, "stationary", ())
    segments = tuple(segment for plan in plans for segment in plan.segments)
    return MotionPlan(
        command,
        operation,
        plans[0].start,
        plans[-1].end,
        "+".join(plan.strategy for plan in plans),
        segments,
    )


def _transfer_stroke_count(context: Any, args: Mapping[str, Any]) -> int:
    from .commands._liquid_transfer import pipette_capacity, plan_strokes
    from cubos.instruments.pipette.liquid_class import IDENTITY_CORRECTION

    pipette = context.gantry.instruments.get("pipette")
    capacity = pipette_capacity(pipette)
    correction = IDENTITY_CORRECTION
    liquid_class = args.get("liquid_class")
    if capacity is not None:
        correction = pipette.correction_for(liquid_class)
    elif liquid_class is not None:
        raise InvalidGeometryError(
            "planned transfer liquid_class requires a pipette with correction metadata."
        )
    return len(plan_strokes(args["volume_ul"], capacity, correction))
