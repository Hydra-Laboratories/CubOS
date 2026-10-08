"""Validation and deterministic expansion of active-learning protocol templates."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from numbers import Real
from typing import Any

import yaml


class TemplateError(ValueError):
    """Raised when a campaign template or trial result is invalid."""


def _document(protocol_yaml: str) -> tuple[dict[str, Any], list[Any]]:
    try:
        document = yaml.safe_load(protocol_yaml)
    except yaml.YAMLError as exc:
        raise TemplateError(f"protocol YAML is not parseable: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("protocol"), list):
        raise TemplateError("protocol YAML must contain a top-level protocol list")
    steps = document["protocol"]
    if not steps:
        raise TemplateError("protocol list must not be empty")
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or len(step) != 1:
            raise TemplateError(
                f"step {index} must be a single-command mapping; loops are not supported"
            )
        command_args = next(iter(step.values()))
        if not isinstance(command_args, dict):
            raise TemplateError(f"step {index} command arguments must be a mapping")
    return document, steps


def _required_mapping(spec: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = spec.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise TemplateError(f"spec.{key} must be a list of mappings")
    return value


def _binding(binding: Any, *, owner: str) -> tuple[int, str]:
    if not isinstance(binding, dict) or not isinstance(binding.get("step_index"), int) \
            or isinstance(binding.get("step_index"), bool) \
            or not isinstance(binding.get("argument"), str) or not binding["argument"]:
        raise TemplateError(f"{owner} bindings require integer step_index and argument")
    return binding["step_index"], binding["argument"]


def _split_path(path: str) -> list[str]:
    parts = path.split(".")
    if any(not part for part in parts):
        raise TemplateError(f"invalid argument path {path!r}")
    return parts


def _lookup(root: Any, path: str, *, label: str) -> Any:
    value = root
    for part in _split_path(path):
        if isinstance(value, Mapping):
            if part not in value:
                raise TemplateError(f"{label}: missing path {path!r}")
            value = value[part]
        elif isinstance(value, (list, tuple)) and part.isdigit():
            index = int(part)
            if index >= len(value):
                raise TemplateError(f"{label}: missing path {path!r}")
            value = value[index]
        else:
            raise TemplateError(f"{label}: missing path {path!r}")
    return value


def _leaf(step: Mapping[str, Any], argument: str, *, label: str) -> Any:
    command, args = next(iter(step.items()))
    return _lookup(args, argument, label=f"{label} (step {command})")


def validate_template(protocol_yaml: str, spec: dict[str, Any]) -> None:
    """Validate a flat protocol template and all declared binding contracts."""
    if not isinstance(spec, dict):
        raise TemplateError("template spec must be a mapping")
    _, steps = _document(protocol_yaml)
    parameters = _required_mapping(spec, "parameters")
    sequences = _required_mapping(spec, "sequences")
    stop = spec.get("stop", {})
    if not isinstance(stop, dict) or not isinstance(stop.get("max_trials"), int) \
            or isinstance(stop.get("max_trials"), bool) or stop["max_trials"] <= 0:
        raise TemplateError("spec.stop.max_trials must be a positive integer")
    max_trials = stop["max_trials"]
    targets: dict[tuple[int, str], str] = {}
    names: set[str] = set()
    for kind, entries in (("parameter", parameters), ("sequence", sequences)):
        for entry_index, entry in enumerate(entries):
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise TemplateError(f"{kind} {entry_index} requires a non-empty name")
            if name in names:
                raise TemplateError(f"duplicate template name {name!r}")
            names.add(name)
            if kind == "parameter":
                minimum, maximum, step = entry.get("minimum"), entry.get("maximum"), entry.get("step")
                if any(isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(float(v))
                       for v in (minimum, maximum, step)) or step <= 0 or minimum > maximum:
                    raise TemplateError(f"parameter {name!r} has invalid bounds or step")
            else:
                values = entry.get("values")
                if not isinstance(values, list) or len(values) < max_trials or any(not isinstance(v, str) for v in values):
                    raise TemplateError(f"sequence {name!r} must contain at least max_trials strings")
            bindings = entry.get("bindings")
            if not isinstance(bindings, list) or not bindings:
                raise TemplateError(f"{kind} {name!r} requires bindings")
            for binding in bindings:
                step_index, argument = _binding(binding, owner=f"{kind} {name!r}")
                if step_index < 0 or step_index >= len(steps):
                    raise TemplateError(f"{kind} {name!r} binding step index {step_index} is out of range")
                target = (step_index, argument)
                if target in targets:
                    raise TemplateError(f"conflicting duplicate binding at step {step_index}, path {argument!r}")
                targets[target] = f"{kind} {name}"
                value = _leaf(steps[step_index], argument, label=f"{kind} {name!r}")
                if kind == "parameter":
                    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
                        raise TemplateError(f"parameter {name!r} target must be a finite numeric leaf")
                elif not isinstance(value, str):
                    raise TemplateError(f"sequence {name!r} target must be a string leaf")


def _assign(root: Any, path: str, value: Any, *, label: str) -> None:
    parts = _split_path(path)
    parent = root
    for part in parts[:-1]:
        if isinstance(parent, Mapping):
            if part not in parent:
                raise TemplateError(f"{label}: missing path {path!r}")
            parent = parent[part]
        elif isinstance(parent, list) and part.isdigit() and int(part) < len(parent):
            parent = parent[int(part)]
        else:
            raise TemplateError(f"{label}: missing path {path!r}")
    last = parts[-1]
    if isinstance(parent, dict) and last in parent:
        parent[last] = value
    elif isinstance(parent, list) and last.isdigit() and int(last) < len(parent):
        parent[int(last)] = value
    else:
        raise TemplateError(f"{label}: missing path {path!r}")


def compile_trial(protocol_yaml: str, spec: dict[str, Any], parameters: dict[str, float], trial_index: int) -> str:
    """Render one trial while retaining YAML structure and unrelated values."""
    validate_template(protocol_yaml, spec)
    if not isinstance(trial_index, int) or isinstance(trial_index, bool) or trial_index < 0:
        raise TemplateError("trial_index must be a non-negative integer")
    document, steps = _document(protocol_yaml)
    max_trials = spec["stop"]["max_trials"]
    if trial_index >= max_trials:
        raise TemplateError("trial_index must be less than stop.max_trials")
    if not isinstance(parameters, dict):
        raise TemplateError("parameters must be a mapping")
    known = {entry["name"] for entry in spec.get("parameters", [])}
    if set(parameters) != known:
        raise TemplateError("parameters must provide exactly the declared parameter names")
    for entry in spec.get("parameters", []):
        name = entry["name"]
        value = parameters[name]
        minimum, maximum, step = float(entry["minimum"]), float(entry["maximum"]), float(entry["step"])
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)) or not minimum <= float(value) <= maximum:
            raise TemplateError(f"parameter {name!r} is outside its finite bounds")
        if not math.isclose((float(value) - minimum) / step, round((float(value) - minimum) / step), rel_tol=0.0, abs_tol=1e-8):
            raise TemplateError(f"parameter {name!r} is not quantized to step {step}")
        for binding in entry["bindings"]:
            _assign(steps[binding["step_index"]][next(iter(steps[binding["step_index"]]))], binding["argument"], float(value), label=f"parameter {name!r}")
    for entry in spec.get("sequences", []):
        for binding in entry["bindings"]:
            step = steps[binding["step_index"]]
            _assign(step[next(iter(step))], binding["argument"], entry["values"][trial_index], label=f"sequence {entry['name']!r}")
    return yaml.safe_dump(document, sort_keys=False)


def extract_result_objective(result: Any, path: str) -> float:
    """Extract one explicitly addressed finite numeric result value."""
    value = result if path == "" else _lookup(result, path, label="objective")
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise TemplateError("objective must resolve to a finite numeric value")
    return float(value)
