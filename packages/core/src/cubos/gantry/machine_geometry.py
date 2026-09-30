"""Built-in machine geometry for supported gantry families."""

from __future__ import annotations

from dataclasses import dataclass

from .gantry_config import GantryConfig, GantryType, OriginPolicy


@dataclass(frozen=True)
class FixedStructureBox:
    """Fixed AABB machine structure.

    Coordinates are in whichever signed frame the caller requested them in
    (CubOS deck-frame or home-frame); see :func:`fixed_structures_for_gantry`.
    """

    name: str
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_min: float
    z_max: float

    def contains(self, x: float, y: float, z: float) -> bool:
        return (
            self.x_min <= x <= self.x_max
            and self.y_min <= y <= self.y_max
            and self.z_min <= z <= self.z_max
        )


def _translate(box: FixedStructureBox, offset: tuple[float, float, float]) -> FixedStructureBox:
    """Return a copy of ``box`` shifted by ``offset`` on every axis."""
    dx, dy, dz = offset
    return FixedStructureBox(
        name=box.name,
        x_min=box.x_min + dx,
        x_max=box.x_max + dx,
        y_min=box.y_min + dy,
        y_max=box.y_max + dy,
        z_min=box.z_min + dz,
        z_max=box.z_max + dz,
    )


# Cub XL reference travel span (x, y, z): the deck-to-home offset assumed by
# the deck-frame constants below when a config does not carry its own
# calibrated max_travel_* GRBL settings.
_CUB_XL_REFERENCE_TRAVEL_MM = (540.0, 300.0, 100.0)

# Built-in structures are defined in deck-frame absolute coordinates, exactly
# as before origin_policy existed.
CUB_XL_RIGHT_X_MAX_RAIL = FixedStructureBox(
    name="Cub XL right X-max rail",
    x_min=480.0,
    x_max=540.0,
    y_min=0.0,
    y_max=300.0,
    z_min=0.0,
    z_max=80.0,
)

_FIXED_STRUCTURES_BY_GANTRY_TYPE: dict[GantryType, tuple[FixedStructureBox, ...]] = {
    GantryType.CUB_XL: (CUB_XL_RIGHT_X_MAX_RAIL,),
}


def _travel_span_mm(gantry: GantryConfig) -> tuple[float, float, float]:
    """Return the deck-to-home offset for a config, one span per axis.

    The deck-origin calibration assigns WPos zero at the physical
    front-left-bottom point and programs the measured spans into the GRBL
    $130/$131/$132 soft limits, so the homed corner's deck-frame position is
    exactly the machine's max_travel settings. Configs that do not carry
    calibrated max_travel_* values fall back to the gantry family's reference
    travel.
    """
    settings = gantry.expected_grbl_settings or {}
    travel = tuple(settings.get(code) for code in ("$130", "$131", "$132"))
    if all(value is not None for value in travel):
        return (float(travel[0]), float(travel[1]), float(travel[2]))
    return _CUB_XL_REFERENCE_TRAVEL_MM


def fixed_structures_for_gantry_type(
    gantry_type: GantryType | str,
) -> tuple[FixedStructureBox, ...]:
    """Return built-in fixed machine structures in deck-frame coordinates.

    Type-only lookup; always deck-frame regardless of any config's
    ``origin_policy``. Use :func:`fixed_structures_for_gantry` when a loaded
    gantry config is available so the result is expressed in that config's
    active frame.
    """
    return _FIXED_STRUCTURES_BY_GANTRY_TYPE.get(GantryType(gantry_type), ())


def fixed_structures_for_gantry(
    gantry: GantryConfig | None,
) -> tuple[FixedStructureBox, ...]:
    """Return built-in fixed machine structures for a loaded gantry config.

    Coordinates are expressed in the config's active frame: deck-frame for
    ``origin_policy: deck_origin`` (the default), home-frame for
    ``origin_policy: home_origin``. Home-frame boxes are the deck-frame
    constants translated by the machine's own deck-to-home travel span (see
    :func:`_travel_span_mm`); a box that lies beyond the machine's travel is
    unreachable and simply never intersects motion.
    """
    if gantry is None:
        return ()
    deck_frame = fixed_structures_for_gantry_type(gantry.gantry_type)
    if OriginPolicy(gantry.origin_policy) is not OriginPolicy.HOME_ORIGIN:
        return deck_frame
    span_x, span_y, span_z = _travel_span_mm(gantry)
    return tuple(
        _translate(box, (-span_x, -span_y, -span_z)) for box in deck_frame
    )
