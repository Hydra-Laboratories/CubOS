"""Small, deterministic, dependency-free Bayesian optimization utilities."""
from __future__ import annotations
import itertools
import math
import random
from typing import Any

_GRID_LIMIT = 100_000
_SAMPLE_LIMIT = 20_000
_JITTER = 1e-8


class SearchExhaustedError(ValueError):
    """Raised when every feasible quantized candidate has been observed."""


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)

def _cholesky(matrix: list[list[float]]) -> list[list[float]]:
    n = len(matrix); lower = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            value = matrix[i][j] - sum(lower[i][k] * lower[j][k] for k in range(j))
            lower[i][j] = math.sqrt(max(value, _JITTER)) if i == j else value / lower[j][j]
    return lower

def _solve_cholesky(lower: list[list[float]], right: list[float]) -> list[float]:
    y = []
    for i in range(len(right)):
        y.append((right[i] - sum(lower[i][j] * y[j] for j in range(i))) / lower[i][i])
    result = [0.0] * len(right)
    for i in range(len(right) - 1, -1, -1):
        result[i] = (y[i] - sum(lower[j][i] * result[j] for j in range(i + 1, len(right)))) / lower[i][i]
    return result

def _constraint_ok(point, names, constraint):
    return constraint is None or math.isclose(
        sum(point[names.index(name)] for name in constraint["parameters"]),
        constraint["total"], abs_tol=1e-7)

def _kernel(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return math.exp(-sum((x - y) ** 2 for x, y in zip(a, b)) / (2.0 * 0.3**2))

def _grid(parameter: dict[str, Any]) -> tuple[str, float, float, float, int]:
    if not isinstance(parameter, dict) or "name" not in parameter:
        raise ValueError("each parameter must define name, minimum, maximum, and step")
    name = parameter["name"]
    if not isinstance(name, str) or not name:
        raise ValueError("parameter names must be non-empty strings")
    minimum = _finite(parameter.get("minimum"), f"{name}.minimum")
    maximum = _finite(parameter.get("maximum"), f"{name}.maximum")
    step = _finite(parameter.get("step"), f"{name}.step")
    if maximum < minimum or step <= 0:
        raise ValueError(f"invalid bounds or step for parameter {name!r}")
    return name, minimum, maximum, step, int(math.floor((maximum - minimum) / step + 1e-10)) + 1

def _candidate_points(parameters, constraint, rng):
    names = [p[0] for p in parameters]
    if constraint is not None:
        if not isinstance(constraint, dict) or not isinstance(constraint.get("parameters"), list):
            raise ValueError("sum_constraint.parameters must be a list")
        constrained = constraint["parameters"]
        if not constrained or any(name not in names for name in constrained) or len(set(constrained)) != len(constrained):
            raise ValueError("sum constraint names must be distinct known parameters")
        total = _finite(constraint.get("total"), "sum_constraint.total")
    else:
        constrained, total = [], 0.0
    product = math.prod(p[4] for p in parameters)
    points = []
    if product <= _GRID_LIMIT:
        for indices in itertools.product(*(range(p[4]) for p in parameters)):
            point = tuple(p[1] + i * p[3] for p, i in zip(parameters, indices))
            if _constraint_ok(point, names, constraint):
                points.append(point)
        return points
    seen = set()
    # For a sum constraint, derive one constrained coordinate instead of
    # hoping that uniform sampling lands on an exact floating point sum.
    derived = names.index(constrained[-1]) if constraint else None
    for _ in range(_SAMPLE_LIMIT):
        indices_list = [rng.randrange(p[4]) for p in parameters]
        if derived is not None:
            remainder = total - sum(
                parameters[i][1] + indices_list[i] * parameters[i][3]
                for i, name in enumerate(names) if name in constrained and i != derived
            )
            raw_index = (remainder - parameters[derived][1]) / parameters[derived][3]
            rounded = round(raw_index)
            if abs(raw_index - rounded) > 1e-7 or not 0 <= rounded < parameters[derived][4]:
                continue
            indices_list[derived] = rounded
        indices = tuple(indices_list)
        if indices in seen: continue
        seen.add(indices)
        point = tuple(p[1] + i * p[3] for p, i in zip(parameters, indices))
        if _constraint_ok(point, names, constraint):
            points.append(point)
    return points

def suggest(parameters: list[dict], observations: list[dict], *, method: str = "ei", initial_trials: int = 3,
            exploration: float = 0.05, seed: int = 7, direction: str = "minimize", sum_constraint: dict | None = None) -> dict[str, float]:
    """Suggest one unobserved quantized point using random, EI, or LCB."""
    if not isinstance(parameters, list) or not parameters or len(parameters) > 8: raise ValueError("parameters must contain between 1 and 8 entries")
    parsed = [_grid(parameter) for parameter in parameters]; names = [p[0] for p in parsed]
    if len(set(names)) != len(names): raise ValueError("parameter names must be unique")
    if not isinstance(observations, list) or len(observations) > 100: raise ValueError("observations must contain at most 100 trials")
    if method not in {"ei", "lcb", "random"}: raise ValueError("method must be 'ei', 'lcb', or 'random'")
    if direction not in {"minimize", "maximize"}: raise ValueError("direction must be 'minimize' or 'maximize'")
    if isinstance(initial_trials, bool) or not isinstance(initial_trials, int) or initial_trials < 0: raise ValueError("initial_trials must be a non-negative integer")
    exploration = _finite(exploration, "exploration")
    if exploration < 0: raise ValueError("exploration must be non-negative")
    rng = random.Random(seed); all_points = _candidate_points(parsed, sum_constraint, rng)
    if not all_points: raise ValueError("no feasible candidates for the supplied bounds and sum constraint")
    observed = []; values = []; point_set = set(all_points)
    for observation in observations:
        if not isinstance(observation, dict) or not isinstance(observation.get("parameters"), dict): raise ValueError("each observation must contain parameters and objective")
        raw = observation["parameters"]
        if set(raw) != set(names): raise ValueError("observation parameters must match the declared parameters")
        supplied = tuple(_finite(raw[name], f"observation parameter {name}") for name in names)
        indices = []
        for value, parameter in zip(supplied, parsed):
            index = round((value - parameter[1]) / parameter[3])
            if not 0 <= index < parameter[4] or not math.isclose(value, parameter[1] + index * parameter[3], abs_tol=1e-7):
                raise ValueError("observation parameter is outside the quantized feasible grid")
            indices.append(index)
        point = tuple(parameter[1] + index * parameter[3] for parameter, index in zip(parsed, indices))
        if not _constraint_ok(point, names, sum_constraint): raise ValueError("observation violates the sum constraint")
        if point in observed: raise ValueError("duplicate observations are not allowed")
        observed.append(point); values.append(_finite(observation.get("objective"), "observation.objective"))
    observed_set = set(observed); remaining = [p for p in all_points if p not in observed_set]
    if not remaining:
        if math.prod(p[4] for p in parsed) <= _GRID_LIMIT:
            raise SearchExhaustedError("all candidate points have been evaluated")
        raise ValueError("sampled candidate pool contains no unobserved point; retry with another seed")
    if method == "random" or len(observed) < initial_trials or not observed:
        return dict(zip(names, rng.choice(remaining)))
    spans = [(p[2] - p[1]) or 1.0 for p in parsed]
    xs = [tuple((x - p[1]) / span for x, p, span in zip(point, parsed, spans)) for point in observed]
    sign = 1.0 if direction == "minimize" else -1.0; transformed = [sign * value for value in values]
    mean = sum(transformed) / len(transformed); scale = math.sqrt(sum((v - mean) ** 2 for v in transformed) / len(transformed)) or 1.0
    ys = [(v - mean) / scale for v in transformed]
    lower = _cholesky([[_kernel(a, b) + (_JITTER if i == j else 0.0) for j, b in enumerate(xs)] for i, a in enumerate(xs)])
    alpha = _solve_cholesky(lower, ys); best = min(ys); scored = []
    score_points = remaining if len(remaining) <= 1024 else rng.sample(remaining, 1024)
    for point in score_points:
        x = tuple((value - p[1]) / span for value, p, span in zip(point, parsed, spans)); correlations = [_kernel(x, training) for training in xs]
        prediction = sum(k * a for k, a in zip(correlations, alpha)); projected = _solve_cholesky(lower, correlations)
        sigma = math.sqrt(max(1e-12, 1.0 - sum(value * value for value in projected)))
        if method == "ei":
            improvement = best - prediction - exploration; z = improvement / sigma
            score = improvement * 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))) + sigma * math.exp(-z * z / 2.0) / math.sqrt(2.0 * math.pi)
        else: score = -prediction + exploration * sigma
        scored.append((score, point))
    point = max(scored, key=lambda item: (item[0], tuple(-value for value in item[1])))[1]
    return dict(zip(names, point))
