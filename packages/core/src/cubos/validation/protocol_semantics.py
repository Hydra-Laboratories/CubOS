"""Semantic validation for protocol runtime movement assumptions.

The protocol model:

* ``measurement_height`` and ``interwell_scan_height`` are *labware-relative*
  offsets (mm above the well's calibrated surface Z; negative = below)
  and are first-class arguments to the protocol commands that use them.
* ``measurement_height`` is required on ``measure`` and ``scan``.
* ``interwell_scan_height`` is required on ``scan``.
* Instruments do not declare these heights.
* ``gantry.safe_z`` is the absolute deck-frame Z used for inter-labware
  travel and the entry approach for the first well of a scan. Resolved
  approach planes must satisfy ``well.z + interwell_scan_height <= safe_z``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from cubos.gantry.instrument_mount import InstrumentedGantry
from cubos.data.cap_state import CapStateError, resolve_cap_target
from cubos.deck.deck import Deck
from cubos.deck.labware.container_role import STOCK, WASTE
from cubos.deck.labware.tip_rack import (
    TipRackResolutionError,
    resolve_tip_rack_slot,
)
from cubos.deck.labware.vial import Vial
from cubos.deck.labware.vial_grid import VialGrid
from cubos.deck.labware.well_plate import WellPlate
from cubos.gantry.gantry_config import GantryConfig
from cubos.gantry.machine_geometry import FixedStructureBox, fixed_structures_for_gantry
from cubos.protocol_engine.protocol import Protocol
from cubos.protocol_engine.registry import CommandRegistry
from cubos.protocol_engine.scan_args import (
    NormalizedScanArguments,
    normalize_scan_arguments,
    surface_detection_enabled,
)
from cubos.instruments.potentiostat.interface import PotentiostatInstrument

from .errors import ProtocolSemanticViolation

Point3D = tuple[float, float, float]


@dataclass(frozen=True)
class PipetteTipState:
    """Tagged sum: untipped (``extension == 0``) or tipped (``extension > 0``).

    Mutate via ``attach``/``detach``; the constructor refuses to build an
    inconsistent state where ``has_tip`` and ``tip_extension`` disagree.
    """

    tip_extension: float = 0.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.tip_extension, bool)
            or not isinstance(self.tip_extension, (int, float))
            or not math.isfinite(float(self.tip_extension))
            or float(self.tip_extension) < 0.0
        ):
            raise ValueError(
                f"tip_extension must be a non-negative finite number, "
                f"got {self.tip_extension!r}."
            )

    @property
    def has_tip(self) -> bool:
        return self.tip_extension > 0.0

    def attach(self, extension: float) -> "PipetteTipState":
        if extension <= 0.0:
            raise ValueError(
                f"attach() requires a positive tip extension, got {extension!r}."
            )
        return PipetteTipState(tip_extension=float(extension))

    def detach(self) -> "PipetteTipState":
        return PipetteTipState()


def _violation(step_index: int, command: str, message: str) -> ProtocolSemanticViolation:
    return ProtocolSemanticViolation(step_index, command, message)


def _finite_field_violation(
    step_index: int,
    command: str,
    field_name: str,
    value: Any,
) -> ProtocolSemanticViolation | None:
    """Return a per-field finite-number violation, or None when *value* passes.

    Mirrors the message format of ``_movement._assert_finite_number`` so a
    field name and the offending value (with type) are always surfaced.
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        return _violation(
            step_index,
            command,
            f"{field_name} must be a finite number, got "
            f"{type(value).__name__} {value!r}.",
        )
    return None


def _resolved_safe_z(gantry: GantryConfig) -> float:
    return gantry.resolved_safe_z


def _row_major_key(well_id: str) -> tuple[str, int]:
    return (well_id[0], int(well_id[1:]))


def _wells_for_axis(plate: WellPlate, axis: Any) -> list[str]:
    if not isinstance(axis, str):
        return []
    axis = axis.upper()
    if axis.isalpha():
        wells = [well for well in plate.wells if well[0] == axis]
    else:
        wells = [well for well in plate.wells if well[1:] == axis]
    return sorted(wells, key=_row_major_key)


def _gantry_xyz_for_tip(
    instrumented_gantry: InstrumentedGantry,
    instrument: str,
    x: float,
    y: float,
    z: float,
    tip_extension: float = 0.0,
) -> tuple[float, float, float]:
    instr = instrumented_gantry.instruments[instrument]
    return (
        x - instr.offset_x,
        y - instr.offset_y,
        z + instr.depth + tip_extension,
    )


def _format_box(box: FixedStructureBox) -> str:
    return (
        f"X[{box.x_min}, {box.x_max}] "
        f"Y[{box.y_min}, {box.y_max}] "
        f"Z[{box.z_min}, {box.z_max}]"
    )


def _segment_intersects_box(
    start: Point3D,
    end: Point3D,
    box: FixedStructureBox,
) -> bool:
    t_min = 0.0
    t_max = 1.0
    for start_value, end_value, low, high in (
        (start[0], end[0], box.x_min, box.x_max),
        (start[1], end[1], box.y_min, box.y_max),
        (start[2], end[2], box.z_min, box.z_max),
    ):
        delta = end_value - start_value
        if delta == 0:
            if start_value < low or start_value > high:
                return False
            continue
        t1 = (low - start_value) / delta
        t2 = (high - start_value) / delta
        axis_min = min(t1, t2)
        axis_max = max(t1, t2)
        t_min = max(t_min, axis_min)
        t_max = min(t_max, axis_max)
        if t_min > t_max:
            return False
    return True


def _validate_machine_structure_point(
    *,
    step_index: int,
    command_name: str,
    gantry: GantryConfig,
    label: str,
    instrument: str,
    x: float,
    y: float,
    z: float,
) -> list[ProtocolSemanticViolation]:
    violations: list[ProtocolSemanticViolation] = []
    for box in fixed_structures_for_gantry(gantry):
        if box.contains(x, y, z):
            violations.append(_violation(
                step_index,
                command_name,
                f"{label} instrument point ({x}, {y}, {z}) will hit the "
                f"{box.name} ({_format_box(box)}) for instrument {instrument!r}.",
            ))
    return violations


def _validate_machine_structure_segment(
    *,
    step_index: int,
    command_name: str,
    gantry: GantryConfig,
    label: str,
    instrument: str,
    start: Point3D,
    end: Point3D,
) -> list[ProtocolSemanticViolation]:
    violations: list[ProtocolSemanticViolation] = []
    for box in fixed_structures_for_gantry(gantry):
        if _segment_intersects_box(start, end, box):
            violations.append(_violation(
                step_index,
                command_name,
                f"{label} travel segment from {start} to {end} will hit "
                f"the {box.name} ({_format_box(box)}) for "
                f"instrument {instrument!r}.",
            ))
    return violations


def _transit_segments(
    current: Point3D,
    target: Point3D,
    travel_z: float,
) -> list[tuple[str, Point3D, Point3D]]:
    current_x, current_y = current[0], current[1]
    target_x, target_y = target[0], target[1]
    travel_start = (current_x, current_y, travel_z)
    x_done = (target_x, current_y, travel_z)
    y_done = (target_x, target_y, travel_z)
    segments = [
        ("travel_z lift/lower", current, travel_start),
        ("travel_z X travel", travel_start, x_done),
        ("travel_z Y travel", x_done, y_done),
        ("travel_z final Z", y_done, target),
    ]
    return [segment for segment in segments if segment[1] != segment[2]]


def _validate_known_transit(
    *,
    step_index: int,
    command_name: str,
    gantry: GantryConfig,
    label: str,
    instrument: str,
    current: Point3D | None,
    target: Point3D,
    travel_z: float,
) -> list[ProtocolSemanticViolation]:
    if current is None:
        return []

    violations: list[ProtocolSemanticViolation] = []
    for segment_label, start, end in _transit_segments(current, target, travel_z):
        violations.extend(_validate_machine_structure_segment(
            step_index=step_index,
            command_name=command_name,
            gantry=gantry,
            label=f"{label} {segment_label}",
            instrument=instrument,
            start=start,
            end=end,
        ))
    return violations


def _home_pose_for_instrument(
    gantry: GantryConfig,
    instrument: Any,
    tip_extension: float = 0.0,
) -> Point3D:
    volume = gantry.working_volume
    return (
        volume.x_max + instrument.offset_x,
        volume.y_max + instrument.offset_y,
        volume.z_max - instrument.depth - tip_extension,
    )


def _validate_home_waypoints(
    *,
    step_index: int,
    instrumented_gantry: InstrumentedGantry,
    gantry: GantryConfig,
    current_poses: dict[str, Point3D],
    instrument_tip_extensions: dict[str, float] | None = None,
) -> list[ProtocolSemanticViolation]:
    violations: list[ProtocolSemanticViolation] = []
    instrument_tip_extensions = instrument_tip_extensions or {}
    for instrument_name, instrument in instrumented_gantry.instruments.items():
        pose = _home_pose_for_instrument(
            gantry,
            instrument,
            tip_extension=instrument_tip_extensions.get(instrument_name, 0.0),
        )
        violations.extend(_validate_machine_structure_point(
            step_index=step_index,
            command_name="home",
            gantry=gantry,
            label="home pose",
            instrument=instrument_name,
            x=pose[0],
            y=pose[1],
            z=pose[2],
        ))
        current_poses[instrument_name] = pose
    return violations


def _validate_gantry_waypoint(
    *,
    step_index: int,
    command_name: str,
    gantry: GantryConfig,
    label: str,
    instrument: str,
    instrumented_gantry: InstrumentedGantry,
    x: float,
    y: float,
    z: float,
    tip_extension: float = 0.0,
) -> list[ProtocolSemanticViolation]:
    if instrument not in instrumented_gantry.instruments:
        return []

    gx, gy, gz = _gantry_xyz_for_tip(
        instrumented_gantry, instrument, x, y, z, tip_extension=tip_extension,
    )
    volume = gantry.working_volume
    violations: list[ProtocolSemanticViolation] = []
    for axis, value, low, high in (
        ("x", gx, volume.x_min, volume.x_max),
        ("y", gy, volume.y_min, volume.y_max),
        ("z", gz, volume.z_min, volume.z_max),
    ):
        if value < low or value > high:
            violations.append(_violation(
                step_index,
                command_name,
                f"{label} gantry {axis}={value} is outside working volume "
                f"[{low}, {high}] for instrument {instrument!r}.",
            ))

    violations.extend(_validate_machine_structure_point(
        step_index=step_index,
        command_name=command_name,
        gantry=gantry,
        label=label,
        instrument=instrument,
        x=x,
        y=y,
        z=z,
    ))
    return violations


def _validate_below_safe_z(
    *,
    step_index: int,
    command_name: str,
    label: str,
    z: float | None,
    gantry: GantryConfig,
) -> list[ProtocolSemanticViolation]:
    """Verify that absolute Z *z* is at or below ``safe_z`` (the ceiling)."""
    safe_z = _resolved_safe_z(gantry)
    if safe_z is None or z is None:
        return []
    if z > safe_z:
        return [_violation(
            step_index,
            command_name,
            f"{label} ({z}) is above the gantry's safe_z ({safe_z}). "
            "All resolved action and approach Z values must satisfy "
            "z <= safe_z so the gantry can retract above them.",
        )]
    return []


def _height_value(
    args: dict[str, Any],
    field_name: str,
    default: float = 0.0,
) -> Any:
    return args.get(field_name, default)


def _validate_pipette_engage(
    *,
    step_index: int,
    command_name: str,
    label: str,
    position: Any,
    height: Any,
    height_field_name: str = "height",
    tip_extension: float,
    instrumented_gantry: InstrumentedGantry,
    deck: Deck,
    gantry: GantryConfig,
    current_poses: dict[str, Point3D],
) -> tuple[list[ProtocolSemanticViolation], Point3D | None]:
    violations: list[ProtocolSemanticViolation] = []
    finite_violation = _finite_field_violation(
        step_index, command_name, height_field_name, height,
    )
    if finite_violation is not None:
        return [finite_violation], None

    try:
        coord = deck.resolve_coordinate(position)
    except (KeyError, AttributeError, ValueError) as exc:
        return [
            _violation(
                step_index,
                command_name,
                f"{label} position {position!r} cannot be resolved on the deck: {exc}",
            )
        ], None

    action_abs = coord.z + float(height)
    action = (coord.x, coord.y, action_abs)
    safe_z = _resolved_safe_z(gantry)
    if safe_z is not None:
        safe_pose = (coord.x, coord.y, safe_z)
        violations.extend(_validate_gantry_waypoint(
            step_index=step_index,
            command_name=command_name,
            gantry=gantry,
            label=f"{label} safe_z",
            instrument="pipette",
            instrumented_gantry=instrumented_gantry,
            x=safe_pose[0],
            y=safe_pose[1],
            z=safe_pose[2],
            tip_extension=tip_extension,
        ))
        violations.extend(_validate_known_transit(
            step_index=step_index,
            command_name=command_name,
            gantry=gantry,
            label=f"{label} safe_z",
            instrument="pipette",
            current=current_poses.get("pipette"),
            target=safe_pose,
            travel_z=safe_z,
        ))
        violations.extend(_validate_machine_structure_segment(
            step_index=step_index,
            command_name=command_name,
            gantry=gantry,
            label=f"{label} action_z descend",
            instrument="pipette",
            start=safe_pose,
            end=action,
        ))

    violations.extend(_validate_gantry_waypoint(
        step_index=step_index,
        command_name=command_name,
        gantry=gantry,
        label=f"{label} action_z",
        instrument="pipette",
        instrumented_gantry=instrumented_gantry,
        x=coord.x,
        y=coord.y,
        z=action_abs,
        tip_extension=tip_extension,
    ))
    violations.extend(_validate_below_safe_z(
        step_index=step_index,
        command_name=command_name,
        label=f"{label} action_z",
        z=action_abs,
        gantry=gantry,
    ))
    current_poses["pipette"] = action
    return violations, action


def _validate_scan_points(
    *,
    step_index: int,
    plate: str,
    instrument: str,
    instrumented_gantry: InstrumentedGantry,
    gantry: GantryConfig,
    wells: list[tuple[str, Any]],
    action_abs: float,
    approach_abs: float,
    safe_z: float | None,
    tip_extension: float = 0.0,
) -> list[ProtocolSemanticViolation]:
    violations: list[ProtocolSemanticViolation] = []
    for well_index, (well_id, well) in enumerate(wells):
        if well_index == 0 and safe_z is not None:
            violations.extend(_validate_gantry_waypoint(
                step_index=step_index,
                command_name="scan",
                gantry=gantry,
                label=f"{plate}.{well_id} safe_z",
                instrument=instrument,
                instrumented_gantry=instrumented_gantry,
                x=well.x,
                y=well.y,
                z=safe_z,
                tip_extension=tip_extension,
            ))
        violations.extend(_validate_gantry_waypoint(
            step_index=step_index,
            command_name="scan",
            gantry=gantry,
            label=f"{plate}.{well_id} action_z",
            instrument=instrument,
            instrumented_gantry=instrumented_gantry,
            x=well.x,
            y=well.y,
            z=action_abs,
            tip_extension=tip_extension,
        ))
        violations.extend(_validate_gantry_waypoint(
            step_index=step_index,
            command_name="scan",
            gantry=gantry,
            label=f"{plate}.{well_id} approach_z",
            instrument=instrument,
            instrumented_gantry=instrumented_gantry,
            x=well.x,
            y=well.y,
            z=approach_abs,
            tip_extension=tip_extension,
        ))
    return violations


def _validate_scan_segments(
    *,
    step_index: int,
    plate: str,
    instrument: str,
    gantry: GantryConfig,
    wells: list[tuple[str, Any]],
    current: Point3D | None,
    action_abs: float,
    approach_abs: float,
    safe_z: float | None,
) -> tuple[list[ProtocolSemanticViolation], Point3D | None]:
    violations: list[ProtocolSemanticViolation] = []
    pose = current

    for well_index, (well_id, well) in enumerate(wells):
        approach = (well.x, well.y, approach_abs)
        action = (well.x, well.y, action_abs)

        if well_index == 0 and safe_z is not None:
            entry = (well.x, well.y, safe_z)
            violations.extend(_validate_known_transit(
                step_index=step_index,
                command_name="scan",
                gantry=gantry,
                label=f"{plate}.{well_id} safe_z",
                instrument=instrument,
                current=pose,
                target=entry,
                travel_z=safe_z,
            ))
            violations.extend(_validate_machine_structure_segment(
                step_index=step_index,
                command_name="scan",
                gantry=gantry,
                label=f"{plate}.{well_id} safe_z to approach_z",
                instrument=instrument,
                start=entry,
                end=approach,
            ))
        elif well_index > 0:
            violations.extend(_validate_known_transit(
                step_index=step_index,
                command_name="scan",
                gantry=gantry,
                label=f"{plate}.{well_id} approach_z",
                instrument=instrument,
                current=pose,
                target=approach,
                travel_z=approach_abs,
            ))

        violations.extend(_validate_machine_structure_segment(
            step_index=step_index,
            command_name="scan",
            gantry=gantry,
            label=f"{plate}.{well_id} action_z descend",
            instrument=instrument,
            start=approach,
            end=action,
        ))
        pose = action

    if wells and pose is not None:
        last_well_id, last_well = wells[-1]
        final_approach = (last_well.x, last_well.y, approach_abs)
        violations.extend(_validate_machine_structure_segment(
            step_index=step_index,
            command_name="scan",
            gantry=gantry,
            label=f"{plate}.{last_well_id} final approach_z",
            instrument=instrument,
            start=pose,
            end=final_approach,
        ))
        pose = final_approach

    return violations, pose


def _validate_scan_command(
    *,
    step_index: int,
    args: dict[str, Any],
    instrumented_gantry: InstrumentedGantry,
    deck: Deck,
    gantry: GantryConfig,
    current_poses: dict[str, Point3D],
    pipette_tip_extension: float = 0.0,
) -> list[ProtocolSemanticViolation]:
    violations: list[ProtocolSemanticViolation] = []

    try:
        normalized = normalize_scan_arguments(
            method_kwargs=args.get("method_kwargs"),
        )
    except ValueError as exc:
        return [_violation(step_index, "scan", str(exc))]

    relative_action = args.get("measurement_height")
    relative_approach = args.get("interwell_scan_height")
    if relative_action is None:
        violations.append(_violation(
            step_index,
            "scan",
            "`measurement_height` is required on `scan` (labware-relative "
            "offset, mm above the well's calibrated surface Z).",
        ))
        return violations
    if relative_approach is None:
        violations.append(_violation(
            step_index,
            "scan",
            "`interwell_scan_height` is required on `scan` (labware-relative "
            "offset for between-wells XY travel).",
        ))
        return violations

    instrument = args.get("instrument")
    plate = args.get("plate")
    if instrument not in instrumented_gantry.instruments:
        violations.append(_violation(
            step_index,
            "scan",
            f"unknown instrument {instrument!r}. Available: "
            f"{', '.join(sorted(instrumented_gantry.instruments.keys()))}.",
        ))
        return violations
    tip_ext = pipette_tip_extension if instrument == "pipette" else 0.0

    try:
        plate_obj = deck.resolve_labware(plate)
    except (KeyError, AttributeError, ValueError) as exc:
        violations.append(_violation(
            step_index,
            "scan",
            f"plate {plate!r} cannot be resolved on the deck: {exc}",
        ))
        return violations
    if not isinstance(plate_obj, WellPlate):
        violations.append(_violation(
            step_index,
            "scan",
            f"plate {plate!r} must resolve to a WellPlate, got "
            f"{type(plate_obj).__name__}.",
        ))
        return violations

    try:
        ref_z = plate_obj.get_well_center("A1").z
    except KeyError:
        violations.append(_violation(
            step_index,
            "scan",
            f"plate {plate!r} has no calibrated A1 well; cannot resolve "
            "the surface Z reference for labware-relative heights.",
        ))
        return violations

    finite_violations = [
        v for v in (
            _finite_field_violation(
                step_index, "scan", "measurement_height", relative_action,
            ),
            _finite_field_violation(
                step_index, "scan", "interwell_scan_height", relative_approach,
            ),
        ) if v is not None
    ]
    if finite_violations:
        violations.extend(finite_violations)
        return violations

    if relative_approach < relative_action:
        violations.append(_violation(
            step_index,
            "scan",
            f"interwell_scan_height ({relative_approach}) is below "
            f"measurement_height ({relative_action}). In +Z-up, the "
            "approach must be at or above the action plane.",
        ))

    action_abs = ref_z + relative_action
    approach_abs = ref_z + relative_approach

    safe_z = _resolved_safe_z(gantry)
    if safe_z is not None and approach_abs > safe_z:
        violations.append(_violation(
            step_index,
            "scan",
            f"resolved approach Z ({approach_abs:.3f} = "
            f"{ref_z}+{relative_approach}) is above the gantry's safe_z "
            f"({safe_z}). Lower `interwell_scan_height` or raise `safe_z`.",
        ))

    sorted_wells = sorted(
        plate_obj.wells.items(),
        key=lambda item: _row_major_key(item[0]),
    )
    violations.extend(_validate_scan_points(
        step_index=step_index,
        plate=plate,
        instrument=instrument,
        instrumented_gantry=instrumented_gantry,
        gantry=gantry,
        wells=sorted_wells,
        action_abs=action_abs,
        approach_abs=approach_abs,
        safe_z=safe_z,
        tip_extension=tip_ext,
    ))

    segment_violations, final_pose = _validate_scan_segments(
        step_index=step_index,
        plate=plate,
        instrument=instrument,
        gantry=gantry,
        wells=sorted_wells,
        current=current_poses.get(instrument),
        action_abs=action_abs,
        approach_abs=approach_abs,
        safe_z=safe_z,
    )
    violations.extend(segment_violations)
    if final_pose is not None:
        current_poses[instrument] = final_pose

    violations.extend(_validate_asmi_indentation(
        step_index=step_index,
        args=args,
        ref_z=ref_z,
        relative_action=relative_action,
        normalized=normalized,
        instrumented_gantry=instrumented_gantry,
        gantry=gantry,
    ))
    violations.extend(_indentation_limit_frame_violations(
        step_index=step_index,
        command_name="scan",
        indentation_limit_height=args.get("indentation_limit_height"),
        relative_action=relative_action,
        method_kwargs=normalized.method_kwargs,
    ))
    return violations


def _indentation_limit_frame_violations(
    *,
    step_index: int,
    command_name: str,
    indentation_limit_height: Any,
    relative_action: float,
    method_kwargs: dict[str, Any],
) -> list[ProtocolSemanticViolation]:
    """Check ``indentation_limit_height`` against its anchoring frame.

    Without surface detection the limit shares the calibrated-well frame
    with ``measurement_height`` and must sit at or below it. With
    ``detect_surface`` the limit is anchored to the sensor-detected sample
    surface — comparing it to ``measurement_height`` would be a frame
    mismatch — and must instead be at or below zero.
    """
    if (
        indentation_limit_height is None
        or not isinstance(indentation_limit_height, (int, float))
        or isinstance(indentation_limit_height, bool)
        or not math.isfinite(float(indentation_limit_height))
    ):
        return []
    if surface_detection_enabled(method_kwargs):
        if indentation_limit_height > 0:
            return [_violation(
                step_index, command_name,
                f"indentation_limit_height ({indentation_limit_height}) must "
                "be at or below 0 when detect_surface is enabled — it is "
                "anchored to the detected sample surface (negative = into "
                "the sample).",
            )]
        return []
    if indentation_limit_height > relative_action:
        return [_violation(
            step_index, command_name,
            f"indentation_limit_height ({indentation_limit_height}) is above "
            f"measurement_height ({relative_action}). The deepest descent "
            "plane must be at or below the action plane in +Z-up.",
        )]
    return []


def _validate_measure_command(
    *,
    step_index: int,
    args: dict[str, Any],
    instrumented_gantry: InstrumentedGantry,
    deck: Deck,
    gantry: GantryConfig,
    current_poses: dict[str, Point3D],
    pipette_tip_extension: float = 0.0,
) -> list[ProtocolSemanticViolation]:
    violations: list[ProtocolSemanticViolation] = []
    instrument = args.get("instrument")
    position = args.get("position")
    relative_action = args.get("measurement_height")
    tip_ext = pipette_tip_extension if instrument == "pipette" else 0.0

    if relative_action is None:
        violations.append(_violation(
            step_index,
            "measure",
            "`measurement_height` is required on `measure` (labware-relative "
            "offset, mm above the resolved coordinate's surface Z).",
        ))
        return violations

    if instrument not in instrumented_gantry.instruments:
        violations.append(_violation(
            step_index,
            "measure",
            f"unknown instrument {instrument!r}. Available: "
            f"{', '.join(sorted(instrumented_gantry.instruments.keys()))}.",
        ))
        return violations

    finite_violation = _finite_field_violation(
        step_index, "measure", "measurement_height", relative_action,
    )
    if finite_violation is not None:
        violations.append(finite_violation)
        return violations

    try:
        coord = deck.resolve_coordinate(position)
    except (KeyError, AttributeError, ValueError) as exc:
        violations.append(_violation(
            step_index,
            "measure",
            f"position {position!r} cannot be resolved on the deck: {exc}",
        ))
        return violations

    action_abs = coord.z + relative_action
    action = (coord.x, coord.y, action_abs)
    safe_z = _resolved_safe_z(gantry)
    if safe_z is not None:
        safe_pose = (coord.x, coord.y, safe_z)
        violations.extend(_validate_gantry_waypoint(
            step_index=step_index,
            command_name="measure",
            gantry=gantry,
            label=f"measure {position!r} safe_z",
            instrument=instrument,
            instrumented_gantry=instrumented_gantry,
            x=safe_pose[0],
            y=safe_pose[1],
            z=safe_pose[2],
            tip_extension=tip_ext,
        ))
        violations.extend(_validate_known_transit(
            step_index=step_index,
            command_name="measure",
            gantry=gantry,
            label=f"measure {position!r} safe_z",
            instrument=instrument,
            current=current_poses.get(instrument),
            target=safe_pose,
            travel_z=safe_z,
        ))
        violations.extend(_validate_machine_structure_segment(
            step_index=step_index,
            command_name="measure",
            gantry=gantry,
            label=f"measure {position!r} action_z descend",
            instrument=instrument,
            start=safe_pose,
            end=action,
        ))

    violations.extend(_validate_gantry_waypoint(
        step_index=step_index,
        command_name="measure",
        gantry=gantry,
        label=f"measure {position!r} action_z",
        instrument=instrument,
        instrumented_gantry=instrumented_gantry,
        x=coord.x,
        y=coord.y,
        z=action_abs,
        tip_extension=tip_ext,
    ))
    violations.extend(_validate_below_safe_z(
        step_index=step_index,
        command_name="measure",
        label=f"measure {position!r} action_z",
        z=action_abs,
        gantry=gantry,
    ))
    current_poses[instrument] = action

    try:
        normalized = normalize_scan_arguments(
            method_kwargs=args.get("method_kwargs"),
        )
    except ValueError as exc:
        violations.append(_violation(step_index, "measure", str(exc)))
        return violations

    violations.extend(_validate_asmi_indentation(
        step_index=step_index,
        args=args,
        ref_z=coord.z,
        relative_action=relative_action,
        normalized=normalized,
        instrumented_gantry=instrumented_gantry,
        gantry=gantry,
    ))
    violations.extend(_indentation_limit_frame_violations(
        step_index=step_index,
        command_name="measure",
        indentation_limit_height=args.get("indentation_limit_height"),
        relative_action=relative_action,
        method_kwargs=normalized.method_kwargs,
    ))
    return violations


def _validate_rinse_command(
    *,
    step_index: int,
    args: dict[str, Any],
    instrumented_gantry: InstrumentedGantry,
    deck: Deck,
    gantry: GantryConfig,
    current_poses: dict[str, Point3D],
) -> list[ProtocolSemanticViolation]:
    violations: list[ProtocolSemanticViolation] = []
    command = "rinse"
    instrument = args.get("instrument")
    vial = args.get("vial")
    height = args.get("measurement_height")
    instr = instrumented_gantry.instruments.get(instrument)
    if instr is None:
        return [_violation(step_index, command, f"unknown instrument {instrument!r}.")]
    if not isinstance(instr, PotentiostatInstrument):
        violations.append(_violation(
            step_index, command,
            f"instrument {instrument!r} must be a PotentiostatInstrument.",
        ))
    finite = _finite_field_violation(step_index, command, "measurement_height", height)
    if finite is not None:
        return violations + [finite]
    if height >= 0:
        violations.append(_violation(
            step_index, command,
            f"measurement_height ({height}) must be negative (below the vial rim).",
        ))
    try:
        target = deck.resolve_labware(vial)
        coord = deck.resolve_coordinate(vial)
    except (KeyError, AttributeError, ValueError) as exc:
        return violations + [_violation(
            step_index, command, f"vial {vial!r} cannot be resolved on the deck: {exc}",
        )]
    if not isinstance(target, Vial):
        violations.append(_violation(
            step_index, command,
            f"target {vial!r} must resolve to a single Vial.",
        ))
        return violations
    safe_z = _resolved_safe_z(gantry)
    action_z = coord.z + float(height)
    if safe_z <= coord.z:
        violations.append(_violation(
            step_index, command,
            f"safe_z ({safe_z}) must be above vial rim Z ({coord.z}) for retraction.",
        ))
    if action_z > safe_z:
        violations.append(_violation(
            step_index, command,
            f"resolved rinse action Z ({action_z:.3f}) is above safe_z ({safe_z}).",
        ))
    safe_pose = (coord.x, coord.y, safe_z)
    action_pose = (coord.x, coord.y, action_z)
    violations.extend(_validate_gantry_waypoint(
        step_index=step_index, command_name=command, gantry=gantry,
        label=f"rinse {vial!r} safe_z", instrument=instrument,
        instrumented_gantry=instrumented_gantry, x=coord.x, y=coord.y, z=safe_z,
    ))
    violations.extend(_validate_gantry_waypoint(
        step_index=step_index, command_name=command, gantry=gantry,
        label=f"rinse {vial!r} action_z", instrument=instrument,
        instrumented_gantry=instrumented_gantry, x=coord.x, y=coord.y, z=action_z,
    ))
    violations.extend(_validate_machine_structure_segment(
        step_index=step_index, command_name=command, gantry=gantry,
        label=f"rinse {vial!r} immersion", instrument=instrument,
        start=safe_pose, end=action_pose,
    ))
    violations.extend(_validate_known_transit(
        step_index=step_index,
        command_name=command,
        gantry=gantry,
        label=f"rinse {vial!r} transit",
        instrument=instrument,
        current=current_poses.get(instrument),
        target=safe_pose,
        travel_z=safe_z,
    ))
    current_poses[instrument] = safe_pose
    return violations


def _validate_move_waypoints(
    *,
    step_index: int,
    args: dict[str, Any],
    protocol: Protocol,
    instrumented_gantry: InstrumentedGantry,
    deck: Deck,
    gantry: GantryConfig,
    current_poses: dict[str, Point3D],
    pipette_tip_extension: float = 0.0,
) -> list[ProtocolSemanticViolation]:
    violations: list[ProtocolSemanticViolation] = []
    instrument = args.get("instrument")
    position = args.get("position")
    travel_z = args.get("travel_z")
    if instrument not in instrumented_gantry.instruments:
        return violations
    tip_ext = pipette_tip_extension if instrument == "pipette" else 0.0

    target: Point3D | None = None
    target_label = f"move target {position!r}"
    transit_z = travel_z
    if isinstance(position, (list, tuple)):
        if len(position) != 3 or any(
            _finite_field_violation(step_index, "move", "position", coord)
            is not None
            for coord in position
        ):
            violations.append(_violation(
                step_index,
                "move",
                f"literal position {position!r} must be exactly three finite "
                "XYZ numbers.",
            ))
            return violations
        target = (position[0], position[1], position[2])
    elif isinstance(position, str) and position in protocol.positions:
        named = protocol.positions[position]
        if (
            not isinstance(named, (list, tuple))
            or len(named) != 3
            or any(
                _finite_field_violation(step_index, "move", "position", coord)
                is not None
                for coord in named
            )
        ):
            violations.append(_violation(
                step_index,
                "move",
                f"named position {position!r} must be exactly three finite "
                f"XYZ numbers, got {named!r}.",
            ))
            return violations
        target = (named[0], named[1], named[2])
    elif isinstance(position, str):
        try:
            coord = deck.resolve_coordinate(position)
        except (KeyError, AttributeError, ValueError) as exc:
            violations.append(_violation(
                step_index,
                "move",
                f"position {position!r} cannot be resolved on the deck: {exc}",
            ))
            return violations
        if travel_z is not None:
            violations.append(_violation(
                step_index,
                "move",
                "travel_z is only supported for literal/named XYZ targets, "
                f"not deck target {position!r}.",
            ))
            return violations
        safe_z = _resolved_safe_z(gantry)
        if safe_z is None:
            violations.append(_violation(
                step_index,
                "move",
                f"deck-target move to {position!r} requires gantry `safe_z` "
                "to be configured.",
            ))
            return violations
        target = (coord.x, coord.y, safe_z)
        target_label = f"move safe_z for {position!r}"
        transit_z = safe_z

    if target is None:
        return violations

    x, y, z = target
    violations.extend(_validate_gantry_waypoint(
        step_index=step_index,
        command_name="move",
        gantry=gantry,
        label=target_label,
        instrument=instrument,
        instrumented_gantry=instrumented_gantry,
        x=x,
        y=y,
        z=z,
        tip_extension=tip_ext,
    ))
    if travel_z is not None:
        violations.extend(_validate_gantry_waypoint(
            step_index=step_index,
            command_name="move",
            gantry=gantry,
            label=f"move travel_z for {position!r}",
            instrument=instrument,
            instrumented_gantry=instrumented_gantry,
            x=x,
            y=y,
            z=travel_z,
            tip_extension=tip_ext,
        ))
        violations.extend(_validate_below_safe_z(
            step_index=step_index,
            command_name="move",
            label=f"move travel_z for {position!r}",
            z=travel_z,
            gantry=gantry,
        ))
    if transit_z is not None:
        violations.extend(_validate_known_transit(
            step_index=step_index,
            command_name="move",
            gantry=gantry,
            label=target_label,
            instrument=instrument,
            current=current_poses.get(instrument),
            target=target,
            travel_z=transit_z,
        ))
    current_poses[instrument] = target
    return violations


def _validate_tip_pickup_metadata(
    *,
    step_index: int,
    position: Any,
    deck: Deck,
) -> tuple[list[ProtocolSemanticViolation], float | None]:
    violations: list[ProtocolSemanticViolation] = []
    try:
        rack, tip_id = resolve_tip_rack_slot(deck, position)
    except TipRackResolutionError as exc:
        return [
            _violation(
                step_index,
                "pick_up_tip",
                f"pick_up_tip position {position!r} must target a TipRack slot ({exc}).",
            )
        ], None

    if tip_id is None:
        return [
            _violation(
                step_index,
                "pick_up_tip",
                f"pick_up_tip position {position!r} must include an explicit "
                "tip slot such as `tips.A1`.",
            )
        ], None
    if tip_id not in rack.tips:
        violations.append(_violation(
            step_index,
            "pick_up_tip",
            f"pick_up_tip target {position!r} is not a known tip slot.",
        ))
    elif not rack.is_tip_present(tip_id):
        violations.append(_violation(
            step_index,
            "pick_up_tip",
            f"pick_up_tip target {position!r} is not available.",
        ))

    if (
        isinstance(rack.tip_length, bool)
        or not isinstance(rack.tip_length, (int, float))
        or not math.isfinite(float(rack.tip_length))
        or float(rack.tip_length) <= 0.0
    ):
        violations.append(_violation(
            step_index,
            "pick_up_tip",
            f"TipRack {rack.name!r} must have positive `tip_length` for "
            "attached-tip collision validation.",
        ))
        return violations, None

    return violations, float(rack.tip_length)


def _require_attached_tip(
    *,
    step_index: int,
    command_name: str,
    tip_state: PipetteTipState,
) -> list[ProtocolSemanticViolation]:
    if tip_state.has_tip:
        return []
    return [_violation(
        step_index,
        command_name,
        f"{command_name} requires an attached pipette tip. Add `pick_up_tip` "
        "before this step.",
    )]


_PIPETTE_COMMANDS = frozenset({
    "aspirate",
    "blowout",
    "clear_well",
    "drop_tip",
    "flush_pipette",
    "mix",
    "pick_up_tip",
    "purge_pipette",
    "rinse_well",
    "transfer",
    "serial_transfer",
})
_NO_MOTION_COMMANDS = frozenset({"pause", "breakpoint"})


# -- Feature 05: compound-command container resolution (structural only) --
#
# Automatic selection (`solution=`/role-based, see
# `cubos.protocol_engine.commands._liquid_selection`) picks its concrete
# container from *tracked volumes*, which this static pass has no access to.
# What IS checkable here, with only the deck definition (no initial-fluids
# seed required): whether the requested role/solution combination is even
# defined on the deck at all -- catching a typo'd solution name or a deck
# with no waste container long before a hardware run. Full volume-adequate
# selection (dead-volume reserve, waste headroom) is separately simulated
# offline when an initial-fluids seed is supplied -- see
# `cubos.validation.fluid_volumes`. Neither pass feeds the resolved
# automatic-selection container back into this module's XY/Z/structure
# bounds checks below (`_validate_pipette_engage`); only explicit
# source/waste/well positions get full motion-bounds coverage. This is a
# deliberate, documented scope gap (see docs/protocol-yaml.md).


def _iter_role_vials(deck: Deck, role: str):
    for labware in deck.volume_labware.values():
        if isinstance(labware, Vial):
            if labware.role == role:
                yield labware
        elif isinstance(labware, VialGrid):
            for vial in labware.vials.values():
                if vial.role == role:
                    yield vial


def _static_stock_candidate_exists(deck: Deck, solution: str) -> bool:
    return any(
        vial.solution == solution for vial in _iter_role_vials(deck, STOCK)
    )


def _static_waste_candidate_exists(deck: Deck, solution: Any) -> bool:
    for vial in _iter_role_vials(deck, WASTE):
        if vial.allowed_solutions is None:
            return True
        if solution is not None and solution in vial.allowed_solutions:
            return True
    return False


def _validate_stock_or_solution(
    *,
    step_index: int,
    command_name: str,
    label: str,
    deck: Deck,
    source: Any,
    solution: Any,
) -> list[ProtocolSemanticViolation]:
    if (source is None) == (solution is None):
        return [_violation(
            step_index,
            command_name,
            f"{label}: provide exactly one of an explicit source or "
            "`solution=` for automatic stock selection.",
        )]
    if (
        solution is not None
        and isinstance(solution, str)
        and not _static_stock_candidate_exists(deck, solution)
    ):
        return [_violation(
            step_index,
            command_name,
            f"{label}: no role={STOCK!r} container with solution={solution!r} "
            "is defined on the deck.",
        )]
    return []


def _validate_waste_or_solution(
    *,
    step_index: int,
    command_name: str,
    label: str,
    deck: Deck,
    waste: Any,
    solution: Any,
) -> list[ProtocolSemanticViolation]:
    if waste is not None:
        return []
    if not _static_waste_candidate_exists(deck, solution):
        detail = f" accepting solution={solution!r}" if solution else ""
        return [_violation(
            step_index,
            command_name,
            f"{label}: no role={WASTE!r} container{detail} is defined on "
            "the deck.",
        )]
    return []


def _validate_optional_engage(
    *,
    step_index: int,
    command_name: str,
    label: str,
    position: Any,
    args: dict[str, Any],
    height_field_name: str,
    tip_extension: float,
    instrumented_gantry: InstrumentedGantry,
    deck: Deck,
    gantry: GantryConfig,
    current_poses: dict[str, Point3D],
) -> list[ProtocolSemanticViolation]:
    """Engage-validate *position* only when it is an explicit deck target.

    Automatic-selection args (``position is None``) have no single resolved
    position to bounds-check statically -- see the scope note above.
    """
    if position is None:
        return []
    violations, _ = _validate_pipette_engage(
        step_index=step_index,
        command_name=command_name,
        label=label,
        position=position,
        height=_height_value(args, height_field_name),
        height_field_name=height_field_name,
        tip_extension=tip_extension,
        instrumented_gantry=instrumented_gantry,
        deck=deck,
        gantry=gantry,
        current_poses=current_poses,
    )
    return violations


def _known_command_names() -> frozenset[str]:
    return frozenset(CommandRegistry.instance().command_names)


def _validate_pipette_command(
    *,
    step_index: int,
    command_name: str,
    args: dict[str, Any],
    instrumented_gantry: InstrumentedGantry,
    deck: Deck,
    gantry: GantryConfig,
    current_poses: dict[str, Point3D],
    tip_state: PipetteTipState,
) -> tuple[list[ProtocolSemanticViolation], PipetteTipState]:
    if command_name in _NO_MOTION_COMMANDS:
        return [], tip_state
    known_commands = _known_command_names()
    if command_name not in known_commands:
        return [
            _violation(
                step_index,
                command_name,
                f"unknown protocol command {command_name!r}; the semantic "
                f"validator only knows: {', '.join(sorted(known_commands))}.",
            )
        ], tip_state
    if command_name not in _PIPETTE_COMMANDS:
        return [], tip_state
    if "pipette" not in instrumented_gantry.instruments:
        return [], tip_state

    violations: list[ProtocolSemanticViolation] = []
    if command_name == "pick_up_tip":
        if tip_state.has_tip:
            violations.append(_violation(
                step_index,
                command_name,
                "pick_up_tip cannot run because the pipette already has an "
                "attached pipette tip. Drop the current tip first.",
            ))
        try:
            rack, tip_id = resolve_tip_rack_slot(deck, args.get("position"))
        except TipRackResolutionError:
            rack, tip_id = None, None
        if rack is not None and rack.side_exit is not None and tip_id in rack.tips:
            metadata_violations, extension = _validate_tip_pickup_metadata(
                step_index=step_index, position=args.get("position"), deck=deck,
            )
            violations.extend(metadata_violations)
            path = rack.pickup_path(tip_id)
            previous = current_poses.get("pipette")
            for phase, target, depth in path:
                violations.extend(_validate_gantry_waypoint(
                    step_index=step_index, command_name=command_name,
                    gantry=gantry, label=f"side-exit {phase}", instrument="pipette",
                    instrumented_gantry=instrumented_gantry,
                    x=target[0], y=target[1], z=target[2], tip_extension=depth,
                ))
                if phase == "approach":
                    violations.extend(_validate_known_transit(
                        step_index=step_index, command_name=command_name,
                        gantry=gantry, label="side-exit approach", instrument="pipette",
                        current=previous, target=target, travel_z=target[2],
                    ))
                elif previous is not None:
                    if phase == "lift":
                        previous = (previous[0], previous[1], previous[2] - rack.tip_length)
                    violations.extend(_validate_machine_structure_segment(
                        step_index=step_index, command_name=command_name,
                        gantry=gantry, label=f"side-exit {phase}", instrument="pipette",
                        start=previous, end=target,
                    ))
                previous = target
            if extension is not None:
                tip_state = tip_state.attach(extension)
                current_poses["pipette"] = path[-1][1]
            return violations, tip_state
        engage_violations, bare_pose = _validate_pipette_engage(
            step_index=step_index,
            command_name=command_name,
            label=f"pick_up_tip {args.get('position')!r}",
            position=args.get("position"),
            height=0.0,
            height_field_name="height",
            tip_extension=tip_state.tip_extension,
            instrumented_gantry=instrumented_gantry,
            deck=deck,
            gantry=gantry,
            current_poses=current_poses,
        )
        violations.extend(engage_violations)
        metadata_violations, tip_extension = _validate_tip_pickup_metadata(
            step_index=step_index,
            position=args.get("position"),
            deck=deck,
        )
        violations.extend(metadata_violations)
        # Advance tip state on metadata success even if engage emitted
        # unrelated violations (XY bounds, structure hits) — otherwise every
        # downstream step cascades a noisy "requires an attached pipette tip"
        # error that drowns out the real root cause.
        if tip_extension is not None and bare_pose is not None:
            tip_state = tip_state.attach(tip_extension)
            current_poses["pipette"] = (
                bare_pose[0],
                bare_pose[1],
                bare_pose[2] - tip_extension,
            )
        return violations, tip_state

    if command_name == "drop_tip":
        had_tip = tip_state.has_tip
        violations.extend(_require_attached_tip(
            step_index=step_index,
            command_name=command_name,
            tip_state=tip_state,
        ))
        previous_extension = tip_state.tip_extension
        engage_violations, tip_pose = _validate_pipette_engage(
            step_index=step_index,
            command_name=command_name,
            label=f"drop_tip {args.get('position')!r}",
            position=args.get("position"),
            height=0.0,
            height_field_name="height",
            tip_extension=previous_extension,
            instrumented_gantry=instrumented_gantry,
            deck=deck,
            gantry=gantry,
            current_poses=current_poses,
        )
        violations.extend(engage_violations)
        # Like pick_up_tip above, advance state independently of engage
        # violations so a single bad drop target doesn't mask all downstream
        # tip-state errors.
        if had_tip and tip_pose is not None:
            tip_state = tip_state.detach()
            current_poses["pipette"] = (
                tip_pose[0],
                tip_pose[1],
                tip_pose[2] + previous_extension,
            )
        return violations, tip_state

    if command_name in {"aspirate", "blowout", "mix"}:
        violations.extend(_require_attached_tip(
            step_index=step_index,
            command_name=command_name,
            tip_state=tip_state,
        ))
        engage_violations, _ = _validate_pipette_engage(
            step_index=step_index,
            command_name=command_name,
            label=f"{command_name} {args.get('position')!r}",
            position=args.get("position"),
            height=_height_value(args, "height"),
            height_field_name="height",
            tip_extension=tip_state.tip_extension,
            instrumented_gantry=instrumented_gantry,
            deck=deck,
            gantry=gantry,
            current_poses=current_poses,
        )
        violations.extend(engage_violations)
        return violations, tip_state

    if command_name == "transfer":
        violations.extend(_require_attached_tip(
            step_index=step_index,
            command_name=command_name,
            tip_state=tip_state,
        ))
        for label, position_key, height_key in (
            ("transfer.aspirate", "source", "source_height"),
            ("transfer.dispense", "destination", "destination_height"),
        ):
            engage_violations, _ = _validate_pipette_engage(
                step_index=step_index,
                command_name=command_name,
                label=f"{label} {args.get(position_key)!r}",
                position=args.get(position_key),
                height=_height_value(args, height_key),
                height_field_name=height_key,
                tip_extension=tip_state.tip_extension,
                instrumented_gantry=instrumented_gantry,
                deck=deck,
                gantry=gantry,
                current_poses=current_poses,
            )
            violations.extend(engage_violations)
        return violations, tip_state

    if command_name == "serial_transfer":
        violations.extend(_require_attached_tip(
            step_index=step_index,
            command_name=command_name,
            tip_state=tip_state,
        ))
        engage_violations, _ = _validate_pipette_engage(
            step_index=step_index,
            command_name=command_name,
            label=f"serial_transfer source {args.get('source')!r}",
            position=args.get("source"),
            height=_height_value(args, "source_height"),
            height_field_name="source_height",
            tip_extension=tip_state.tip_extension,
            instrumented_gantry=instrumented_gantry,
            deck=deck,
            gantry=gantry,
            current_poses=current_poses,
        )
        violations.extend(engage_violations)

        plate_name = args.get("plate")
        try:
            plate_obj = deck.resolve_labware(plate_name)
        except KeyError:
            return violations, tip_state
        if not isinstance(plate_obj, WellPlate) or not isinstance(plate_name, str):
            return violations, tip_state
        for well_id in _wells_for_axis(plate_obj, args.get("axis")):
            engage_violations, _ = _validate_pipette_engage(
                step_index=step_index,
                command_name=command_name,
                label=f"serial_transfer destination {plate_name}.{well_id}",
                position=f"{plate_name}.{well_id}",
                height=_height_value(args, "destination_height"),
                height_field_name="destination_height",
                tip_extension=tip_state.tip_extension,
                instrumented_gantry=instrumented_gantry,
                deck=deck,
                gantry=gantry,
                current_poses=current_poses,
            )
            violations.extend(engage_violations)
        return violations, tip_state

    if command_name == "rinse_well":
        violations.extend(_require_attached_tip(
            step_index=step_index, command_name=command_name, tip_state=tip_state,
        ))
        well = args.get("well")
        violations.extend(_validate_optional_engage(
            step_index=step_index, command_name=command_name,
            label=f"rinse_well well {well!r}", position=well, args=args,
            height_field_name="well_height",
            tip_extension=tip_state.tip_extension,
            instrumented_gantry=instrumented_gantry, deck=deck, gantry=gantry,
            current_poses=current_poses,
        ))
        source = args.get("source")
        solution = args.get("solution")
        violations.extend(_validate_stock_or_solution(
            step_index=step_index, command_name=command_name,
            label="rinse_well source", deck=deck, source=source, solution=solution,
        ))
        violations.extend(_validate_optional_engage(
            step_index=step_index, command_name=command_name,
            label=f"rinse_well source {source!r}", position=source, args=args,
            height_field_name="source_height",
            tip_extension=tip_state.tip_extension,
            instrumented_gantry=instrumented_gantry, deck=deck, gantry=gantry,
            current_poses=current_poses,
        ))
        waste = args.get("waste")
        violations.extend(_validate_waste_or_solution(
            step_index=step_index, command_name=command_name,
            label="rinse_well waste", deck=deck, waste=waste, solution=solution,
        ))
        violations.extend(_validate_optional_engage(
            step_index=step_index, command_name=command_name,
            label=f"rinse_well waste {waste!r}", position=waste, args=args,
            height_field_name="waste_height",
            tip_extension=tip_state.tip_extension,
            instrumented_gantry=instrumented_gantry, deck=deck, gantry=gantry,
            current_poses=current_poses,
        ))
        return violations, tip_state

    if command_name == "flush_pipette":
        violations.extend(_require_attached_tip(
            step_index=step_index, command_name=command_name, tip_state=tip_state,
        ))
        source = args.get("source")
        solution = args.get("solution")
        violations.extend(_validate_stock_or_solution(
            step_index=step_index, command_name=command_name,
            label="flush_pipette source", deck=deck, source=source, solution=solution,
        ))
        violations.extend(_validate_optional_engage(
            step_index=step_index, command_name=command_name,
            label=f"flush_pipette source {source!r}", position=source, args=args,
            height_field_name="source_height",
            tip_extension=tip_state.tip_extension,
            instrumented_gantry=instrumented_gantry, deck=deck, gantry=gantry,
            current_poses=current_poses,
        ))
        waste = args.get("waste")
        violations.extend(_validate_waste_or_solution(
            step_index=step_index, command_name=command_name,
            label="flush_pipette waste", deck=deck, waste=waste, solution=solution,
        ))
        violations.extend(_validate_optional_engage(
            step_index=step_index, command_name=command_name,
            label=f"flush_pipette waste {waste!r}", position=waste, args=args,
            height_field_name="waste_height",
            tip_extension=tip_state.tip_extension,
            instrumented_gantry=instrumented_gantry, deck=deck, gantry=gantry,
            current_poses=current_poses,
        ))
        return violations, tip_state

    if command_name == "purge_pipette":
        violations.extend(_require_attached_tip(
            step_index=step_index, command_name=command_name, tip_state=tip_state,
        ))
        source = args.get("source")
        solution = args.get("solution")
        violations.extend(_validate_stock_or_solution(
            step_index=step_index, command_name=command_name,
            label="purge_pipette source", deck=deck, source=source, solution=solution,
        ))
        violations.extend(_validate_optional_engage(
            step_index=step_index, command_name=command_name,
            label=f"purge_pipette source {source!r}", position=source, args=args,
            height_field_name="source_height",
            tip_extension=tip_state.tip_extension,
            instrumented_gantry=instrumented_gantry, deck=deck, gantry=gantry,
            current_poses=current_poses,
        ))
        waste = args.get("waste")
        violations.extend(_validate_waste_or_solution(
            step_index=step_index, command_name=command_name,
            label="purge_pipette waste", deck=deck, waste=waste, solution=solution,
        ))
        violations.extend(_validate_optional_engage(
            step_index=step_index, command_name=command_name,
            label=f"purge_pipette waste {waste!r}", position=waste, args=args,
            height_field_name="waste_height",
            tip_extension=tip_state.tip_extension,
            instrumented_gantry=instrumented_gantry, deck=deck, gantry=gantry,
            current_poses=current_poses,
        ))
        return violations, tip_state

    if command_name == "clear_well":
        violations.extend(_require_attached_tip(
            step_index=step_index, command_name=command_name, tip_state=tip_state,
        ))
        well = args.get("well")
        engage_violations, _ = _validate_pipette_engage(
            step_index=step_index, command_name=command_name,
            label=f"clear_well well {well!r}", position=well,
            height=_height_value(args, "well_height"),
            height_field_name="well_height",
            tip_extension=tip_state.tip_extension,
            instrumented_gantry=instrumented_gantry, deck=deck, gantry=gantry,
            current_poses=current_poses,
        )
        violations.extend(engage_violations)
        waste = args.get("waste")
        solution = args.get("solution")
        violations.extend(_validate_waste_or_solution(
            step_index=step_index, command_name=command_name,
            label="clear_well waste", deck=deck, waste=waste, solution=solution,
        ))
        violations.extend(_validate_optional_engage(
            step_index=step_index, command_name=command_name,
            label=f"clear_well waste {waste!r}", position=waste, args=args,
            height_field_name="waste_height",
            tip_extension=tip_state.tip_extension,
            instrumented_gantry=instrumented_gantry, deck=deck, gantry=gantry,
            current_poses=current_poses,
        ))
        return violations, tip_state

    return violations, tip_state


def _validate_asmi_indentation(
    *,
    step_index: int,
    args: dict[str, Any],
    ref_z: float,
    relative_action: float,
    normalized: NormalizedScanArguments,
    instrumented_gantry: InstrumentedGantry,
    gantry: GantryConfig,
) -> list[ProtocolSemanticViolation]:
    """Bounds-check ASMI indentation against the working volume.

    ``indentation_limit_height`` is a *signed* labware-relative offset
    (mm above the well's calibrated surface Z; negative = below). The
    deepest absolute Z reached during the descent is
    ``ref_z + indentation_limit_height``.
    """
    # Match by *type* (not by the user-chosen instrument key) so a force
    # sensor named e.g. ``force_sensor`` or ``asmi_main`` still goes
    # through the depth-bound check. The deepest-Z bound is the only thing
    # protecting against driving the gantry through the deck.
    from cubos.instruments.asmi.interface import ASMIInstrument

    violations: list[ProtocolSemanticViolation] = []
    instrument = args.get("instrument")
    if (
        instrument not in instrumented_gantry.instruments
        or not isinstance(instrumented_gantry.instruments[instrument], ASMIInstrument)
        or args.get("method") != "indentation"
    ):
        return violations

    indentation_limit_height = args.get("indentation_limit_height")
    step_size = normalized.method_kwargs.get("step_size")

    if step_size is not None and step_size <= 0:
        violations.append(_violation(
            step_index,
            "scan",
            f"ASMI step_size must be positive, got {step_size}.",
        ))

    detect_surface = surface_detection_enabled(normalized.method_kwargs)
    surface_kwargs_ok = True
    if detect_surface:
        for field in (
            "surface_search_step",
            "surface_force_threshold",
            "surface_search_max_travel",
        ):
            value = normalized.method_kwargs.get(field)
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                surface_kwargs_ok = False
                violations.append(_violation(
                    step_index,
                    "scan",
                    f"ASMI {field} must be a positive number, got {value!r}.",
                ))

    if indentation_limit_height is None:
        return violations
    finite_violation = _finite_field_violation(
        step_index, "scan", "indentation_limit_height", indentation_limit_height,
    )
    if finite_violation is not None:
        violations.append(finite_violation)
        return violations

    if detect_surface:
        # The surface's Z is unknown statically; bound the worst case:
        # the search may descend `surface_search_max_travel` below the
        # measurement plane, and the indentation then continues
        # `indentation_limit_height` below the surface found there.
        if not surface_kwargs_ok:
            return violations
        from cubos.instruments.asmi.interface import (
            DEFAULT_SURFACE_SEARCH_MAX_TRAVEL_MM,
        )
        max_travel = normalized.method_kwargs.get(
            "surface_search_max_travel", DEFAULT_SURFACE_SEARCH_MAX_TRAVEL_MM,
        )
        deepest_abs = (
            ref_z + relative_action - max_travel
            + min(float(indentation_limit_height), 0.0)
        )
        limit_hint = (
            "Raise `measurement_height`, lower `surface_search_max_travel`, "
            "raise `indentation_limit_height`, raise the labware, or adjust "
            "z_min."
        )
    else:
        deepest_abs = ref_z + indentation_limit_height
        limit_hint = (
            "Raise `indentation_limit_height`, raise the labware, or adjust "
            "z_min."
        )
    if deepest_abs < gantry.working_volume.z_min:
        violations.append(_violation(
            step_index,
            "scan",
            f"ASMI indentation deepest absolute Z ({deepest_abs:.3f}) is "
            f"below working_volume.z_min ({gantry.working_volume.z_min}). "
            + limit_hint,
        ))
    return violations


def _validate_instrument_reference(
    *,
    step_index: int,
    command_name: str,
    instrument: Any,
    field_name: str,
    instrumented_gantry: InstrumentedGantry,
) -> list[ProtocolSemanticViolation]:
    """Check that an instrument-name argument names a mounted instrument."""
    if instrument not in instrumented_gantry.instruments:
        return [_violation(
            step_index,
            command_name,
            f"unknown {field_name} {instrument!r}. Available: "
            f"{', '.join(sorted(instrumented_gantry.instruments.keys()))}.",
        )]
    return []


def _validate_cure_command(
    *,
    step_index: int,
    args: dict[str, Any],
    instrumented_gantry: InstrumentedGantry,
    deck: Deck,
) -> list[ProtocolSemanticViolation]:
    """Check ``cure``'s ``instrument``/``position`` resolve.

    ``cure`` is a thin wrapper over ``measure``'s travel machinery but is
    dispatched separately here (rather than through ``_validate_measure_command``)
    because its instrument is a UV-curing device, not a sensor -- the ASMI
    indentation/bounds machinery ``_validate_measure_command`` also runs
    does not apply and would mislabel violations under a ``measure`` command
    name.
    """
    instrument_violations = _validate_instrument_reference(
        step_index=step_index,
        command_name="cure",
        instrument=args.get("instrument"),
        field_name="instrument",
        instrumented_gantry=instrumented_gantry,
    )
    if instrument_violations:
        return instrument_violations
    position = args.get("position")
    try:
        deck.resolve_coordinate(position)
    except (KeyError, AttributeError, ValueError) as exc:
        return [_violation(
            step_index,
            "cure",
            f"position {position!r} cannot be resolved on the deck: {exc}",
        )]
    return []


def _validate_cap_decap_command(
    *,
    step_index: int,
    command_name: str,
    args: dict[str, Any],
    instrumented_gantry: InstrumentedGantry,
    deck: Deck,
) -> list[ProtocolSemanticViolation]:
    """Check ``cap``/``decap``'s ``instrument``/``vial`` resolve."""
    instrument_violations = _validate_instrument_reference(
        step_index=step_index,
        command_name=command_name,
        instrument=args.get("instrument"),
        field_name="instrument",
        instrumented_gantry=instrumented_gantry,
    )
    if instrument_violations:
        return instrument_violations
    vial = args.get("vial")
    try:
        resolve_cap_target(deck, vial)
    except CapStateError as exc:
        return [_violation(
            step_index,
            command_name,
            f"vial {vial!r} cannot be resolved on the deck: {exc}",
        )]
    return []


def _validate_image_well_command(
    *,
    step_index: int,
    args: dict[str, Any],
    instrumented_gantry: InstrumentedGantry,
    deck: Deck,
) -> list[ProtocolSemanticViolation]:
    """Check ``image_well``'s ``camera``/``lights``/``well`` resolve."""
    violations = _validate_instrument_reference(
        step_index=step_index,
        command_name="image_well",
        instrument=args.get("camera"),
        field_name="camera",
        instrumented_gantry=instrumented_gantry,
    )
    lights = args.get("lights")
    if lights is not None:
        violations.extend(_validate_instrument_reference(
            step_index=step_index,
            command_name="image_well",
            instrument=lights,
            field_name="lights instrument",
            instrumented_gantry=instrumented_gantry,
        ))
    if violations:
        return violations
    well = args.get("well")
    try:
        deck.resolve_coordinate(well)
    except (KeyError, AttributeError, ValueError) as exc:
        return [_violation(
            step_index,
            "image_well",
            f"well {well!r} cannot be resolved on the deck: {exc}",
        )]
    return []


def validate_protocol_semantics(
    protocol: Protocol,
    instrumented_gantry: InstrumentedGantry,
    deck: Deck,
    gantry: GantryConfig,
) -> list[ProtocolSemanticViolation]:
    """Return protocol semantic violations that static bounds checks miss."""
    violations: list[ProtocolSemanticViolation] = []
    current_poses: dict[str, Point3D] = {}
    pipette_tip_state = PipetteTipState()
    consumed_side_tips: dict[str, set[str]] = {}
    for step in protocol.steps:
        if step.command_name == "pick_up_tip":
            position = step.args.get("position")
            try:
                rack, tip_id = resolve_tip_rack_slot(deck, position)
            except TipRackResolutionError:
                rack, tip_id = None, None
            if rack is not None and rack.side_exit is not None and tip_id in rack.tips:
                rack_key = position.rsplit(".", 1)[0]
                consumed = consumed_side_tips.setdefault(rack_key, set())
                blockers = rack.exit_blockers(tip_id, consumed)
                if blockers or tip_id in consumed:
                    violations.append(_violation(step.index, step.command_name,
                        f"Side-exit slot {position} is consumed or its exit lane is blocked by {blockers}."))
                consumed.add(tip_id)
        if step.command_name == "home":
            violations.extend(_validate_home_waypoints(
                step_index=step.index,
                instrumented_gantry=instrumented_gantry,
                gantry=gantry,
                current_poses=current_poses,
                instrument_tip_extensions={
                    "pipette": pipette_tip_state.tip_extension,
                } if pipette_tip_state.has_tip else None,
            ))
        elif step.command_name == "move":
            violations.extend(_validate_move_waypoints(
                step_index=step.index,
                args=step.args,
                protocol=protocol,
                instrumented_gantry=instrumented_gantry,
                deck=deck,
                gantry=gantry,
                current_poses=current_poses,
                pipette_tip_extension=pipette_tip_state.tip_extension,
            ))
        elif step.command_name == "measure":
            violations.extend(_validate_measure_command(
                step_index=step.index,
                args=step.args,
                instrumented_gantry=instrumented_gantry,
                deck=deck,
                gantry=gantry,
                current_poses=current_poses,
                pipette_tip_extension=pipette_tip_state.tip_extension,
            ))
        elif step.command_name == "rinse":
            violations.extend(_validate_rinse_command(
                step_index=step.index,
                args=step.args,
                instrumented_gantry=instrumented_gantry,
                deck=deck,
                gantry=gantry,
                current_poses=current_poses,
            ))
        elif step.command_name == "scan":
            violations.extend(_validate_scan_command(
                step_index=step.index,
                args=step.args,
                instrumented_gantry=instrumented_gantry,
                deck=deck,
                gantry=gantry,
                current_poses=current_poses,
                pipette_tip_extension=pipette_tip_state.tip_extension,
            ))
        elif step.command_name == "cure":
            violations.extend(_validate_cure_command(
                step_index=step.index,
                args=step.args,
                instrumented_gantry=instrumented_gantry,
                deck=deck,
            ))
        elif step.command_name in ("cap", "decap"):
            violations.extend(_validate_cap_decap_command(
                step_index=step.index,
                command_name=step.command_name,
                args=step.args,
                instrumented_gantry=instrumented_gantry,
                deck=deck,
            ))
        elif step.command_name == "image_well":
            violations.extend(_validate_image_well_command(
                step_index=step.index,
                args=step.args,
                instrumented_gantry=instrumented_gantry,
                deck=deck,
            ))
        else:
            pipette_violations, pipette_tip_state = _validate_pipette_command(
                step_index=step.index,
                command_name=step.command_name,
                args=step.args,
                instrumented_gantry=instrumented_gantry,
                deck=deck,
                gantry=gantry,
                current_poses=current_poses,
                tip_state=pipette_tip_state,
            )
            violations.extend(pipette_violations)
    return violations
