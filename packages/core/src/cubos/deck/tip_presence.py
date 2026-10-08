"""Overlay durable per-slot tip status onto deck tip racks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .deck import Deck
from .labware.tip_rack import TipRack


def apply_durable_tip_status(deck: Deck, snapshot: Mapping[str, Any]) -> None:
    """Set each rack's ``tip_present`` from a durable tip snapshot.

    ``snapshot`` is the ``get_tip_snapshot`` shape: ``containers`` entries carry
    ``rack_key``, ``slot_id`` and ``status``. Only ``available`` slots stay
    loaded; ``attached`` and ``consumed`` slots are marked absent so static
    validation (side-exit lanes, presence checks) matches the durable state the
    runtime will execute against. Slots the deck does not define are ignored.
    """
    for item in snapshot.get("containers", ()):
        rack = deck.labware.get(item["rack_key"])
        if not isinstance(rack, TipRack) or item["slot_id"] not in rack.tips:
            continue
        rack.tip_present[item["slot_id"]] = item["status"] == "available"
