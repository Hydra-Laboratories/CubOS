"""Protocol command: scan a well plate with an instrument."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, Dict

from cubos.deck.labware.well_plate import WellPlate

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
from ._movement import _assert_finite_number

if TYPE_CHECKING:
    from ..runtime import ProtocolContext

logger = logging.getLogger(__name__)


def _row_major_key(well_id: str) -> tuple:
    """Sort key for row-major traversal: (row_letter, column_number)."""
    return (well_id[0], int(well_id[1:]))


@protocol_command("scan", summary=_summaries.scan)
def scan(
    context: ProtocolContext,
    plate: str,
    instrument: str,
    method: str,
    measurement_height: float,
    interwell_scan_height: float,
    indentation_limit_height: float | None = None,
    delay_s: float = 0.0,
    method_kwargs: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Scan every well on *plate* using *instrument*'s *method*.

    Iterates wells in row-major order (A1, A2, ..., B1, B2, ...).

    Motion per well, with ``ref_z`` = the well coordinate's deck-frame Z
    (the plate-surface Z, set by calibration):

    * **First well of the plate.** Travel at the gantry's ``safe_z``
      (absolute) → descend to ``ref_z + interwell_scan_height`` →
      descend to ``ref_z + measurement_height`` → act.
    * **Subsequent wells.** Rise to ``ref_z + interwell_scan_height`` at
      the current XY → travel XY at that height → descend to
      ``ref_z + measurement_height`` → act.

    Args:
        context:              Runtime context (instrumented gantry, deck, logger).
        plate:                Deck key of the well plate.
        instrument:           Name of the instrument registered on the gantry.
        method:               Method on the instrument to call per well.
        measurement_height:   Required labware-relative offset for the
                              action plane (mm above the well-surface Z;
                              negative = below).
        interwell_scan_height: Required labware-relative offset for
                              between-wells XY travel (mm above the
                              well-surface Z). Must be at or above
                              ``measurement_height`` in +Z-up.
        indentation_limit_height:
                              ASMI indentation deepest plane, as a
                              labware-relative offset (mm above the
                              well-surface Z; negative = below). Must be
                              at or below ``measurement_height``. The
                              engine resolves this to an absolute
                              ``target_z`` and forwards it to the
                              instrument method.
        delay_s:              Seconds to pause between wells (default 0.0).
        method_kwargs:        Keyword arguments passed per well.

    Returns:
        Mapping of well ID to the result of each method call.
    """
    try:
        plate_obj = context.deck.resolve_labware(plate)
    except KeyError as exc:
        raise ProtocolExecutionError(str(exc)) from exc
    if not isinstance(plate_obj, WellPlate):
        raise ProtocolExecutionError(
            f"scan requires a WellPlate, but '{plate}' is "
            f"{type(plate_obj).__name__}."
        )

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
    callable_method = getattr(instr, method)

    try:
        normalized = normalize_scan_arguments(method_kwargs=method_kwargs)
        _assert_finite_number(
            measurement_height, field_name="measurement_height",
            source="scan",
        )
        _assert_finite_number(
            interwell_scan_height, field_name="interwell_scan_height",
            source="scan",
        )
    except ValueError as exc:
        raise ProtocolExecutionError(str(exc)) from exc

    # ``well_z`` is the plate-surface deck-frame Z, carried on each well's
    # calibrated coordinate (uniform across wells of a single plate).
    well_z = plate_obj.get_well_center("A1").z

    action_z = well_z + measurement_height
    approach_z = well_z + interwell_scan_height

    if interwell_scan_height < measurement_height:
        raise ProtocolExecutionError(
            f"scan: interwell_scan_height ({interwell_scan_height}) is below "
            f"measurement_height ({measurement_height}) for plate "
            f"'{plate}'. Approach must be at or above the action plane."
        )
    detect_surface = surface_detection_enabled(normalized.method_kwargs)
    if indentation_limit_height is not None:
        if detect_surface and indentation_limit_height > 0:
            raise ProtocolExecutionError(
                f"scan: indentation_limit_height ({indentation_limit_height}) "
                "must be at or below 0 when detect_surface is enabled — it "
                "is anchored to the detected sample surface (negative = "
                "into the sample)."
            )
        if not detect_surface and indentation_limit_height > measurement_height:
            raise ProtocolExecutionError(
                f"scan: indentation_limit_height ({indentation_limit_height}) "
                f"is above measurement_height ({measurement_height}) for plate "
                f"'{plate}'. The deepest descent plane must be at or below the "
                "action plane in +Z-up."
            )

    results: Dict[str, Any] = {}
    sorted_wells = sorted(plate_obj.wells, key=_row_major_key)

    # Resolve every persisted well through Deck's canonical target registry.
    # When durable tracking is active, read one authoritative snapshot before
    # any movement so a missing/corrupt state or target cannot produce a
    # partially scanned plate with misleading contents.
    persistence_targets = {}
    if (
        context.fluid_state_id is not None
        or (context.data_store is not None and context.campaign_id is not None)
    ):
        persistence_targets = {
            well_id: resolve_measurement_target(context, f"{plate}.{well_id}")
            for well_id in sorted_wells
        }
    tracked_contents_index = tracked_fluid_contents(
        context,
        persistence_targets.values(),
    )

    for i, well_id in enumerate(sorted_wells):
        if i > 0 and delay_s > 0:
            context.logger.info("Pausing %.1fs between wells", delay_s)
            time.sleep(delay_s)

        well = plate_obj.get_well_center(well_id)
        if i == 0:
            context.gantry.move_to_labware(instrument, well)
            context.gantry.move(instrument, (well.x, well.y, approach_z))
        else:
            context.gantry.move(
                instrument, (well.x, well.y, approach_z), travel_z=approach_z,
            )
        context.gantry.move(instrument, (well.x, well.y, action_z))

        # Use the shared dispatch helper so closed-loop methods get the
        # same gantry-injection + None-gantry guard + finite-number guard
        # as `measure`, instead of scan reimplementing them inline.
        kwargs = inject_runtime_args(
            callable_method, normalized.method_kwargs, context,
            well_z=well_z,
            measurement_height=measurement_height,
            indentation_limit_height=indentation_limit_height,
        )
        result = callable_method(**kwargs)
        results[well_id] = result

        if (
            context.data_store is not None
            and context.campaign_id is not None
            and well_id in persistence_targets
        ):
            try:
                measurement = normalize_measurement(
                    instrument_name=instrument,
                    method_name=method,
                    raw_result=result,
                )
                persistence_target = persistence_targets[well_id]
                if tracked_contents_index is not None:
                    contents = contents_for_target(
                        tracked_contents_index,
                        persistence_target,
                    )
                else:
                    contents = context.data_store.get_contents(
                        context.campaign_id,
                        persistence_target.labware_key,
                        persistence_target.location_id,
                    )
                contents_json = json.dumps(contents) if contents else "[]"
                context.data_store.log_experiment_measurement(
                    campaign_id=context.campaign_id,
                    labware_key=persistence_target.labware_key,
                    labware_name=persistence_target.labware_name,
                    well_id=persistence_target.location_id,
                    contents_json=contents_json,
                    result=measurement,
                )
            except TypeError as exc:
                logger.warning(
                    "Measurement result from %s.%s at well %s is not "
                    "persistable: %s",
                    instrument, method, well_id, exc,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to log measurement for well %s: %s",
                    well_id, exc, exc_info=True,
                )

    if sorted_wells:
        last_well = plate_obj.get_well_center(sorted_wells[-1])
        context.gantry.move(
            instrument, (last_well.x, last_well.y, approach_z),
            travel_z=approach_z,
        )

    return results
