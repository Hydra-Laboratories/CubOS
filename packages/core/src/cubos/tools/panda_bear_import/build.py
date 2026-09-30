"""Build the four PANDA-BEAR -> CubOS output configs from a read snapshot.

Pure functions over the dataclasses from :mod:`db_reader` -- no file I/O, no
SQL, no PANDA table names. Callers (the CLI) own reading the snapshot,
loading resolutions, and writing files.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from typing import Mapping

from cubos.deck.loader import derive_wells_preview
from cubos.deck.yaml_schema import WellPlateYamlEntry

from .conflicts import Conflict
from .constants import (
    CAPPER_CAPTURE_RETRIES,
    CAPPER_CAPTURE_SETTLE_S,
    CAPPER_ENGAGE_DEPTH_MM,
    CAPPER_INSTRUMENT_KEY,
    ELECTRODE_FRAME_TOOL,
    ENVELOPE_TOLERANCE_MM,
    PIPETTE_FRAME_TOOL,
    PITCH_RESIDUAL_TOLERANCE_MM,
    VIAL_CATEGORY_ELECTRODE,
    VIAL_CATEGORY_ROLES,
    VIAL_DIAMETER_MM,
    VIAL_HEIGHT_MM,
)
from .conversion import (
    Point,
    ToolOffset,
    WorkingVolume,
    envelope_violation,
    pitch_offsets,
    round_mm,
    to_gantry,
)
from .db_reader import PandaBearSnapshot, TipRow, VialRow
from .resolutions import Resolutions

_LOCATION_ID_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


class ImportBuildError(RuntimeError):
    """Raised for a data shape the importer cannot safely handle at all.

    Distinct from ``Conflict``: these indicate malformed/irregular source
    data (e.g. a non-rectangular well grid) rather than an expected,
    resolvable ambiguity.
    """


@dataclass(frozen=True)
class ImportResult:
    deck: dict
    fluids: dict
    gantry_full: dict
    tour: dict
    conflicts: tuple[Conflict, ...]

    @property
    def blocking(self) -> tuple[Conflict, ...]:
        return tuple(c for c in self.conflicts if c.severity == "conflict" and not c.resolved)


def build_import(
    snapshot: PandaBearSnapshot,
    resolutions: Resolutions,
    tool_offsets: Mapping[str, ToolOffset],
    working_volume: WorkingVolume,
    gantry_raw: dict,
) -> ImportResult:
    conflicts: list[Conflict] = []

    vial_labware, fluids, vial_conflicts = _build_vials(
        snapshot.vials, resolutions, tool_offsets, working_volume,
    )
    conflicts.extend(vial_conflicts)

    plate_dict, plate_conflicts = _build_wellplate(
        snapshot, resolutions, tool_offsets[PIPETTE_FRAME_TOOL], working_volume,
    )
    conflicts.extend(plate_conflicts)

    (tiprack_dict, disposal_dict), tiprack_conflicts = _build_tiprack(
        snapshot, resolutions, tool_offsets[PIPETTE_FRAME_TOOL], working_volume,
    )
    conflicts.extend(tiprack_conflicts)

    labware: dict = {}
    labware.update(vial_labware)
    if plate_dict:
        labware["ito_pama_plate"] = plate_dict
    if tiprack_dict:
        labware["tip_rack"] = tiprack_dict
    if disposal_dict:
        labware["tip_disposal"] = disposal_dict

    deck_dict = {"labware": labware}
    fluids_dict = {"fluids": fluids}
    gantry_full_dict = _build_gantry_full(gantry_raw, tool_offsets[PIPETTE_FRAME_TOOL])
    tour_dict = _build_tour(labware)

    return ImportResult(
        deck=deck_dict,
        fluids=fluids_dict,
        gantry_full=gantry_full_dict,
        tour=tour_dict,
        conflicts=tuple(conflicts),
    )


# --------------------------------------------------------------------------
# Vials
# --------------------------------------------------------------------------


def _build_vials(
    vials: tuple[VialRow, ...],
    resolutions: Resolutions,
    tool_offsets: Mapping[str, ToolOffset],
    working_volume: WorkingVolume,
) -> tuple[dict, dict, list[Conflict]]:
    conflicts: list[Conflict] = []
    included, selection_conflicts = _select_vials(vials, resolutions)
    conflicts.extend(selection_conflicts)

    labware: dict = {}
    fluids: dict = {}
    for row in sorted(included, key=lambda r: r.position):
        tool = ELECTRODE_FRAME_TOOL if row.category == VIAL_CATEGORY_ELECTRODE else PIPETTE_FRAME_TOOL
        offset = tool_offsets[tool]
        top_point = _convert(row.x, row.y, row.top, offset)

        env_msg = envelope_violation(top_point, working_volume, ENVELOPE_TOLERANCE_MM)
        if env_msg:
            conflicts.append(Conflict(
                category="position_out_of_envelope",
                severity="warning",
                row_ids=(row.id,),
                message=(
                    f"vial {row.position!r} (id {row.id}) gantry position out of "
                    f"working volume: {env_msg}"
                ),
                resolved=True,
                resolution="reported only (positional; not blocking per project scope)",
            ))

        labware[row.position] = {
            "type": "vial",
            "name": row.position,
            "height": VIAL_HEIGHT_MM,
            "diameter": VIAL_DIAMETER_MM,
            "location": {"x": top_point.x, "y": top_point.y, "z": top_point.z},
            "capacity_ul": round_mm(row.capacity),
            "working_volume_ul": round_mm(row.capacity),
            **_vial_role_and_solution(row),
        }

        volume_ul, composition, mismatch = _vial_composition(row)
        if mismatch:
            conflicts.append(Conflict(
                category="vial_contents_volume_mismatch",
                severity="warning",
                row_ids=(row.id,),
                message=(
                    f"vial {row.position!r} (id {row.id}) contents JSON does not sum to "
                    f"the recorded volume; rescaled proportionally to volume_ul={volume_ul:g}"
                ),
                resolved=True,
                resolution="rescaled to match recorded volume",
            ))
        fluids[row.position] = {"volume_ul": volume_ul, "composition": composition}

    return labware, fluids, conflicts


def _select_vials(
    vials: tuple[VialRow, ...], resolutions: Resolutions,
) -> tuple[list[VialRow], list[Conflict]]:
    conflicts: list[Conflict] = []
    by_position: dict[str, list[VialRow]] = {}
    for row in vials:
        if not row.active:
            continue
        by_position.setdefault(row.position, []).append(row)

    included: list[VialRow] = []
    for position, rows in sorted(by_position.items()):
        if len(rows) == 1:
            included.append(rows[0])
            continue
        ids = tuple(sorted(r.id for r in rows))
        remaining = [r for r in rows if r.id not in resolutions.exclude_vial_ids]
        resolved = len(remaining) == 1
        conflicts.append(Conflict(
            category="vial_duplicate_position",
            severity="conflict",
            row_ids=ids,
            message=(
                f"position {position!r} has {len(rows)} active panda_vials rows {ids}; "
                "expected exactly one."
            ),
            resolved=resolved,
            resolution="exclude_vial_ids" if resolved else None,
        ))
        if resolved:
            included.append(remaining[0])
    return included, conflicts


def _vial_role_and_solution(row: VialRow) -> dict:
    """Return the Feature-05 ``role``/``solution`` fields for one vial row.

    ``role`` comes straight from ``panda_vials.category`` via
    ``VIAL_CATEGORY_ROLES`` (0=stock, 1=waste, 2=process/bath); unrecognized
    category values are left unlabeled (``None``) rather than guessed.

    ``solution`` is ``panda_vials.name`` -- PANDA-BEAR's own automatic stock
    selection (``panda_lib.actions.vessel_handling.solution_selector``)
    matches a requested solution name against exactly this field
    (``solution.name.lower() == solution_name.lower()``), so it is the
    proven canonical solution identity, not a display label. Omitted when
    blank.
    """
    fields: dict = {}
    role = VIAL_CATEGORY_ROLES.get(row.category)
    if role is not None:
        fields["role"] = role
    solution = row.name.strip()
    if solution:
        fields["solution"] = solution
    return fields


def _vial_composition(row: VialRow) -> tuple[float, dict[str, float], bool]:
    """Build a fluid-state-valid ``(volume_ul, composition, mismatch)``.

    ``load_initial_fluids`` requires ``sum(composition.values()) ==
    volume_ul`` exactly. A few production rows have a ``contents`` JSON blob
    whose values don't sum to the row's own ``volume`` column (verified
    against the production snapshot: ids 6 and 11). Rescale non-zero
    components proportionally to the recorded volume and report the
    mismatch as a warning rather than raising -- the recorded ``volume`` is
    the trusted total.
    """
    volume = round_mm(row.volume)
    raw = {str(k): float(v) for k, v in row.contents.items() if float(v) > 0}
    total = sum(raw.values())

    if not raw or total <= 0:
        composition = {} if volume <= 0 else {"unknown": volume}
        mismatch = bool(raw) and volume > 0
        return volume, composition, mismatch

    mismatch = abs(total - volume) > 1e-6
    scale = volume / total
    scaled = {name: round(amount * scale, 6) for name, amount in raw.items()}
    if scaled:
        drift = volume - sum(scaled.values())
        largest = max(scaled, key=scaled.get)
        scaled[largest] = round(scaled[largest] + drift, 6)
    return volume, dict(sorted(scaled.items())), mismatch


# --------------------------------------------------------------------------
# Well plate
# --------------------------------------------------------------------------


def _build_wellplate(
    snapshot: PandaBearSnapshot,
    resolutions: Resolutions,
    offset: ToolOffset,
    working_volume: WorkingVolume,
) -> tuple[dict, list[Conflict]]:
    conflicts: list[Conflict] = []
    current_plates = [w for w in snapshot.wellplates if w.current]
    if len(current_plates) != 1:
        raise ImportBuildError(
            f"Expected exactly one current panda_wellplates row, found {len(current_plates)}."
        )
    plate = current_plates[0]
    wells = [w for w in snapshot.wells if w.plate_id == plate.id]
    if not wells:
        raise ImportBuildError(f"Current wellplate {plate.id} has no panda_well_hx rows.")

    well_heights = sorted({w.height for w in wells})
    if len(well_heights) > 1:
        conflicts.append(Conflict(
            category="well_hx_height_inconsistent",
            severity="warning",
            row_ids=(plate.id,),
            message=f"plate {plate.id} panda_well_hx rows disagree on height: {well_heights}",
            resolved=True,
            resolution="reported only; using the first observed value",
        ))
    well_height = well_heights[0]

    if abs(well_height - plate.height) > 1e-9:
        override = resolutions.wellplate_height_overrides.get(plate.id)
        resolved = override is not None and abs(override - well_height) < 1e-9
        conflicts.append(Conflict(
            category="wellplate_height_source_mismatch",
            severity="conflict",
            row_ids=(plate.id,),
            message=(
                f"plate {plate.id}: panda_wellplates.height={plate.height:g} disagrees with "
                f"panda_well_hx height={well_height:g} recorded on every one of its wells"
            ),
            resolved=resolved,
            resolution="wellplate_height_overrides" if resolved else None,
        ))
        if not resolved:
            return {}, conflicts

    parsed = {w.well_id: _parse_location_id(w.well_id) for w in wells}
    row_letters = sorted({r for r, _c in parsed.values()})
    col_numbers = sorted({c for _r, c in parsed.values()})
    rows, columns = len(row_letters), len(col_numbers)
    if rows * columns != len(wells):
        raise ImportBuildError(
            f"plate {plate.id} panda_well_hx rows ({len(wells)}) do not form a complete "
            f"{rows}x{columns} grid."
        )

    by_id = {w.well_id: w for w in wells}
    a1 = by_id[f"{row_letters[0]}{col_numbers[0]}"]
    a2_id = f"{row_letters[0]}{col_numbers[1]}" if columns > 1 else f"{row_letters[1]}{col_numbers[0]}"
    b1_id = f"{row_letters[1]}{col_numbers[0]}" if rows > 1 else a2_id
    a2 = by_id[a2_id]
    b1 = by_id[b1_id]

    a1_point = _convert(a1.x, a1.y, a1.top, offset)
    a2_point = _convert(a2.x, a2.y, a2.top, offset)
    b1_point = _convert(b1.x, b1.y, b1.top, offset)
    x_offset, y_offset = pitch_offsets(a1_point, a2_point, b1_point)

    calibration = {
        "a1": {"x": a1_point.x, "y": a1_point.y, "z": a1_point.z},
        "a2": {"x": a2_point.x, "y": a2_point.y},
    }
    capacity_ul = round_mm(plate.capacity_ul)

    entry = WellPlateYamlEntry(
        name="ito_pama_plate",
        rows=rows,
        columns=columns,
        calibration=calibration,
        x_offset=x_offset,
        y_offset=y_offset,
        capacity_ul=capacity_ul,
        working_volume_ul=capacity_ul,
    )
    predicted = derive_wells_preview(entry, resolved_z=a1_point.z)

    max_residual = 0.0
    for w in wells:
        actual = _convert(w.x, w.y, w.top, offset)
        env_msg = envelope_violation(actual, working_volume, ENVELOPE_TOLERANCE_MM)
        if env_msg:
            conflicts.append(Conflict(
                category="position_out_of_envelope",
                severity="warning",
                row_ids=(plate.id,),
                message=(
                    f"well {w.well_id!r} on plate {plate.id} gantry position out of "
                    f"working volume: {env_msg}"
                ),
                resolved=True,
                resolution="reported only (positional; not blocking per project scope)",
            ))
        predicted_point = predicted[w.well_id]
        residual = math.dist((actual.x, actual.y), (predicted_point.x, predicted_point.y))
        max_residual = max(max_residual, residual)

    if max_residual > PITCH_RESIDUAL_TOLERANCE_MM:
        conflicts.append(Conflict(
            category="well_pitch_residual",
            severity="warning",
            row_ids=(plate.id,),
            message=(
                f"plate {plate.id}: max well-grid pitch residual {max_residual:.4f}mm "
                f"exceeds the {PITCH_RESIDUAL_TOLERANCE_MM}mm tolerance"
            ),
            resolved=True,
            resolution="reported only",
        ))

    plate_dict = {
        "type": "well_plate",
        "name": "ito_pama_plate",
        "rows": rows,
        "columns": columns,
        "calibration": calibration,
        "x_offset": x_offset,
        "y_offset": y_offset,
        "capacity_ul": capacity_ul,
        "working_volume_ul": capacity_ul,
    }
    return plate_dict, conflicts


# --------------------------------------------------------------------------
# Tip rack + disposal
# --------------------------------------------------------------------------


def _build_tiprack(
    snapshot: PandaBearSnapshot,
    resolutions: Resolutions,
    offset: ToolOffset,
    working_volume: WorkingVolume,
) -> tuple[tuple[dict, dict], list[Conflict]]:
    conflicts: list[Conflict] = []
    tips_by_rack: dict[int, list[TipRow]] = {}
    for tip in snapshot.tips:
        tips_by_rack.setdefault(tip.rack_id, []).append(tip)

    selected: tuple | None = None
    for rack in sorted(snapshot.tipracks, key=lambda r: r.id):
        tips = tips_by_rack.get(rack.id, [])

        if not tips:
            resolved = rack.id in resolutions.exclude_tiprack_ids
            conflicts.append(Conflict(
                category="tiprack_no_tips",
                severity="conflict",
                row_ids=(rack.id,),
                message=(
                    f"tiprack {rack.id} (declared {rack.rows!r} x {rack.cols}) has zero "
                    "rows in panda_tips."
                ),
                resolved=resolved,
                resolution="exclude_tiprack_ids" if resolved else None,
            ))
            continue

        if rack.id in resolutions.exclude_tiprack_ids:
            conflicts.append(Conflict(
                category="tiprack_excluded",
                severity="warning",
                row_ids=(rack.id,),
                message=(
                    f"tiprack {rack.id} excluded by exclude_tiprack_ids despite having "
                    f"{len(tips)} panda_tips row(s)."
                ),
                resolved=True,
                resolution="exclude_tiprack_ids",
            ))
            continue

        parsed = {tip.tip_id: _parse_location_id(tip.tip_id) for tip in tips}
        row_letters = sorted({r for r, _c in parsed.values()})
        col_numbers = sorted({c for _r, c in parsed.values()})
        actual_rows, actual_cols = len(row_letters), len(col_numbers)
        if actual_rows * actual_cols != len(tips):
            raise ImportBuildError(
                f"tiprack {rack.id} panda_tips rows ({len(tips)}) do not form a complete "
                f"{actual_rows}x{actual_cols} grid."
            )

        declared_total = len(rack.rows) * rack.cols
        if declared_total != len(tips):
            override = resolutions.tiprack_shape_overrides.get(rack.id)
            resolved = override is not None and tuple(override) == (actual_rows, actual_cols)
            conflicts.append(Conflict(
                category="tiprack_shape_mismatch",
                severity="conflict",
                row_ids=(rack.id,),
                message=(
                    f"tiprack {rack.id} declares {rack.rows!r} x {rack.cols} "
                    f"({declared_total} tips) but panda_tips has {len(tips)} row(s) "
                    f"forming a {actual_rows}x{actual_cols} grid."
                ),
                resolved=resolved,
                resolution="tiprack_shape_overrides" if resolved else None,
            ))
            if not resolved:
                continue

        if selected is not None:
            raise ImportBuildError(
                "Multiple tipracks resolved for import; expected exactly one "
                "(exclude the extra with exclude_tiprack_ids)."
            )
        selected = (rack, tips, row_letters, col_numbers, actual_rows, actual_cols)

    if selected is None:
        return ({}, {}), conflicts

    rack, tips, row_letters, col_numbers, rows, columns = selected
    by_id = {tip.tip_id: tip for tip in tips}

    missing_ids = tuple(sorted(t.id for t in tips if t.tip_length is None))
    if missing_ids:
        resolved = resolutions.tip_length_mm is not None
        conflicts.append(Conflict(
            category="missing_tip_length",
            severity="conflict",
            row_ids=missing_ids,
            message=f"tiprack {rack.id}: {len(missing_ids)} tip(s) have NULL tip_length.",
            resolved=resolved,
            resolution="tip_length_mm" if resolved else None,
        ))
        if not resolved:
            return ({}, {}), conflicts
        tip_length_mm = resolutions.tip_length_mm
    else:
        lengths = {t.tip_length for t in tips}
        if len(lengths) != 1:
            raise ImportBuildError(
                f"tiprack {rack.id} tips report inconsistent tip_length values: {sorted(lengths)}."
            )
        tip_length_mm = next(iter(lengths))

    pickup_heights = {t.pickup_height for t in tips}
    if len(pickup_heights) != 1:
        raise ImportBuildError(
            f"tiprack {rack.id} tips report inconsistent pickup_height values."
        )
    pickup_height = next(iter(pickup_heights))

    a1 = by_id[f"{row_letters[0]}{col_numbers[0]}"]
    a2_id = f"{row_letters[0]}{col_numbers[1]}" if columns > 1 else f"{row_letters[1]}{col_numbers[0]}"
    b1_id = f"{row_letters[1]}{col_numbers[0]}" if rows > 1 else a2_id
    a2 = by_id[a2_id]
    b1 = by_id[b1_id]

    a1_point = _convert(a1.x, a1.y, pickup_height, offset)
    a2_point = _convert(a2.x, a2.y, pickup_height, offset)
    b1_point = _convert(b1.x, b1.y, pickup_height, offset)
    x_offset, y_offset = pitch_offsets(a1_point, a2_point, b1_point)
    drop_point = _convert(rack.drop_x, rack.drop_y, rack.drop_z, offset)

    all_tip_ids = tuple(sorted(t.id for t in tips))
    env_msg = envelope_violation(a1_point, working_volume, ENVELOPE_TOLERANCE_MM)
    if env_msg:
        conflicts.append(Conflict(
            category="position_out_of_envelope",
            severity="warning",
            row_ids=all_tip_ids,
            message=(
                f"tiprack {rack.id}: pickup_z={a1_point.z:g} is out of the working volume for "
                f"all {len(tips)} tips ({env_msg}). Pickup Z is uniform across the rack "
                "(panda_tips.pickup_height is identical for every tip); consistent with an "
                "unverified ~4mm homing pull-off / Z-datum offset -- pending live controller "
                "$#/WPos confirmation before physical use."
            ),
            resolved=True,
            resolution="reported only (positional; not blocking per project scope)",
        ))
    drop_env_msg = envelope_violation(drop_point, working_volume, ENVELOPE_TOLERANCE_MM)
    if drop_env_msg:
        conflicts.append(Conflict(
            category="position_out_of_envelope",
            severity="warning",
            row_ids=(rack.id,),
            message=f"tiprack {rack.id} drop position out of working volume: {drop_env_msg}",
            resolved=True,
            resolution="reported only (positional; not blocking per project scope)",
        ))

    calibration = {
        "a1": {"x": a1_point.x, "y": a1_point.y, "z": a1_point.z},
        "a2": {"x": a2_point.x, "y": a2_point.y},
    }
    tiprack_dict = {
        "type": "tip_rack",
        "name": "tip_rack",
        "rows": rows,
        "columns": columns,
        "pickup_z": a1_point.z,
        "drop_z": drop_point.z,
        "tip_length": round_mm(tip_length_mm),
        "calibration": calibration,
        "x_offset": x_offset,
        "y_offset": y_offset,
    }
    disposal_dict = {
        "type": "tip_disposal",
        "name": "tip_disposal",
        "location": {"x": drop_point.x, "y": drop_point.y, "z": drop_point.z},
        "slots": {
            "discard": {
                "location": {"x": drop_point.x, "y": drop_point.y, "z": drop_point.z},
                "supported_labware_types": ["pipette_tip"],
                "description": "Tiprack drop_coordinates, converted to gantry frame.",
            },
        },
    }
    return (tiprack_dict, disposal_dict), conflicts


# --------------------------------------------------------------------------
# Gantry + tour
# --------------------------------------------------------------------------


def _build_gantry_full(gantry_raw: dict, pipette_offset: ToolOffset) -> dict:
    merged = copy.deepcopy(gantry_raw)
    instruments = dict(merged.get("instruments", {}))
    instruments["pipette"] = {
        "type": "pipette",
        "vendor": "opentrons",
        "pipette_model": "p300_single_gen2",
        "port": "",
        "offline": True,
        # CubOS offsets are the negation of the PANDA-BEAR tool offset for
        # x/y; depth is the +z offset unchanged (see instrument_mount.py:
        # gantry_x = target_x - offset_x, gantry_z = target_z + depth, which
        # is algebraically identical to PANDA-BEAR's
        # gantry = tool_target + tool_offset when offset_cubos = -offset_pandabear).
        "offset_x": round_mm(-pipette_offset.x),
        "offset_y": round_mm(-pipette_offset.y),
        "depth": round_mm(pipette_offset.z),
    }
    _upgrade_capper_entry(instruments)
    merged["instruments"] = instruments
    return merged


def _upgrade_capper_entry(instruments: dict) -> None:
    """Upgrade a generic `mounted_tool`/`mount_only` capper mount in place.

    The raw source gantry YAML predates the dedicated `capper` instrument
    type (Feature 06) and still declares the capper/decapper mount as a
    calibration-only `mounted_tool`/`mount_only` placeholder. This rewrites
    it to `type: capper, vendor: pawduino` (see
    cubos.instruments.capper.vendors.pawduino), preserving its calibrated
    `offset_x`/`offset_y`/`depth`/`offline` and adding the motion-sequence
    config the `decap`/`cap` protocol commands read
    (`engage_depth_mm`/`capture_retries`/`capture_settle_s` -- placeholder
    values, see constants.py).

    A no-op when the mount entry is absent (the capper instrument stays
    optional -- existing non-capper gantry configs import unchanged) or
    already something other than the generic mount_only placeholder (never
    clobbers an already-upgraded or hand-authored entry).
    """
    entry = instruments.get(CAPPER_INSTRUMENT_KEY)
    if not isinstance(entry, dict):
        return
    if entry.get("type") != "mounted_tool" or entry.get("vendor") != "mount_only":
        return
    instruments[CAPPER_INSTRUMENT_KEY] = {
        **entry,
        "type": "capper",
        "vendor": "pawduino",
        "port": "",
        "engage_depth_mm": CAPPER_ENGAGE_DEPTH_MM,
        "capture_retries": CAPPER_CAPTURE_RETRIES,
        "capture_settle_s": CAPPER_CAPTURE_SETTLE_S,
    }


def _row_label(row_number: int) -> str:
    """1-indexed row number -> spreadsheet-style label (1->A, 2->B, ...)."""
    label = ""
    value = row_number
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        label = chr(65 + remainder) + label
    return label


def _build_tour(labware: dict) -> dict:
    plate_key = next((k for k, v in labware.items() if v.get("type") == "well_plate"), None)
    rack_key = next((k for k, v in labware.items() if v.get("type") == "tip_rack"), None)
    disposal_key = next((k for k, v in labware.items() if v.get("type") == "tip_disposal"), None)
    vial_keys = [k for k, v in labware.items() if v.get("type") == "vial"]

    canonical_order = ["s1", "s2", "s3", "s4", "s5", "s6", "w1", "w2", "w3", "e1"]
    vial_order = [k for k in canonical_order if k in vial_keys]
    vial_order += sorted(k for k in vial_keys if k not in vial_order)

    def hover(position: str) -> dict:
        return {"move": {"instrument": "camera", "position": position}}

    steps: list[dict] = [{"home": None}]

    if plate_key is not None:
        plate = labware[plate_key]
        last_well = f"{_row_label(plate['rows'])}{plate['columns']}"
        steps.append(hover(f"{plate_key}.A1"))
        steps.append(hover(f"{plate_key}.{last_well}"))

    if rack_key is not None:
        rack = labware[rack_key]
        last_tip = f"{_row_label(rack['rows'])}{rack['columns']}"
        steps.append(hover(f"{rack_key}.A1"))
        steps.append(hover(f"{rack_key}.{last_tip}"))

    for key in vial_order:
        steps.append(hover(key))

    if disposal_key is not None:
        steps.append(hover(f"{disposal_key}.discard"))

    steps.append({"home": None})
    return {"protocol": steps}


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _convert(x: float, y: float, z: float, offset: ToolOffset) -> Point:
    return to_gantry(Point(x=x, y=y, z=z), offset)


def _parse_location_id(location_id: str) -> tuple[str, int]:
    match = _LOCATION_ID_RE.match(location_id)
    if not match:
        raise ImportBuildError(f"Unrecognized well/tip ID {location_id!r}.")
    return match.group(1), int(match.group(2))
