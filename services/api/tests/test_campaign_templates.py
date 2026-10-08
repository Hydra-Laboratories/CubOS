import pytest
import yaml

from cubos_api.services.campaign_templates import (
    TemplateError,
    compile_trial,
    extract_result_objective,
    validate_template,
)


YAML = """protocol:
  - move:
      position: [1.0, 2.0, 3.0]
      instrument: asmi
  - measure:
      instrument: asmi
      method_kwargs:
        label: baseline
"""

SPEC = {
    "parameters": [{
        "name": "x",
        "minimum": 0.0,
        "maximum": 2.0,
        "step": 0.5,
        "bindings": [{"step_index": 0, "argument": "position.0"}],
    }],
    "sequences": [{
        "name": "label",
        "values": ["a", "b"],
        "bindings": [{"step_index": 1, "argument": "method_kwargs.label"}],
    }],
    "stop": {"max_trials": 2},
}


def test_compile_propagates_parameter_and_sequence_and_keeps_unbound_values():
    rendered = compile_trial(YAML, SPEC, {"x": 1.5}, 1)
    data = yaml.safe_load(rendered)
    assert data["protocol"][0]["move"]["position"] == [1.5, 2.0, 3.0]
    assert data["protocol"][1]["measure"]["method_kwargs"]["label"] == "b"
    assert data["protocol"][0]["move"]["instrument"] == "asmi"


def test_compile_supports_shared_bindings():
    spec = {**SPEC, "parameters": [{**SPEC["parameters"][0], "bindings": [
        {"step_index": 0, "argument": "position.0"},
        {"step_index": 0, "argument": "position.1"},
    ]}]}
    rendered = compile_trial(YAML, spec, {"x": 1.0}, 0)
    assert yaml.safe_load(rendered)["protocol"][0]["move"]["position"] == [1.0, 1.0, 3.0]


@pytest.mark.parametrize("bad", [
    {"step_index": 8, "argument": "position.0"},
    {"step_index": 0, "argument": "position.8"},
    {"step_index": 0, "argument": "position.0.missing"},
])
def test_validate_rejects_invalid_indexes_and_paths(bad):
    spec = {**SPEC, "parameters": [{**SPEC["parameters"][0], "bindings": [bad]}]}
    with pytest.raises(TemplateError):
        validate_template(YAML, spec)


def test_validate_rejects_duplicate_conflicting_binding():
    spec = {**SPEC, "sequences": [{**SPEC["sequences"][0], "bindings": [
        {"step_index": 0, "argument": "position.0"},
    ]}]}
    with pytest.raises(TemplateError, match="conflicting"):
        validate_template(YAML, spec)


def test_validate_rejects_loop_shape_and_short_sequence():
    with pytest.raises(TemplateError):
        validate_template("protocol:\n  - repeat: [move]\n", SPEC)
    short = {**SPEC, "sequences": [{**SPEC["sequences"][0], "values": ["a"]}]}
    with pytest.raises(TemplateError):
        validate_template(YAML, short)


@pytest.mark.parametrize("value", [1.25, -0.5, 2.5, float("nan"), True])
def test_compile_rejects_bad_parameter_values(value):
    with pytest.raises(TemplateError):
        compile_trial(YAML, SPEC, {"x": value}, 0)


def test_extract_objective_supports_nested_lists_and_root_numeric():
    result = {"results": [[{"force": [0.1, 2.75]}]]}
    assert extract_result_objective(result, "results.0.0.force.1") == 2.75
    assert extract_result_objective(3, "") == 3.0


@pytest.mark.parametrize("path", ["missing", "results.0.0.nope"])
def test_extract_objective_rejects_missing_path(path):
    with pytest.raises(TemplateError):
        extract_result_objective({"results": [[{"value": 1}]]}, path)


@pytest.mark.parametrize("value", [True, "1.2", float("inf"), None])
def test_extract_objective_rejects_nonfinite_nonnumeric_values(value):
    with pytest.raises(TemplateError):
        extract_result_objective({"value": value}, "value")
