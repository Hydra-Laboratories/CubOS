import math

import pytest

from cubos.optimization import SearchExhaustedError, suggest
from cubos.optimization.optimizer import _cholesky, _solve_cholesky


P = [{"name": "x", "minimum": 0, "maximum": 1, "step": 0.25},
     {"name": "y", "minimum": 0, "maximum": 1, "step": 0.5}]


def test_random_is_bounded_quantized_and_deterministic():
    first = suggest(P, [], method="random", seed=12)
    assert first == suggest(P, [], method="random", seed=12)
    assert first["x"] in {0.0, .25, .5, .75, 1.0}
    assert first["y"] in {0.0, .5, 1.0}


def test_feedback_changes_gp_proposal_and_direction():
    observations = [
        {"parameters": {"x": 0, "y": 0}, "objective": 0.0},
        {"parameters": {"x": 1, "y": 1}, "objective": 10.0},
        {"parameters": {"x": .5, "y": .5}, "objective": 4.0},
    ]
    ei = suggest(P, observations, initial_trials=0, exploration=0, seed=4)
    maximize = suggest(P, observations, initial_trials=0, exploration=0, seed=4, direction="maximize")
    assert ei != maximize
    changed = [dict(item) for item in observations]
    changed[1] = {"parameters": {"x": 1, "y": 1}, "objective": -10.0}
    assert suggest(P, changed, initial_trials=0, exploration=0, seed=4) != ei


def test_lcb_is_available_and_sum_constraint_is_respected():
    result = suggest(P, [{"parameters": {"x": 0, "y": 1}, "objective": 2}], method="lcb", initial_trials=0, sum_constraint={"parameters": ["x", "y"], "total": 1}, seed=2)
    assert math.isclose(result["x"] + result["y"], 1)


def test_duplicates_exhaustion_and_invalid_data_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        suggest([{"name": "x", "minimum": 0, "maximum": 1, "step": 1}], [
            {"parameters": {"x": 0}, "objective": 1},
            {"parameters": {"x": 0}, "objective": 2}])
    with pytest.raises(SearchExhaustedError, match="all candidate"):
        suggest([{"name": "x", "minimum": 0, "maximum": 1, "step": 1}], [
            {"parameters": {"x": 0}, "objective": 1}, {"parameters": {"x": 1}, "objective": 2}])
    with pytest.raises(ValueError):
        suggest([{"name": "x", "minimum": 0, "maximum": 1, "step": 1}], [{"parameters": {"x": 0}, "objective": float("nan")}])
    with pytest.raises(ValueError, match="no feasible"):
        suggest(P, [], sum_constraint={"parameters": ["x", "y"], "total": 9})


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), -float("inf")])
def test_non_finite_and_boolean_values_are_rejected(bad):
    with pytest.raises(ValueError):
        suggest([{"name": "x", "minimum": bad, "maximum": 1, "step": 1}], [])
    with pytest.raises(ValueError):
        suggest([{"name": "x", "minimum": 0, "maximum": 1, "step": 1}], [
            {"parameters": {"x": 0}, "objective": bad}])


def test_large_sum_grid_finds_feasible_points_with_bounded_sampling():
    parameters = [
        {"name": "x", "minimum": 0, "maximum": 100_000, "step": 1},
        {"name": "y", "minimum": 0, "maximum": 100_000, "step": 1},
    ]
    point = suggest(parameters, [], sum_constraint={"parameters": ["x", "y"], "total": 100}, seed=1)
    assert point["x"] + point["y"] == 100


def test_dimension_and_trial_limits_are_bounded():
    eight = [{"name": str(i), "minimum": 0, "maximum": 1, "step": 1} for i in range(8)]
    assert set(suggest(eight, [])) == {str(i) for i in range(8)}
    with pytest.raises(ValueError, match="8"):
        suggest(eight + [{"name": "extra", "minimum": 0, "maximum": 1, "step": 1}], [])
    observations = [{"parameters": {"x": 0}, "objective": i} for i in range(101)]
    with pytest.raises(ValueError, match="100"):
        suggest([{"name": "x", "minimum": 0, "maximum": 200, "step": 1}], observations)


def test_cholesky_posterior_solve_uses_forward_and_backward_substitution():
    lower = _cholesky([[1.0, 0.5], [0.5, 1.0]])
    result = _solve_cholesky(lower, [0.5, 0.5])
    assert result == pytest.approx([1 / 3, 1 / 3])


def test_decimal_grid_observation_is_canonicalized():
    parameter = [{"name": "x", "minimum": 0, "maximum": 0.9, "step": 0.1}]
    result = suggest(parameter, [{"parameters": {"x": 0.3}, "objective": 1.0}], initial_trials=0)
    assert result["x"] != 0.3
