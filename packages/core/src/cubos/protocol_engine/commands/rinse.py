"""Protocol command for rinsing a potentiostat probe in a vial."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from cubos.deck.labware.vial import Vial
from cubos.instruments.potentiostat.interface import PotentiostatInstrument

from ..errors import ProtocolExecutionError
from ..registry import protocol_command
from . import _summaries
from ._cap_preflight import require_uncapped
from ._movement import _assert_finite_number, engage_at_labware

if TYPE_CHECKING:
    from ..runtime import ProtocolContext


@protocol_command("rinse", summary=_summaries.rinse)
def rinse(
    context: ProtocolContext,
    instrument: str,
    vial: str,
    measurement_height: float,
) -> None:
    """Dip a potentiostat probe into a vial three times.

    ``measurement_height`` is relative to the vial rim and must be negative
    so every dip is physically below the calibrated rim. Each dip approaches
    from ``safe_z``, descends once, and retracts once before the next dip.
    """
    try:
        instr = context.gantry.instruments[instrument]
    except KeyError as exc:
        raise ProtocolExecutionError(
            f"rinse: unknown instrument '{instrument}'."
        ) from exc
    if not isinstance(instr, PotentiostatInstrument):
        raise ProtocolExecutionError(
            f"rinse: instrument '{instrument}' is not a potentiostat."
        )
    if (
        isinstance(measurement_height, bool)
        or not isinstance(measurement_height, (int, float))
        or not math.isfinite(float(measurement_height))
        or measurement_height >= 0
    ):
        raise ProtocolExecutionError(
            "rinse: measurement_height must be a finite negative number "
            f"(below the vial rim), got {measurement_height!r}."
        )
    try:
        target = context.deck.resolve_labware(vial)
    except (KeyError, ValueError, AttributeError) as exc:
        raise ProtocolExecutionError(
            f"rinse: cannot resolve vial {vial!r} on the deck: {exc}"
        ) from exc
    if not isinstance(target, Vial):
        raise ProtocolExecutionError(
            f"rinse: target {vial!r} must resolve to a single Vial, "
            "not an entire holder, grid, or other labware."
        )
    if context.fluid_state_id is not None:
        require_uncapped(context, [vial], command_label="rinse")
    elif target.capped is True:
        raise ProtocolExecutionError(
            f"rinse: vial {vial!r} is capped; decap it before rinsing."
        )
    if context.gantry.safe_z is None:
        raise ProtocolExecutionError(
            "rinse: gantry safe_z must be configured for probe retraction."
        )
    try:
        _assert_finite_number(context.gantry.safe_z, field_name="safe_z", source="rinse")
        _assert_finite_number(target.location.z, field_name="vial rim Z", source="rinse")
    except ValueError as exc:
        raise ProtocolExecutionError(str(exc)) from exc
    if target.location.z >= context.gantry.safe_z:
        raise ProtocolExecutionError(
            "rinse: safe_z must be above the vial rim so each withdrawal clears the vial."
        )

    for _ in range(3):
        try:
            x, y, _, action_z = engage_at_labware(
                context,
                instrument,
                vial,
                measurement_height=measurement_height,
                command_label="rinse",
            )
            context.gantry.move(instrument, (x, y, context.gantry.safe_z))
        except ValueError as exc:
            raise ProtocolExecutionError(str(exc)) from exc
        context.logger.info(
            "rinse: %s dipped in %s at z=%.3f", instrument, vial, action_z,
        )
