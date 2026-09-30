"""Shared movement helpers for protocol commands.

CubOS uses a +Z-up deck frame and labware-relative action heights.

* ``measurement_height`` and ``interwell_scan_height`` are *labware-relative*
  offsets above (positive) or below (negative) the labware's surface
  reference Z (the deck-frame Z carried by the resolved well/labware
  coordinate). Both are first-class arguments to the protocol commands
  that use them (``measure`` and ``scan`` for ``measurement_height``,
  ``scan`` for ``interwell_scan_height``). Instruments do not carry them.
* Inter-labware travel uses the gantry's absolute ``safe_z``, exposed on
  ``InstrumentedGantry.safe_z``.

There is no inter-labware helper. Callers compose the motion explicitly:
``context.gantry.move_to_labware`` (lifts to the working-volume ceiling,
travels XY, ends at ``safe_z``) followed by ``context.gantry.move`` to
descend to the per-labware action plane. A raw ``move`` that changes XY
without an explicit ``travel_z`` lifts to the same ceiling first.
"""

from __future__ import annotations

import math
from typing import Any


def _assert_finite_number(value: Any, *, field_name: str, source: str) -> None:
    """Reject non-numeric / non-finite values reaching the height resolver.

    YAML loads `Dict[str, Any]` paths can carry strings, bools, NaN, or inf
    that would otherwise hit late `TypeError`s deep in motion arithmetic.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"{source}: `{field_name}` must be a finite number, got "
            f"{type(value).__name__} {value!r}."
        )
    if not math.isfinite(float(value)):
        raise ValueError(
            f"{source}: `{field_name}` must be a finite number, got {value!r}."
        )


def unpack_xyz(coord: Any) -> tuple[float, float, float]:
    """Extract ``(x, y, z)`` from a ``Coordinate3D``-like object, tuple, or list."""
    if isinstance(coord, (tuple, list)):
        return (coord[0], coord[1], coord[2])
    return (coord.x, coord.y, coord.z)


def engage_at_labware(
    context: Any,
    instrument: str,
    position: str,
    *,
    measurement_height: float,
    command_label: str,
) -> tuple[float, float, float, float]:
    """Travel above *position* at ``safe_z``, descend to the action plane.

    ``measurement_height`` is a labware-relative offset (mm above the
    labware's surface reference Z, i.e. the deck-frame Z carried by the
    resolved coordinate — the well-rim Z for plates, the tip-top Z for
    tip racks, the vial-rim Z for vials). The gantry descends to
    ``coord.z + measurement_height``.

    Returns ``(x, y, well_z, action_z)`` where ``x, y`` is the resolved
    tip XY, ``well_z`` is the labware surface reference (the resolved
    coordinate's Z) and ``action_z`` is that reference plus
    ``measurement_height``. Callers forward the
    well-surface Z to closed-loop instrument methods that compute their
    own descent geometry from a labware-relative ``measurement_height`` /
    ``indentation_limit_height``.

    Raises:
        ValueError: missing instrument, position, or labware on the deck;
            non-finite ``measurement_height``. All command-boundary
            failures surface as ``ValueError`` so callers can wrap them
            into ``ProtocolExecutionError`` consistently.
    """
    _assert_finite_number(
        measurement_height, field_name="measurement_height",
        source=f"command '{command_label}'",
    )
    try:
        context.gantry.instruments[instrument]
    except KeyError as exc:
        raise ValueError(
            f"{command_label}: unknown instrument '{instrument}'."
        ) from exc
    try:
        coord = context.deck.resolve_coordinate(position)
    except (KeyError, AttributeError, ValueError) as exc:
        raise ValueError(
            f"{command_label}: cannot resolve position {position!r} on the "
            f"deck: {exc}"
        ) from exc
    x, y, well_z = unpack_xyz(coord)
    action_z = well_z + measurement_height
    context.gantry.move_to_labware(instrument, coord)
    context.gantry.move(instrument, (x, y, action_z))
    return x, y, well_z, action_z
