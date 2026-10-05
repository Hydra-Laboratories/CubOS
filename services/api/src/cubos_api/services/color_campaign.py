"""Generate the fixed-volume three- or four-stock color-matching campaign."""

from __future__ import annotations

import copy
import math
import uuid
from pathlib import Path

import yaml

from cubos.gantry.gantry_config import GantryConfig
from cubos.optimization import rgb_to_lab
from cubos_api.models.campaigns import CampaignSpec, ColorCampaignSetup


def _component_step(total_volume_ul: float) -> float:
    return total_volume_ul / 60.0


def _three_component_initial_points(
    component_min_ul: float,
    component_max_ul: float,
    total_volume_ul: float,
) -> list[dict[str, float]]:
    dominant = total_volume_ul - 2 * component_min_ul
    if dominant > component_max_ul:
        raise ValueError("Component maximum cannot fit a dominant initial recipe")
    middle = (total_volume_ul - component_min_ul) / 2
    if middle > component_max_ul:
        raise ValueError("Component maximum cannot fit a mixed initial recipe")
    return [
        {"red_ul": dominant, "yellow_ul": component_min_ul, "blue_ul": component_min_ul},
        {"red_ul": component_min_ul, "yellow_ul": dominant, "blue_ul": component_min_ul},
        {"red_ul": component_min_ul, "yellow_ul": component_min_ul, "blue_ul": dominant},
        {"red_ul": middle, "yellow_ul": middle, "blue_ul": component_min_ul},
        {"red_ul": middle, "yellow_ul": component_min_ul, "blue_ul": middle},
        {"red_ul": component_min_ul, "yellow_ul": middle, "blue_ul": middle},
    ]


INITIAL_POINTS = _three_component_initial_points(50.0, 200.0, 300.0)

_NO_DILUENT_COMMANDS = [
    "pick_up_tip", "transfer", "drop_tip",
    "pick_up_tip", "transfer", "drop_tip",
    "pick_up_tip", "transfer", "mix", "drop_tip",
]
_DILUENT_COMMANDS = [
    "pick_up_tip", "transfer", "drop_tip",
] + _NO_DILUENT_COMMANDS


def _four_component_initial_points(
    component_min_ul: float, component_max_ul: float, total_volume_ul: float = 300.0,
) -> list[dict[str, float]]:
    """Six dye-dominant-corner and mixed points, every one summing to 300 uL."""
    minimum = component_min_ul
    step_ul = _component_step(total_volume_ul)
    water_min_ul = step_ul
    grid_max = minimum + step_ul * math.floor(
        (component_max_ul - minimum) / step_ul + 1e-9
    )
    corner_ceiling = min(grid_max, total_volume_ul - 2 * minimum - water_min_ul)
    corner_dye = minimum + step_ul * math.floor(
        (corner_ceiling - minimum) / step_ul + 1e-9
    )
    corner_water = total_volume_ul - corner_dye - 2 * minimum
    mid_ceiling = min(grid_max, (total_volume_ul - minimum - water_min_ul) / 2)
    mid_dye = minimum + step_ul * math.floor(
        (mid_ceiling - minimum) / step_ul + 1e-9
    )
    mixed_water = total_volume_ul - 2 * mid_dye - minimum

    def point(red: float, yellow: float, blue: float, water: float) -> dict[str, float]:
        return {"red_ul": red, "yellow_ul": yellow, "blue_ul": blue, "water_ul": water}

    candidates = [
        point(corner_dye, minimum, minimum, corner_water),
        point(minimum, corner_dye, minimum, corner_water),
        point(minimum, minimum, corner_dye, corner_water),
        point(mid_dye, mid_dye, minimum, mixed_water),
        point(mid_dye, minimum, mid_dye, mixed_water),
        point(minimum, mid_dye, mid_dye, mixed_water),
    ]
    unique: list[dict[str, float]] = []
    seen: set[tuple[float, ...]] = set()
    for candidate in candidates:
        key = tuple(candidate[name] for name in ("red_ul", "yellow_ul", "blue_ul", "water_ul"))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _source_steps(
    source_protocol_yaml: str, *, batch_size: int, has_diluent: bool,
) -> list[dict]:
    try:
        document = yaml.safe_load(source_protocol_yaml)
    except yaml.YAMLError as exc:
        raise ValueError(f"Cannot parse source color protocol: {exc}") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"protocol"}
        or not isinstance(document.get("protocol"), list)
    ):
        raise ValueError(
            "Source color protocol must contain only a protocol list; other "
            "sections cannot be silently omitted"
        )
    steps = document["protocol"]
    expected = _DILUENT_COMMANDS if has_diluent else _NO_DILUENT_COMMANDS
    observed = [
        next(iter(step)) if isinstance(step, dict) and len(step) == 1 else None
        for step in steps
    ]
    if observed != expected:
        raise ValueError(
            "Source color protocol must be exactly four pickup/transfer groups "
            "(water first, then red, yellow, blue) with a final mix and drop "
            "when a diluent source is configured, or three groups without one; "
            "unsupported steps cannot be silently omitted"
        )
    for index, step in enumerate(steps):
        args = next(iter(step.values()))
        if not isinstance(args, dict):
            raise ValueError(f"Source color protocol step {index} needs arguments")
    transfer_indexes = (1, 4, 7, 10) if has_diluent else (1, 4, 7)
    for index in transfer_indexes:
        transfer = steps[index]["transfer"]
        if not all(key in transfer for key in ("source", "destination", "volume_ul")):
            raise ValueError(f"Source color transfer step {index} is incomplete")
        destination_height = transfer.get("destination_height", 0.0)
        try:
            destination_height = float(destination_height)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Source color transfer step {index} has invalid destination_height"
            ) from exc
        if not math.isfinite(destination_height):
            raise ValueError("Destination height must be finite")
        if batch_size > 1 and destination_height < 0:
            raise ValueError(
                "Shared component tips require a nonnegative destination_height "
                "to dispense above or at the calibrated well reference"
            )
    mix_index = 11 if has_diluent else 8
    mix = steps[mix_index]["mix"]
    if not all(key in mix for key in ("position", "volume_ul", "cycles", "height")):
        raise ValueError("Source color mix step is incomplete")
    return copy.deepcopy(steps)


def target_protocol(
    target_well: str,
    camera: str,
    roi_fraction: float,
    image_height: float | None = None,
    expected_center: tuple[float, float] | None = None,
    expected_center_source: str | None = None,
) -> str:
    measure_args: dict[str, object] = {
        "instrument": camera,
        "position": target_well,
        "label": "color_target",
        "roi_fraction": roi_fraction,
    }
    if image_height is not None:
        measure_args["image_height"] = image_height
    if expected_center is not None:
        measure_args["expected_center"] = list(expected_center)
        measure_args["expected_center_source"] = expected_center_source
    return yaml.safe_dump({"protocol": [
        {"move": {"instrument": camera, "position": target_well}},
        {"measure_color": measure_args},
    ]}, sort_keys=False)


def build_color_campaign(
    setup: ColorCampaignSetup,
    protocol_directory: Path,
    *,
    available_tip_positions: list[str] | None = None,
    source_protocol_yaml: str,
    gantry_config: GantryConfig | None = None,
) -> CampaignSpec:
    """Write an immutable generated protocol and return its campaign spec."""
    if setup.target_mode == "camera":
        if setup.target_lab is None or setup.reference_processing_profile_id is None:
            raise ValueError(
                "Camera color campaigns require an accepted target measurement and profile"
            )
        target_lab = setup.target_lab
        reference_origin = setup.reference_origin or "accepted_camera_measurement"
        reference_rgb = None
    else:
        if setup.target_rgb is None:
            raise ValueError("RGB color campaigns require target_rgb")
        target_lab = rgb_to_lab(setup.target_rgb)
        reference_origin = setup.reference_origin or "user_selected_srgb"
        if reference_origin != "user_selected_srgb":
            raise ValueError("RGB color campaigns require a user-selected sRGB reference")
        if setup.reference_processing_profile_id is not None:
            raise ValueError("RGB color campaigns cannot use a camera processing profile")
        reference_rgb = setup.target_rgb

    has_diluent = setup.diluent_source is not None
    source_steps = _source_steps(
        source_protocol_yaml, batch_size=setup.batch_size, has_diluent=has_diluent,
    )
    trial_count = len(setup.candidate_wells)
    batch_size = setup.batch_size
    component_count = 4 if has_diluent else 3
    component_names = (["water"] if has_diluent else []) + ["red", "yellow", "blue"]
    initial_points = (
        _four_component_initial_points(
            setup.component_min_ul, setup.component_max_ul, setup.total_volume_ul,
        )
        if has_diluent else _three_component_initial_points(
            setup.component_min_ul, setup.component_max_ul, setup.total_volume_ul,
        )
    )
    required_tips = (
        trial_count * component_count if batch_size == 1
        else sum(component_count + min(batch_size, trial_count - start)
                 for start in range(0, trial_count, batch_size))
    )
    if available_tip_positions is None:
        available_tip_positions = [
            f"tips.{chr(ord('A') + index // 12)}{index % 12 + 1}"
            for index in range(required_tips)
        ]
    if len(available_tip_positions) < required_tips:
        raise ValueError(
            f"Color campaign needs {required_tips} available tips for "
            f"{trial_count} trials, but the durable state has "
            f"{len(available_tip_positions)}."
        )
    tip_values: dict[str, list[str]] = {f"{name}_tip": [] for name in component_names}
    tip_values["mix_tip"] = []
    cursor = 0
    for start in range(0, trial_count, batch_size):
        count = min(batch_size, trial_count - start)
        assigned = available_tip_positions[cursor:cursor + component_count]
        cursor += component_count
        if batch_size == 1:
            mixes: list[str] = []
        else:
            mixes = available_tip_positions[cursor:cursor + count]
            cursor += count
        for name, tip in zip(component_names, assigned):
            tip_values[f"{name}_tip"].extend([tip] * count)
        tip_values["mix_tip"].extend(mixes)

    if has_diluent:
        component_steps = [
            ("water", 0, 1, 2),
            ("red", 3, 4, 5),
            ("yellow", 6, 7, 8),
            ("blue", 9, 10, 12),
        ]
        mix_template_index = 11
        mix_pickup_template_index = 9
        drop_template_index = 12
    else:
        component_steps = [
            ("red", 0, 1, 2),
            ("yellow", 3, 4, 5),
            ("blue", 6, 7, 9),
        ]
        mix_template_index = 8
        mix_pickup_template_index = 6
        drop_template_index = 9
    last_color = component_steps[-1][0]

    first_well = setup.candidate_wells[0]
    initial_point = initial_points[0]
    steps: list[dict] = []
    for color, pickup_index, transfer_index, drop_index in component_steps:
        pickup = copy.deepcopy(source_steps[pickup_index])
        pickup["pick_up_tip"]["position"] = tip_values[f"{color}_tip"][0]
        transfer = copy.deepcopy(source_steps[transfer_index])
        transfer["transfer"].update({
            "source": setup.diluent_source if color == "water" else getattr(setup, f"{color}_source"),
            "destination": first_well,
            "volume_ul": initial_point[f"{color}_ul"],
        })
        steps.extend((pickup, transfer))
        if color != last_color or batch_size > 1:
            steps.append(copy.deepcopy(source_steps[drop_index]))
    if batch_size > 1:
        mix_pickup = copy.deepcopy(source_steps[mix_pickup_template_index])
        mix_pickup["pick_up_tip"]["position"] = tip_values["mix_tip"][0]
        steps.append(mix_pickup)
    mix_step = copy.deepcopy(source_steps[mix_template_index])
    mix_step["mix"]["position"] = first_well
    steps.extend((mix_step, copy.deepcopy(source_steps[drop_template_index])))
    steps.extend((
        {"move": {"instrument": setup.camera_instrument, "position": first_well}},
        {"measure_color": {
            "instrument": setup.camera_instrument,
            "position": first_well,
            "label": "color_candidate",
            "roi_fraction": setup.roi_fraction,
            "reference_lab": list(target_lab),
            **(
                {"reference_processing_profile_id": setup.reference_processing_profile_id}
                if setup.reference_processing_profile_id is not None else {}
            ),
            "reference_origin": reference_origin,
            **(
                {"reference_rgb": list(reference_rgb)}
                if reference_rgb is not None else {}
            ),
            "expected_center": list(setup.expected_center),
            "expected_center_source": setup.expected_center_source,
            **(
                {"image_height": setup.image_height}
                if setup.image_height is not None else {}
            ),
        }},
    ))
    if setup.photo_position is not None:
        if gantry_config is None:
            raise ValueError("photo_position requires the selected gantry configuration")
        try:
            camera_mount = gantry_config.instruments[setup.camera_instrument]
        except KeyError as exc:
            raise ValueError(
                f"Photo pose camera {setup.camera_instrument!r} is not configured on the gantry"
            ) from exc
        carriage_x, carriage_y, carriage_z = setup.photo_position
        if not gantry_config.working_volume.contains(carriage_x, carriage_y, carriage_z):
            raise ValueError("photo_position is outside the gantry working volume")
        camera_position = [
            carriage_x + float(camera_mount.get("offset_x", 0.0)),
            carriage_y + float(camera_mount.get("offset_y", 0.0)),
            carriage_z - float(camera_mount.get("depth", 0.0)),
        ]
        steps.extend((
            {"move": {
                "instrument": setup.camera_instrument,
                "position": camera_position,
            }},
            {"photo_pause": {
                "settle_seconds": 2.0,
                "capture_hold_seconds": 2.0,
                "well": first_well,
            }},
        ))
    protocol = {"protocol": steps}
    filename = f"ade_color_matching_{uuid.uuid4().hex[:8]}.yaml"

    transfer_indexes = tuple(3 * i + 1 for i in range(component_count))
    pickup_indexes = tuple(3 * i for i in range(component_count))
    mix_index = (
        3 * (component_count - 1) + 2 if batch_size == 1 else 3 * component_count + 1
    )
    move_index = mix_index + 2
    measure_index = move_index + 1
    photo_pause_index = measure_index + 2 if setup.photo_position is not None else None
    mix_pickup_index = 3 * component_count
    destination_indexes = [
        *((index, "destination") for index in transfer_indexes),
        (mix_index, "position"),
        (move_index, "position"),
        (measure_index, "position"),
    ]
    if photo_pause_index is not None:
        destination_indexes.append((photo_pause_index, "well"))
    destination_bindings = [
        {"step_index": index, "argument": argument}
        for index, argument in destination_indexes
    ]
    if has_diluent:
        water_min_ul = _component_step(setup.total_volume_ul)
        water_max = setup.total_volume_ul - 3 * setup.component_min_ul
        bounds = {
            "water": (water_min_ul, water_max),
            "red": (setup.component_min_ul, setup.component_max_ul),
            "yellow": (setup.component_min_ul, setup.component_max_ul),
            "blue": (setup.component_min_ul, setup.component_max_ul),
        }
    else:
        bounds = {
            "red": (setup.component_min_ul, setup.component_max_ul),
            "yellow": (setup.component_min_ul, setup.component_max_ul),
            "blue": (setup.component_min_ul, setup.component_max_ul),
        }
    spec = CampaignSpec(
        name="CIEDE2000 color matching",
        gantry_file=setup.gantry_file,
        deck_file=setup.deck_file,
        protocol_file=filename,
        parameters=[
            {"name": f"{name}_ul", "minimum": bounds[name][0], "maximum": bounds[name][1],
             "step": _component_step(setup.total_volume_ul),
             "bindings": [{"step_index": transfer_indexes[index], "argument": "volume_ul"}]}
            for index, name in enumerate(component_names)
        ],
        sequences=[
            {"name": "candidate_well", "values": setup.candidate_wells,
             "bindings": destination_bindings},
            *[
                {"name": f"{name}_tip", "values": tip_values[f"{name}_tip"],
                 "bindings": [{"step_index": pickup_indexes[index], "argument": "position"}]}
                for index, name in enumerate(component_names)
            ],
            *([{"name": "mix_tip", "values": tip_values["mix_tip"],
                "bindings": [{"step_index": mix_pickup_index, "argument": "position"}]}]
              if batch_size > 1 else []),
        ],
        objective={"mode": "result", "path": f"{measure_index}.delta_e_00", "direction": "minimize"},
        optimizer={"method": "ei", "kernel": "matern52", "initial_trials": 6,
                   "initial_points": initial_points, "exploration": 0.05, "seed": 7},
        stop={"max_trials": trial_count, "target_value": 3.0, "patience": 0,
              "min_improvement": 0.0, "max_seconds": None},
        sum_constraint={
            "parameters": [f"{name}_ul" for name in component_names],
            "total": setup.total_volume_ul,
        },
        mock_mode=setup.mock_mode,
        fluid_state_id=setup.fluid_state_id,
        batch_size=batch_size,
        source_protocol_file=setup.source_protocol_file,
        target_mode=setup.target_mode,
        target_rgb=setup.target_rgb,
        target_run_id=(setup.target_run_id if setup.target_mode == "camera" else None),
        target_analysis_revision=(
            setup.target_analysis_revision if setup.target_mode == "camera" else None
        ),
        target_well=(setup.target_well if setup.target_mode == "camera" else None),
        target_lab=(setup.target_lab if setup.target_mode == "camera" else None),
        reference_processing_profile_id=(
            setup.reference_processing_profile_id
            if setup.target_mode == "camera" else None
        ),
    )
    protocol_directory.mkdir(parents=True, exist_ok=True)
    (protocol_directory / filename).write_text(yaml.safe_dump(protocol, sort_keys=False))
    return spec
