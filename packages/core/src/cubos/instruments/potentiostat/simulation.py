"""Vendor-agnostic synthetic traces for offline potentiostat runs.

Uses stdlib ``math`` + ``random`` so drivers (and therefore the package) can
be imported without numpy. Trace sizes here are small (seconds of data at
10-100 ms sampling), so list/tuple math is perfectly adequate.

Each ``simulate_*`` function is deterministic for a given ``rng`` state;
drivers hold a fixed-seed ``random.Random`` so offline results are stable
across runs of a freshly-constructed driver.
"""

from __future__ import annotations

import math
import random
from typing import Any, Mapping

from cubos.instruments.potentiostat.models import (
    CAParams,
    CAResult,
    CPParams,
    CPResult,
    CVParams,
    CVResult,
    OCPParams,
    OCPResult,
)


def simulate_CV(
    params: CVParams,
    rng: random.Random,
    vendor: str,
    metadata: Mapping[str, Any],
) -> CVResult:
    # One full cycle: start → v1 → v2 → end. Span is distance traversed.
    voltage_span = (
        abs(params.vertex1_V - params.start_V)
        + abs(params.vertex2_V - params.vertex1_V)
        + abs(params.end_V - params.vertex2_V)
    )
    cycle_duration = voltage_span / params.scan_rate_V_per_s
    samples_per_cycle = max(
        int(math.ceil(cycle_duration / params.sampling_interval_s)), 2
    )

    voltages: list[float] = []
    time_points: list[float] = []
    for cycle_index in range(params.cycles):
        sweep = _triangular_sweep(
            params.start_V,
            params.vertex1_V,
            params.vertex2_V,
            params.end_V,
            samples_per_cycle,
        )
        voltages.extend(sweep)
        cycle_start_time = cycle_index * cycle_duration
        time_points.extend(
            cycle_start_time
            + sample_index * cycle_duration / samples_per_cycle
            for sample_index in range(samples_per_cycle)
        )

    # Simple Butler-Volmer-ish synthetic current: scaled sinh around 0V.
    currents = [
        1e-6 * math.sinh(voltage / 0.05) + rng.gauss(0.0, 5e-9)
        for voltage in voltages
    ]

    return CVResult(
        time_s=tuple(time_points),
        voltage_v=tuple(voltages),
        current_a=tuple(currents),
        scan_rate_v_s=params.scan_rate_V_per_s,
        step_size_v=params.scan_rate_V_per_s * params.sampling_interval_s,
        cycles=params.cycles,
        vendor=vendor,
        metadata=metadata,
    )


def simulate_OCP(
    params: OCPParams,
    rng: random.Random,
    vendor: str,
    metadata: Mapping[str, Any],
) -> OCPResult:
    sample_count = max(
        int(math.ceil(params.duration_s / params.sampling_interval_s)), 1
    )
    time_points = tuple(
        sample_index * params.duration_s / sample_count
        for sample_index in range(sample_count)
    )
    # Slow exponential settle toward a stable OCV of ~0.35 V.
    decay = max(params.duration_s / 4.0, 1e-6)
    voltages = tuple(
        0.35 + 0.05 * math.exp(-t / decay) + rng.gauss(0.0, 1e-4)
        for t in time_points
    )
    return OCPResult(
        time_s=time_points,
        voltage_v=voltages,
        sample_period_s=params.sampling_interval_s,
        duration_s=params.duration_s,
        vendor=vendor,
        metadata=metadata,
    )


def simulate_CA(
    params: CAParams,
    rng: random.Random,
    vendor: str,
    metadata: Mapping[str, Any],
) -> CAResult:
    sample_count = max(
        int(math.ceil(params.duration_s / params.sampling_interval_s)), 1
    )
    time_points = tuple(
        sample_index * params.duration_s / sample_count
        for sample_index in range(sample_count)
    )
    # Cottrell-like t^-1/2 decay, clipped near t=0.
    currents = tuple(
        1e-5 / math.sqrt(max(t, params.sampling_interval_s))
        + rng.gauss(0.0, 1e-8)
        for t in time_points
    )
    voltages = tuple(params.potential_V for _ in range(sample_count))
    return CAResult(
        time_s=time_points,
        voltage_v=voltages,
        current_a=currents,
        sample_period_s=params.sampling_interval_s,
        duration_s=params.duration_s,
        step_potential_v=params.potential_V,
        vendor=vendor,
        metadata=metadata,
    )


def simulate_CP(
    params: CPParams,
    rng: random.Random,
    vendor: str,
    metadata: Mapping[str, Any],
) -> CPResult:
    sample_count = max(
        int(math.ceil(params.duration_s / params.sampling_interval_s)), 1
    )
    time_points = tuple(
        sample_index * params.duration_s / sample_count
        for sample_index in range(sample_count)
    )
    currents = tuple(params.current_A for _ in range(sample_count))
    # Faradaic-ish drift on the working electrode potential.
    voltages = tuple(
        0.1 + 0.002 * t + rng.gauss(0.0, 1e-4) for t in time_points
    )
    return CPResult(
        time_s=time_points,
        voltage_v=voltages,
        current_a=currents,
        sample_period_s=params.sampling_interval_s,
        duration_s=params.duration_s,
        step_current_a=params.current_A,
        vendor=vendor,
        metadata=metadata,
    )


def _triangular_sweep(
    start_voltage: float,
    first_vertex_voltage: float,
    second_vertex_voltage: float,
    end_voltage: float,
    sample_count: int,
) -> list[float]:
    """Distribute samples across three linear legs weighted by span.

    Legs are endpoint-exclusive except the final one, which closes the
    sweep at ``end_voltage``. Returned list is exactly ``sample_count`` long.
    """
    voltage_legs = [
        (start_voltage, first_vertex_voltage),
        (first_vertex_voltage, second_vertex_voltage),
        (second_vertex_voltage, end_voltage),
    ]
    leg_lengths = [abs(end - start) for start, end in voltage_legs]
    total_length = sum(leg_lengths) or 1.0
    samples_by_leg = [
        max(int(round(sample_count * (leg_length / total_length))), 1)
        for leg_length in leg_lengths
    ]
    samples_by_leg[-1] = max(
        sample_count - (samples_by_leg[0] + samples_by_leg[1]), 1
    )

    voltages: list[float] = []
    for leg_index, ((start, end), leg_sample_count) in enumerate(
        zip(voltage_legs, samples_by_leg)
    ):
        if leg_index < 2:
            voltages.extend(
                start + (end - start) * sample_index / leg_sample_count
                for sample_index in range(leg_sample_count)
            )
        else:
            if leg_sample_count == 1:
                voltages.append(end)
            else:
                voltages.extend(
                    start
                    + (end - start) * sample_index / (leg_sample_count - 1)
                    for sample_index in range(leg_sample_count)
                )
    return voltages[:sample_count]
