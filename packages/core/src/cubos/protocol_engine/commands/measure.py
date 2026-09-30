"""Protocol command: measure with an instrument at a deck position."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, TYPE_CHECKING

from ..errors import ProtocolExecutionError
from ..measurements import normalize_measurement
from ..registry import protocol_command
from . import _summaries
from ..scan_args import normalize_scan_arguments, surface_detection_enabled
from ._dispatch import inject_runtime_args
from ._fluid_contents import (
    contents_for_target,
    resolve_measurement_target,
    tracked_fluid_contents,
)
from ._movement import _assert_finite_number, engage_at_labware

if TYPE_CHECKING:
    from ..runtime import ProtocolContext

logger = logging.getLogger(__name__)


@protocol_command("measure", summary=_summaries.measure)
def measure(
    context: ProtocolContext,
    instrument: str,
    position: str,
    measurement_height: float,
    method: str = "measure",
    indentation_limit_height: float | None = None,
    method_kwargs: Dict[str, Any] = {},
) -> Any:
    """Measure at a deck position using *instrument*.

    Motion:
      1. Travel at the gantry's ``safe_z`` (absolute) to above the target.
      2. Descend straight down to ``well.z + measurement_height``.
      3. Call ``instrument.method(**method_kwargs)``.

    ``measurement_height`` is a required first-class argument: a
    labware-relative offset (mm above the well/labware calibrated surface Z;
    negative = below).

    ``indentation_limit_height`` is optional and only meaningful for
    closed-loop methods that declare it (e.g. ``ASMI.indentation``).
    Same semantics as on ``scan``: a signed labware-relative offset for
    the deepest descent plane; must be at or below ``measurement_height``.
    Lets ``measure`` drive a single-well indentation without iterating
    a plate.
    """
    if instrument not in context.gantry.instruments:
        raise ProtocolExecutionError(
            f"Unknown instrument '{instrument}'. "
            f"Available: {', '.join(sorted(context.gantry.instruments.keys()))}"
        )
    instr = context.gantry.instruments[instrument]

    if not hasattr(instr, method):
        raise ProtocolExecutionError(
            f"Instrument '{instrument}' has no method '{method}'."
        )

    # Reuse scan's legacy / first-class kwarg rejection so a user porting
    # an old config that put ``indentation_limit`` / ``z_limit`` /
    # ``safe_approach_height`` / ``measurement_height`` inside
    # ``method_kwargs`` gets the same rename hint here as on ``scan``,
    # rather than a generic Python TypeError or a silent overwrite.
    try:
        normalized = normalize_scan_arguments(method_kwargs=method_kwargs)
        _assert_finite_number(
            measurement_height,
            field_name="measurement_height",
            source="measure",
        )
    except ValueError as exc:
        raise ProtocolExecutionError(str(exc)) from exc

    detect_surface = surface_detection_enabled(normalized.method_kwargs)
    if indentation_limit_height is not None:
        if detect_surface and indentation_limit_height > 0:
            raise ProtocolExecutionError(
                "measure: indentation_limit_height "
                f"({indentation_limit_height}) must be at or below 0 when "
                "detect_surface is enabled — it is anchored to the detected "
                "sample surface (negative = into the sample)."
            )
        if not detect_surface and indentation_limit_height > measurement_height:
            raise ProtocolExecutionError(
                f"measure: indentation_limit_height ({indentation_limit_height}) "
                f"is above measurement_height ({measurement_height}). The "
                "deepest descent plane must be at or below the action plane "
                "in +Z-up."
            )

    # Durable state is authoritative for a tracked run. Resolve and snapshot
    # it before any movement or instrument call so corrupt, missing, or
    # campaign-mismatched state fails closed instead of producing an
    # unpersisted physical measurement.
    persistence_target = None
    tracked_contents_index = None
    if context.fluid_state_id is not None:
        persistence_target = resolve_measurement_target(context, position)
        tracked_contents_index = tracked_fluid_contents(
            context,
            [persistence_target],
        )

    callable_method = getattr(instr, method)
    try:
        kwargs_probe = inject_runtime_args(
            callable_method, normalized.method_kwargs, context,
            well_z=0.0,
            measurement_height=measurement_height,
            indentation_limit_height=indentation_limit_height,
        )
    except ProtocolExecutionError:
        raise

    try:
        _, _, well_z, action_z = engage_at_labware(
            context, instrument, position,
            measurement_height=measurement_height,
            command_label="measure",
        )
    except ValueError as exc:
        raise ProtocolExecutionError(str(exc)) from exc

    context.logger.info(
        "measure: %s.%s(%s) at %s — action_z=%.3f",
        instrument, method, method_kwargs, position, action_z,
    )

    # Forward the labware-surface reference Z plus the user's labware-relative
    # offset so closed-loop instrument methods can resolve their own action /
    # target Z values. Open-loop methods that don't declare these parameters
    # receive only the YAML-supplied ``method_kwargs``.
    kwargs = inject_runtime_args(
        callable_method, normalized.method_kwargs, context,
        well_z=well_z,
        measurement_height=measurement_height,
        indentation_limit_height=indentation_limit_height,
    )
    if kwargs == kwargs_probe and "well_z" not in kwargs:
        kwargs = kwargs_probe
    result = callable_method(**kwargs)

    if context.data_store is not None and context.campaign_id is not None:
        try:
            measurement = normalize_measurement(
                instrument_name=instrument,
                method_name=method,
                raw_result=result,
            )
            target = persistence_target or resolve_measurement_target(
                context,
                position,
            )
            if tracked_contents_index is not None:
                contents = contents_for_target(tracked_contents_index, target)
            else:
                contents = context.data_store.get_contents(
                    context.campaign_id, target.labware_key, target.location_id,
                )
            contents_json = json.dumps(contents) if contents else "[]"
            context.data_store.log_experiment_measurement(
                campaign_id=context.campaign_id,
                labware_key=target.labware_key,
                labware_name=target.labware_name,
                well_id=target.location_id,
                contents_json=contents_json,
                result=measurement,
            )
        except TypeError as exc:
            logger.warning(
                "Measurement result from %s.%s at position %s is not "
                "persistable: %s",
                instrument, method, position, exc,
            )
        except Exception as exc:
            logger.warning(
                "Failed to log measurement for position %s: %s",
                position, exc, exc_info=True,
            )

    return result
