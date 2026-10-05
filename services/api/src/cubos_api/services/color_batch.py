"""Strict color-major expansion of one verified color campaign template."""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Any

import yaml

from cubos_api.models.campaigns import CampaignSpec
from cubos_api.services.campaign_templates import compile_trial


_BATCH_COMMANDS_LEGACY = (
    "pick_up_tip", "transfer", "drop_tip",
    "pick_up_tip", "transfer", "drop_tip",
    "pick_up_tip", "transfer", "drop_tip",
    "pick_up_tip", "mix", "drop_tip",
    "move", "measure_color",
)
_BATCH_COMMANDS_DILUENT = (
    "pick_up_tip", "transfer", "drop_tip",
    "pick_up_tip", "transfer", "drop_tip",
    "pick_up_tip", "transfer", "drop_tip",
    "pick_up_tip", "transfer", "drop_tip",
    "pick_up_tip", "mix", "drop_tip",
    "move", "measure_color",
)
_DYE_STEPS_LEGACY = ((0, 1, 2), (3, 4, 5), (6, 7, 8))
_MIX_STEPS_LEGACY = (9, 10, 11, 12, 13)
_DYE_STEPS_DILUENT = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (9, 10, 11))
_MIX_STEPS_DILUENT = (12, 13, 14, 15, 16)
_MAX_COMPONENT_TRANSFER_UL = 300.0
_PHOTO_TAIL = ("move", "photo_pause")


@dataclass(frozen=True)
class BatchCompilation:
    protocol_yaml: str
    objective_paths: tuple[str, ...]
    sample_map: tuple[dict[str, Any], ...]


def _commands(steps: list) -> tuple[str | None, ...]:
    return tuple(
        next(iter(step)) if isinstance(step, dict) and len(step) == 1 else None
        for step in steps
    )


def _detect_layout(
    protocol_yaml: str,
) -> tuple[tuple[str, ...], tuple[tuple[int, ...], ...], tuple[int, ...]]:
    document = yaml.safe_load(protocol_yaml)
    steps = document.get("protocol") if isinstance(document, dict) else None
    commands = _commands(steps) if isinstance(steps, list) else ()
    if commands in (_BATCH_COMMANDS_DILUENT, _BATCH_COMMANDS_DILUENT + _PHOTO_TAIL):
        return commands, _DYE_STEPS_DILUENT, _MIX_STEPS_DILUENT
    if commands in (_BATCH_COMMANDS_LEGACY, _BATCH_COMMANDS_LEGACY + _PHOTO_TAIL):
        return commands, _DYE_STEPS_LEGACY, _MIX_STEPS_LEGACY
    raise ValueError(
        "Color batch template must use the explicit color-and-dedicated-mix "
        "workflow: three pickup/transfer/drop groups (legacy), or four groups "
        "with water first (diluent), each followed by a dedicated mix tip, "
        "move, and measurement; unsupported steps cannot be omitted"
    )


def _steps(protocol_yaml: str, expected_commands: tuple[str, ...]) -> list[dict[str, dict[str, Any]]]:
    document = yaml.safe_load(protocol_yaml)
    if not isinstance(document, dict) or set(document) != {"protocol"}:
        raise ValueError("Color batch template must contain only a protocol list")
    steps = document["protocol"]
    if not isinstance(steps, list):
        raise ValueError("Color batch template protocol must be a list")
    if _commands(steps) != expected_commands:
        raise ValueError(
            "Color batch template must keep the same fixed step layout for "
            "every compiled sample; unsupported steps cannot be omitted"
        )
    if any(not isinstance(next(iter(step.values())), dict) for step in steps):
        raise ValueError("Color batch template command arguments must be mappings")
    return steps


def compile_color_trial_batch(
    base_protocol_yaml: str,
    spec: CampaignSpec,
    parameter_sets: list[dict[str, float]],
    start_index: int,
) -> BatchCompilation:
    """Compile up to eight samples with one noncontact tip per shared component."""
    # TODO(iter): test strict template rejection, partial batches, and exact
    # source/mix settings after the operator's offline batch review.
    if spec.batch_size <= 1:
        raise ValueError("Color batch compiler requires batch_size greater than one")
    if spec.source_protocol_file is None:
        raise ValueError("Color batch requires source protocol provenance")
    commands, dye_steps, mix_steps = _detect_layout(base_protocol_yaml)
    measurement_index_template = mix_steps[-1]
    if spec.objective.mode != "result" or spec.objective.path != f"{measurement_index_template}.delta_e_00":
        raise ValueError(
            f"Color batch template requires a result objective at generated "
            f"step {measurement_index_template} delta_e_00; other objective "
            "paths cannot be remapped"
        )
    if not parameter_sets or len(parameter_sets) > spec.batch_size:
        raise ValueError("Color batch must contain between one and batch_size samples")
    if (
        start_index < 0
        or start_index % spec.batch_size != 0
        or start_index + len(parameter_sets) > spec.stop.max_trials
    ):
        raise ValueError("Color batch sample range is outside the campaign plan")
    _steps(base_protocol_yaml, commands)
    compiled = [
        _steps(compile_trial(
            base_protocol_yaml, spec.model_dump(), parameters, start_index + offset,
        ), commands)
        for offset, parameters in enumerate(parameter_sets)
    ]
    destination_wells = [sample[1]["transfer"]["destination"] for sample in compiled]
    if len(set(destination_wells)) != len(destination_wells):
        raise ValueError("Each color batch sample requires a distinct candidate well")
    output: list[dict[str, dict[str, Any]]] = []
    component_tips: set[str] = set()
    for pickup_index, transfer_index, drop_index in dye_steps:
        tip = compiled[0][pickup_index]["pick_up_tip"]["position"]
        if any(
            sample[pickup_index]["pick_up_tip"]["position"] != tip
            for sample in compiled
        ):
            raise ValueError("Each component uses exactly one shared tip per batch")
        if tip in component_tips:
            raise ValueError("Each component requires a separate tip")
        component_tips.add(tip)
        output.append(copy.deepcopy(compiled[0][pickup_index]))
        source = compiled[0][transfer_index]["transfer"]["source"]
        for sample_index, sample in enumerate(compiled):
            transfer = sample[transfer_index]["transfer"]
            if transfer.get("source") != source:
                raise ValueError("A shared component tip cannot switch stock sources")
            if transfer.get("destination") != destination_wells[sample_index]:
                raise ValueError("Color batch transfer destination is inconsistent")
            volume = float(transfer["volume_ul"])
            if not math.isfinite(volume) or not 0 < volume <= _MAX_COMPONENT_TRANSFER_UL:
                raise ValueError(
                    f"Each shared-tip component transfer must be greater than 0 "
                    f"and at most {_MAX_COMPONENT_TRANSFER_UL:g} µL"
                )
            try:
                destination_height = float(transfer.get("destination_height", 0.0))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Shared component destination_height must be a finite number"
                ) from exc
            if not math.isfinite(destination_height) or destination_height < 0:
                raise ValueError(
                    "Shared component tips require a nonnegative destination_height"
                )
            output.append(copy.deepcopy(sample[transfer_index]))
        output.append(copy.deepcopy(compiled[0][drop_index]))

    mix_tips: set[str] = set()
    objective_paths: list[str] = []
    sample_map: list[dict[str, Any]] = []
    mix_pickup_index, mix_index, _mix_drop_index, move_index, measure_index = mix_steps
    for sample_offset, sample in enumerate(compiled):
        mix_tip = sample[mix_pickup_index]["pick_up_tip"]["position"]
        if mix_tip in component_tips or mix_tip in mix_tips:
            raise ValueError("Each sample requires a fresh dedicated mix tip")
        mix_tips.add(mix_tip)
        well = destination_wells[sample_offset]
        if any(
            sample[index][command]["position"] != well
            for index, command in (
                (mix_index, "mix"), (move_index, "move"), (measure_index, "measure_color"),
            )
        ):
            raise ValueError("Mix and color measurement must target the sample well")
        sample_tail = mix_steps + tuple(range(mix_steps[-1] + 1, len(sample)))
        for index in sample_tail:
            output.append(copy.deepcopy(sample[index]))
        measurement_index = len(output) - len(sample_tail) + sample_tail.index(measure_index)
        objective_path = f"{measurement_index}.delta_e_00"
        objective_paths.append(objective_path)
        sample_map.append({
            "sample_index": start_index + sample_offset,
            "candidate_well": well,
            "parameters": dict(parameter_sets[sample_offset]),
            "measurement_step_index": measurement_index,
            "objective_path": objective_path,
        })
    return BatchCompilation(
        protocol_yaml=yaml.safe_dump({"protocol": output}, sort_keys=False),
        objective_paths=tuple(objective_paths),
        sample_map=tuple(sample_map),
    )
