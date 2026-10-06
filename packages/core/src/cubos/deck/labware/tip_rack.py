from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .holder import HolderLabware
from .labware import Coordinate3D, Labware

if TYPE_CHECKING:
    from cubos.deck.deck import Deck


DEFAULT_TIP_LENGTH_MM = 59.3


class SideExit(BaseModel):
    """Lift from engagement, then leave an open rack along deck-frame X."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    lift_mm: float = Field(..., ge=30, description="Vertical lift before X withdrawal (mm).")
    exit_x: float = Field(..., description="Absolute tool X beyond the open rack edge, including clearance.")


class TipRackResolutionError(ValueError):
    """Raised when a target string does not resolve to a TipRack slot."""


def resolve_tip_rack_slot(
    deck: "Deck", target: Any,
) -> tuple["TipRack", str | None]:
    """Resolve ``"tips.A1"`` or ``"holder.tips.A1"`` to ``(rack, tip_id)``.

    Returns ``(rack, None)`` when *target* names a TipRack without a slot
    (``"tips"``). Raises ``TipRackResolutionError`` for non-string targets,
    unresolved labware paths, or paths that resolve to non-TipRack labware.
    Only ``KeyError`` from ``Deck.resolve_labware`` is caught — other
    exceptions (``AttributeError``, ``ValueError``) indicate genuine bugs
    and propagate.
    """
    if not isinstance(target, str):
        raise TipRackResolutionError(
            f"tip-rack target must be a string, got {type(target).__name__}."
        )
    if "." in target:
        rack_path, tip_id = target.rsplit(".", 1)
    else:
        rack_path, tip_id = target, None
    try:
        labware = deck.resolve_labware(rack_path)
    except KeyError as exc:
        raise TipRackResolutionError(
            f"tip-rack path {rack_path!r} is not on the deck."
        ) from exc
    if not isinstance(labware, TipRack):
        raise TipRackResolutionError(
            f"target {target!r} resolves to {type(labware).__name__}, "
            "not a TipRack."
        )
    return labware, tip_id


class TipRack(HolderLabware):
    """Tip rack labware with addressable pickup positions and per-tip presence tracking.

    Inherits the bounding-box/seat geometry from :class:`HolderLabware`, so a
    tip rack is treated like any other physical deck fixture. Additional
    fields capture the per-tip layout:

    * ``rows`` / ``columns`` — rack layout (used only for validation).
    * ``pickup_z`` / ``drop_z`` — default pickup and discard Z.
    * ``tip_length`` — extra active tool depth added after pickup.
    * ``tips`` — mapping from tip ID (``"A1"``, ``"A2"``...) to absolute XYZ.
    * ``tip_present`` — per-tip boolean flag; ``True`` = loaded, ``False`` =
      empty/consumed. Auto-initializes to all-True from the ``tips`` keys
      when left empty.

    ``location`` and ``length`` / ``width`` / ``height`` are derived
    from the ``tips`` dict if they are not explicitly provided, so callers
    can construct a rack with tips alone and still get a valid
    :class:`HolderLabware`.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model_name: str = "tip_rack"
    rows: int = Field(..., gt=0, le=26, description="Number of rack rows.")
    columns: int = Field(..., gt=0, description="Number of rack columns.")
    pickup_z: float = Field(
        ...,
        description=(
            "Default pickup Z for each tip, in the gantry config's active "
            "frame (absolute deck-frame or home-frame, signed)."
        ),
    )
    drop_z: float | None = Field(
        default=None,
        description=(
            "Optional discard/park Z for tips, in the gantry config's active "
            "frame (absolute deck-frame or home-frame, signed)."
        ),
    )
    tip_length: float = Field(
        ...,
        gt=0,
        description=(
            "Attached tip extension below the bare pipette nozzle in millimeters. "
            "Must be provided explicitly per rack — there is no safe default for "
            "this collision-critical dimension."
        ),
    )
    tips: Dict[str, Coordinate3D] = Field(
        ...,
        description="Mapping from tip ID (e.g. 'A1') to absolute pickup coordinates.",
    )
    tip_present: Dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "Per-tip presence flag keyed by tip ID "
            "(True = loaded, False = empty/consumed). "
            "Auto-initializes to all-True from the tips keys if left empty."
        ),
    )
    side_exit: SideExit | None = None

    def exit_blockers(self, tip_id: str, consumed: set[str] | None = None) -> list[str]:
        """Loaded tips in the same X lane must be removed from the opening inward."""
        if self.side_exit is None:
            return []
        p = self.get_tip_location(tip_id)
        low, high = sorted((p.x, self.side_exit.exit_x))
        return [key for key, q in self.tips.items()
                if key != tip_id and self.is_tip_present(key)
                and key not in (consumed or set())
                and abs(q.y - p.y) < 1e-6 and low < q.x < high]

    def pickup_path(self, tip_id: str) -> list[tuple[str, tuple[float, float, float], float]]:
        """Return approach, engagement, lift and exit in active tool coordinates.

        The extension changes at engagement. Subtracting tip_length from the
        two withdrawal Z targets keeps the carriage lift equal to lift_mm.
        """
        if self.side_exit is None:
            return []
        p = self.get_tip_location(tip_id)
        lift = self.side_exit.lift_mm
        z = p.z + lift
        return [
            ("approach", (p.x, p.y, z), 0.0),
            ("engage", (p.x, p.y, p.z), 0.0),
            ("lift", (p.x, p.y, z - self.tip_length), self.tip_length),
            ("exit", (self.side_exit.exit_x, p.y, z - self.tip_length), self.tip_length),
        ]

    @model_validator(mode="before")
    @classmethod
    def _derive_holder_fields_from_tips(cls, data: Any) -> Any:
        """Fill in HolderLabware fields from the tips dict when not provided."""
        if not isinstance(data, dict):
            return data

        tips_raw = data.get("tips") or {}
        if not tips_raw:
            return data

        def _xyz(coord: Any) -> tuple[float | None, float | None, float | None]:
            if isinstance(coord, dict):
                return coord.get("x"), coord.get("y"), coord.get("z")
            return getattr(coord, "x", None), getattr(coord, "y", None), getattr(coord, "z", None)

        xs: list[float] = []
        ys: list[float] = []
        for coord in tips_raw.values():
            x, y, _ = _xyz(coord)
            if x is not None:
                xs.append(x)
            if y is not None:
                ys.append(y)

        # Auto-fill location from A1 if not provided.
        if data.get("location") is None and "A1" in tips_raw:
            a1_x, a1_y, a1_z = _xyz(tips_raw["A1"])
            if a1_x is not None and a1_y is not None:
                z = a1_z if a1_z is not None else data.get("pickup_z", 0.0)
                data["location"] = Coordinate3D(x=a1_x, y=a1_y, z=z)

        # Auto-fill bounding box from tip spread; clamp to 1 mm minimum so
        # HolderLabware's positive-dimension validator accepts single-tip or
        # single-row racks.
        if data.get("length") is None and xs:
            data["length"] = max(round(max(xs) - min(xs), 3), 1.0)
        if data.get("width") is None and ys:
            data["width"] = max(round(max(ys) - min(ys), 3), 1.0)
        if data.get("height") is None:
            pickup_z = data.get("pickup_z")
            drop_z = data.get("drop_z")
            if pickup_z is not None and drop_z is not None:
                data["height"] = max(round(abs(pickup_z - drop_z), 3), 1.0)
            else:
                data["height"] = 1.0

        # Initialize tip_present to all-True if empty.
        if not data.get("tip_present"):
            data["tip_present"] = {tip_id: True for tip_id in tips_raw}

        return data

    @field_validator("name")
    def _validate_non_empty_text(cls, value: str) -> str:
        return Labware.validate_name(value)

    @model_validator(mode="after")
    def _validate_tip_layout(self) -> "TipRack":
        if not self.tips:
            raise ValueError("TipRack must define at least one tip.")
        if "A1" not in self.tips:
            raise ValueError("TipRack must define an 'A1' tip for anchoring.")
        expected_tip_count = self.rows * self.columns
        if len(self.tips) != expected_tip_count:
            raise ValueError(
                f"TipRack tips count must equal rows*columns ({expected_tip_count}), "
                f"got {len(self.tips)}."
            )
        extra_keys = set(self.tip_present) - set(self.tips)
        if extra_keys:
            raise ValueError(
                f"tip_present contains keys not in tips: {sorted(extra_keys)}"
            )
        if self.side_exit is not None:
            xs = [p.x for p in self.tips.values()]
            if min(xs) <= self.side_exit.exit_x <= max(xs):
                raise ValueError("side_exit.exit_x must be beyond all rack tip positions.")
        return self

    def get_location(self, location_id: str | None = None) -> Coordinate3D:
        """Resolve a tip ID, holder slot, or nested labware position.

        Tip IDs (``"A1"``, ``"B15"``...) take precedence and return the
        absolute tip coordinate. Any other ID is delegated to the
        :class:`HolderLabware` resolver for slots / contained_labware.
        """
        if location_id is not None and location_id in self.tips:
            return self.tips[location_id]
        return super().get_location(location_id)

    def get_tip_location(self, tip_id: str) -> Coordinate3D:
        try:
            return self.tips[tip_id]
        except KeyError as exc:
            raise KeyError(f"Unknown tip ID '{tip_id}'") from exc

    def get_initial_position(self) -> Coordinate3D:
        return self.get_tip_location("A1")

    def iter_positions(self) -> dict[str, Coordinate3D]:
        positions: dict[str, Coordinate3D] = {"location": self.location}
        positions.update(dict(self.tips))
        positions.update({slot_id: slot.location for slot_id, slot in self.slots.items()})
        return positions

    def mark_tip_used(self, tip_id: str) -> None:
        """Mark a tip as consumed (``tip_present[tip_id] = False``)."""
        if tip_id not in self.tips:
            raise KeyError(f"Unknown tip ID '{tip_id}'")
        self.tip_present[tip_id] = False

    def is_tip_present(self, tip_id: str) -> bool:
        return self.tip_present.get(tip_id, False)

    def next_available_tip(self) -> str | None:
        """Return the first tip ID that is still loaded, or ``None`` if empty."""
        for tip_id in self.tips:
            if self.tip_present.get(tip_id, False) and not self.exit_blockers(tip_id):
                return tip_id
        return None
