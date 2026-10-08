"""Deck: runtime container for loaded labware with target resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterator, Mapping

from .labware.labware import Coordinate3D, Labware


@dataclass(frozen=True)
class DeckLabwareTarget:
    """Resolved labware path plus optional location/well id."""

    labware_key: str
    labware: Labware
    location_id: str | None = None

    @property
    def labware_name(self) -> str:
        return getattr(self.labware, "name", self.labware_key)


class Deck:
    """
    Runtime container holding labware loaded from a deck YAML.

    Provides dict-like access to labware by key plus:

    - ``resolve_coordinate()``: resolve a deck-coordinate target string (e.g.
      ``"plate_1.A1"``) into a coordinate.
    - ``resolve_labware()``: resolve a nested labware object path (e.g.
      ``"plate_holder.plate"``).
    - ``resolve_labware_target()``: split an addressable deck target into
      the owning labware path plus optional location/well id.
    """

    def __init__(
        self,
        labware: Dict[str, Labware],
        *,
        volume_labware: Mapping[str, Labware] | None = None,
        target_aliases: Mapping[str, str] | None = None,
        motion_planning: Mapping[str, Any] | None = None,
    ) -> None:
        self._labware = dict(labware)
        self._has_explicit_volume_registry = volume_labware is not None
        self._volume_labware = (
            dict(volume_labware)
            if volume_labware is not None
            else {
                key: item
                for key, item in labware.items()
                if hasattr(item, "capacity_ul") or hasattr(item, "vials")
            }
        )
        self._target_aliases = dict(target_aliases or {})
        self._motion_planning = (
            dict(motion_planning) if motion_planning is not None else None
        )

    @property
    def motion_planning(self) -> Dict[str, Any] | None:
        """Return opt-in routing settings, or ``None`` for a legacy deck."""
        return None if self._motion_planning is None else dict(self._motion_planning)

    @property
    def planning_enabled(self) -> bool:
        return self._motion_planning is not None

    @property
    def labware(self) -> Dict[str, Labware]:
        return self._labware

    @property
    def volume_labware(self) -> Dict[str, Labware]:
        """Return volume-bearing labware by canonical flat instance ID.

        Existing holder objects remain in :attr:`labware` for compatibility,
        while this mapping is the persistence-facing source of truth. Legacy
        aliases are intentionally excluded.
        """
        return self._volume_labware

    @property
    def has_explicit_volume_registry(self) -> bool:
        """Whether ``volume_labware`` was supplied by a canonical loader.

        Plain programmatic ``Deck({...})`` callers retain legacy recursive
        persistence registration even though direct volume labware is inferred
        for target resolution.
        """
        return self._has_explicit_volume_registry

    @property
    def target_aliases(self) -> Dict[str, str]:
        """Return legacy/display target aliases mapped to canonical targets."""
        return self._target_aliases

    def canonicalize_target(self, target: str) -> str:
        """Return the stable flat identity for an addressable deck target.

        Exact aliases cover legacy vial paths and user-defined position
        labels. Prefix aliases cover a legacy nested plate path followed by a
        well ID, for example ``holder.plate.A1``.
        """
        if not isinstance(target, str) or not target:
            raise ValueError("Deck target must be a non-empty string.")

        exact = self._target_aliases.get(target)
        if exact is not None:
            return exact

        for alias in sorted(self._target_aliases, key=len, reverse=True):
            prefix = f"{alias}."
            if target.startswith(prefix):
                canonical_prefix = self._target_aliases[alias]
                return f"{canonical_prefix}.{target[len(prefix):]}"

        if "." in target:
            labware_key, location_id = target.split(".", 1)
            labware = self._volume_labware.get(labware_key)
            canonicalize = getattr(labware, "canonicalize_location_id", None)
            if callable(canonicalize):
                canonical_location_id = canonicalize(location_id)
                return f"{labware_key}.{canonical_location_id}"

        return target

    def _resolve_canonical_target(
        self,
        target: str,
    ) -> DeckLabwareTarget | None:
        canonical = self.canonicalize_target(target)
        labware = self._volume_labware.get(canonical)
        if labware is not None:
            return DeckLabwareTarget(
                labware_key=canonical,
                labware=labware,
                location_id=None,
            )

        if "." not in canonical:
            return None
        labware_key, location_id = canonical.split(".", 1)
        labware = self._volume_labware.get(labware_key)
        if labware is None:
            return None
        return DeckLabwareTarget(
            labware_key=labware_key,
            labware=labware,
            location_id=location_id,
        )

    def resolve_coordinate(self, target: str) -> Coordinate3D:
        """
        Resolve a deck coordinate target to an absolute ``Coordinate3D``.

        Formats:
            'plate_1.A1'  -> well A1 on plate_1
            'vial_1'      -> vial center (initial position)
            'plate_1'     -> plate initial position (A1)
        """
        canonical = self._resolve_canonical_target(target)
        if canonical is not None:
            if canonical.location_id is None:
                return canonical.labware.get_initial_position()
            return canonical.labware.get_location(canonical.location_id)

        if "." in target:
            labware_key, location_id = target.split(".", 1)
            return self._get_labware(labware_key).get_location(location_id)
        return self._get_labware(target).get_initial_position()

    def resolve_labware(self, target: str) -> Labware:
        """Resolve a top-level or nested labware path to a Labware object.

        Formats:
            'plate_1'            -> top-level plate_1 object
            'plate_holder.plate' -> contained plate object
        """
        parts = target.split(".")
        try:
            labware = self._get_labware(parts[0])
        except KeyError:
            canonical = self.canonicalize_target(target)
            try:
                return self._volume_labware[canonical]
            except KeyError:
                raise KeyError(f"No labware '{target}' on deck.") from None

        for child_name in parts[1:]:
            children = getattr(labware, "contained_labware", {})
            try:
                labware = children[child_name]
            except KeyError as exc:
                raise KeyError(
                    f"No nested labware '{child_name}' on deck."
                ) from exc
        return labware

    def resolve_labware_target(self, target: str) -> DeckLabwareTarget:
        """Resolve the labware owner for a deck target.

        Formats:
            'vial_1'                 -> labware_key='vial_1', location_id=None
            'plate_1.A1'             -> labware_key='plate_1', location_id='A1'
            'plate_holder.plate.A1'  -> labware_key='plate_holder.plate',
                                       location_id='A1'
        """
        canonical = self._resolve_canonical_target(target)
        if canonical is not None:
            return canonical

        try:
            labware = self.resolve_labware(target)
        except KeyError:
            if "." not in target:
                raise
        else:
            return DeckLabwareTarget(
                labware_key=target,
                labware=labware,
                location_id=None,
            )

        labware_key, location_id = target.rsplit(".", 1)
        return DeckLabwareTarget(
            labware_key=labware_key,
            labware=self.resolve_labware(labware_key),
            location_id=location_id,
        )

    def _get_labware(self, key: str) -> Labware:
        try:
            return self._labware[key]
        except KeyError:
            raise KeyError(f"No labware '{key}' on deck.") from None

    def __getitem__(self, key: str) -> Labware:
        return self._get_labware(key)

    def __contains__(self, key: object) -> bool:
        return key in self._labware

    def __len__(self) -> int:
        return len(self._labware)

    def __iter__(self) -> Iterator[str]:
        return iter(self._labware)

    def __repr__(self) -> str:
        keys = ", ".join(self._labware.keys())
        return f"Deck([{keys}])"
