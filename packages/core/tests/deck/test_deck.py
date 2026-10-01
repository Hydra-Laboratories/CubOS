"""Tests for the Deck class: labware container with target resolution."""

import pytest

from cubos.deck import Coordinate3D, WellPlate, Vial
from cubos.deck.deck import Deck
from cubos.deck.labware.well_plate_holder import WellPlateHolder


# ----- Fixtures -----

def _make_plate() -> WellPlate:
    return WellPlate(
        name="plate_1",
        model_name="test_96",
        length=127.71,
        width=85.43,
        height=14.10,
        rows=2,
        columns=2,
        wells={
            "A1": Coordinate3D(x=0.0, y=0.0, z=75.0),
            "A2": Coordinate3D(x=10.0, y=0.0, z=75.0),
            "B1": Coordinate3D(x=0.0, y=8.0, z=75.0),
            "B2": Coordinate3D(x=10.0, y=8.0, z=75.0),
        },
        capacity_ul=200.0,
        working_volume_ul=150.0,
    )


def _make_vial() -> Vial:
    return Vial(
        name="vial_1",
        model_name="standard_1_5ml",
        height=66.75,
        diameter=28.0,
        location=Coordinate3D(x=30.0, y=40.0, z=20.0),
        capacity_ul=1500.0,
        working_volume_ul=1200.0,
    )


def _make_deck() -> Deck:
    return Deck({"plate_1": _make_plate(), "vial_1": _make_vial()})


# ----- Container protocol -----


def test_deck_getitem_missing_key_raises():
    """deck['unknown'] raises KeyError with clear message."""
    deck = _make_deck()
    with pytest.raises(KeyError, match="unknown"):
        deck["unknown"]


# ----- resolve_coordinate() -----


def test_resolve_coordinate_unknown_labware_raises():
    """resolve_coordinate('unknown.A1') raises KeyError for missing labware."""
    deck = _make_deck()
    with pytest.raises(KeyError, match="unknown"):
        deck.resolve_coordinate("unknown.A1")


def test_resolve_coordinate_unknown_labware_bare_raises():
    """resolve_coordinate('unknown') raises KeyError for missing labware."""
    deck = _make_deck()
    with pytest.raises(KeyError, match="unknown"):
        deck.resolve_coordinate("unknown")


def test_resolve_coordinate_invalid_well_id_raises():
    """resolve_coordinate('plate_1.Z99') raises KeyError for invalid well."""
    deck = _make_deck()
    with pytest.raises(KeyError, match="Z99"):
        deck.resolve_coordinate("plate_1.Z99")


def test_resolve_coordinate_vial_with_dot_location_raises():
    """resolve_coordinate('vial_1.X') raises KeyError since vial has no sub-locations."""
    deck = _make_deck()
    with pytest.raises(KeyError):
        deck.resolve_coordinate("vial_1.X")


@pytest.mark.parametrize("target,pattern", [
    ("unknown.A1", "unknown"),
    ("unknown", "unknown"),
    ("plate_1.Z99", "Z99"),
    ("vial_1.X", "Unknown location ID"),
])
def test_resolve_coordinate_match_errors_for_invalid_targets(target, pattern):
    """Coordinate resolver raises matching errors for invalid targets."""
    deck = _make_deck()

    with pytest.raises(KeyError, match=pattern):
        deck.resolve_coordinate(target)


def test_resolve_labware_missing_child_reports_failing_segment():
    deck = Deck({
        "plate_holder": WellPlateHolder(
            name="plate_holder",
            location=Coordinate3D(x=0.0, y=0.0, z=0.0),
        ),
    })

    with pytest.raises(KeyError) as exc_info:
        deck.resolve_labware("plate_holder.missing_plate")

    message = str(exc_info.value)
    assert "missing_plate" in message
    assert "plate_holder.missing_plate" not in message


def test_resolve_labware_target_nested_holder_plate_well():
    plate = _make_plate()
    holder = WellPlateHolder(
        name="plate_holder",
        location=Coordinate3D(x=0.0, y=0.0, z=0.0),
        contained_labware={"plate": plate},
    )
    deck = Deck({"plate_holder": holder})

    target = deck.resolve_labware_target("plate_holder.plate.A1")

    assert target.labware_key == "plate_holder.plate"
    assert target.labware_name == "plate_1"
    assert target.location_id == "A1"
    assert target.labware is plate
