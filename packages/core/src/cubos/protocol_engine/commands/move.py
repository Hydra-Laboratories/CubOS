"""Protocol command: move."""

from typing import Any, TYPE_CHECKING

from ..errors import ProtocolExecutionError
from ..registry import protocol_command
from . import _summaries

if TYPE_CHECKING:
    from ..runtime import ProtocolContext


@protocol_command("move", summary=_summaries.move)
def move(
    context: "ProtocolContext",
    instrument: str,
    position: Any,
    travel_z: float | None = None,
) -> None:
    """Move *instrument* to *position*.

    Position resolution:
        1. ``[x, y, z]`` list/tuple → raw gantry move to the literal
           coordinates (applies only the instrument's mounting offsets).
        2. Named position from the protocol YAML ``positions:`` block →
           same raw move treatment.
        3. Deck target string (e.g. ``"plate_1.A1"``) → safe
           ``move_to_labware`` that retracts (if needed), travels XY at
           the gantry's absolute ``safe_z``, and ends above the target
           (no descent — use ``measure``/``aspirate``/etc. to engage).

    Args:
        context:    Runtime context (instrumented gantry, deck, logger).
        instrument: Name of the instrument registered on the gantry.
        position:   Named position, [x, y, z] list, or deck target string.
        travel_z:   Optional raw transit Z for literal/named XYZ moves.
                    When set, the gantry first moves Z to ``travel_z`` at
                    the current XY, then moves XY at that Z, then finishes
                    at ``position``. When omitted and the move changes XY,
                    the gantry lifts to the working-volume ceiling first.
    """
    if isinstance(position, (list, tuple)):
        target = tuple(position)
        context.logger.info(
            "move: %s -> %s (raw, travel_z=%s)", instrument, target, travel_z,
        )
        if travel_z is None:
            context.gantry.move(instrument, target)
        else:
            context.gantry.move(instrument, target, travel_z=travel_z)
        return
    if isinstance(position, str) and position in context.positions:
        target = tuple(context.positions[position])
        context.logger.info(
            "move: %s -> %s (named: %s, travel_z=%s)",
            instrument,
            target,
            position,
            travel_z,
        )
        if travel_z is None:
            context.gantry.move(instrument, target)
        else:
            context.gantry.move(instrument, target, travel_z=travel_z)
        return

    # Deck target — route through move_to_labware so the gantry's
    # absolute safe_z is applied consistently with measure/aspirate at
    # the same position.
    # If resolution fails AND the string doesn't look like a deck target
    # (no '.'), surface a clearer error that lists both namespaces so a
    # typo in a named position doesn't masquerade as a missing labware.
    try:
        coord = context.deck.resolve_coordinate(position)
    except Exception as exc:
        if isinstance(position, str) and "." not in position:
            named = sorted(context.positions.keys()) if context.positions else []
            raise ProtocolExecutionError(
                f"move: {position!r} is not a named position "
                f"({named or 'none defined'}) and is not a resolvable deck "
                f"target: {exc}"
            ) from exc
        raise
    if travel_z is not None:
        raise ProtocolExecutionError(
            "move: travel_z is only supported for literal/named XYZ targets, "
            "not deck targets. Deck targets already use move_to_labware() "
            "with instrument safe-approach behavior."
        )
    context.logger.info("move: %s -> %s (labware: safe approach)", instrument, position)
    context.gantry.move_to_labware(instrument, coord)
