import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

import cubos.deck.labware.definitions.registry as definition_registry
from cubos.deck import (
    Coordinate3D,
    Deck,
    LabwareSlot,
    TipDisposal,
    VialHolder,
    WellPlate,
    WellPlateHolder,
)
from cubos.deck.loader import load_deck_from_yaml
from cubos.gantry.gantry_config import GantryConfig, GantryType, WorkingVolume
from cubos.validation.bounds import validate_deck_positions

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _make_gantry() -> GantryConfig:
    return GantryConfig(
        serial_port="/dev/ttyUSB0",
        gantry_type=GantryType.CUB_XL,
        factory_z_travel_mm=90.0,
        working_volume=WorkingVolume(
            x_min=0.0,
            x_max=300.0,
            y_min=0.0,
            y_max=300.0,
            z_min=0.0,
            z_max=100.0,
        ),
    )


def test_vial_holder_rejects_more_slots_than_capacity():
    too_many_slots = {
        f"vial_{index}": LabwareSlot(
            location=Coordinate3D(x=float(index), y=0.0, z=0.0),
            supported_labware_types=("vial",),
        )
        for index in range(1, 11)
    }

    with pytest.raises(ValidationError, match="slot_count"):
        VialHolder(
            name="vial_holder",
            location=Coordinate3D(x=0.0, y=0.0, z=0.0),
            slots=too_many_slots,
        )


def test_load_holder_labware_from_yaml():
    yaml_str = """
labware:
  waste:
    type: tip_disposal
    name: waste
    location:
      x: 25.0
      y: 35.0
      z: 10.0
    height: 10.0
  slide_holder:
    type: well_plate_holder
    name: slide_holder
    location:
      x: 50.0
      y: 60.0
      z: 12.0
    slots:
      plate:
        location:
          x: 51.0
          y: 61.0
        supported_labware_types:
          - well_plate
  vial_holder:
    type: vial_holder
    name: vial_holder
    location:
      x: 70.0
      y: 80.0
      z: 15.0
    slots:
      vial_1:
        location:
          x: 71.0
          y: 81.0
        supported_labware_types:
          - vial
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as handle:
        handle.write(yaml_str)
        path = handle.name

    try:
        deck = load_deck_from_yaml(path, factory_z_travel_mm=90.0)
        assert isinstance(deck["waste"], TipDisposal)
        assert isinstance(deck["slide_holder"], WellPlateHolder)
        assert isinstance(deck["vial_holder"], VialHolder)
        assert deck["waste"].location.z == pytest.approx(10.0)
        assert deck.resolve_coordinate("slide_holder.plate") == Coordinate3D(x=51.0, y=61.0, z=12.0)
        assert deck.resolve_coordinate("vial_holder.vial_1") == Coordinate3D(x=71.0, y=81.0, z=15.0)
    finally:
        Path(path).unlink(missing_ok=True)


def test_deck_yaml_load_name_expands_from_definitions_registry():
    """A deck YAML may reference a definition via `load_name:` and supply only
    the user-specific fields (typically just `location`)."""
    yaml_str = """
labware:
  my_vials:
    load_name: ursa_vial_holder
    location:
      x: 17.1
      y: 132.9
      z: 164.0
  my_plate_holder:
    load_name: ursa_wellplate_holder_conductive
    location:
      x: 50.0
      y: 60.0
      z: 12.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as handle:
        handle.write(yaml_str)
        path = handle.name

    try:
        deck = load_deck_from_yaml(path)

        vials = deck["my_vials"]
        assert isinstance(vials, VialHolder)
        assert vials.name == "my_vials"  # defaulted to deck key
        assert vials.length == pytest.approx(36.2)
        assert vials.width == pytest.approx(300.2)
        assert vials.height == pytest.approx(35.1)
        assert vials.labware_seat_height_from_bottom == pytest.approx(18.0)
        assert vials.slot_count == 9
        assert vials.location == Coordinate3D(x=17.1, y=132.9, z=164.0)

        plate_holder = deck["my_plate_holder"]
        assert isinstance(plate_holder, WellPlateHolder)
        assert plate_holder.name == "my_plate_holder"
        assert plate_holder.length == pytest.approx(100.0)
        assert plate_holder.width == pytest.approx(155.0)
        assert plate_holder.height == pytest.approx(14.8)
        assert plate_holder.labware_seat_height_from_bottom == pytest.approx(5.0)
    finally:
        Path(path).unlink(missing_ok=True)


def test_panda_sbs_holder_definition_loads_through_registry():
    yaml_str = """
labware:
  plate_holder:
    load_name: panda_sbs_wellplate_holder
    location:
      x: 90.0
      y: 112.5
      z: 10.0
    well_plate:
      model_name: sbs_96_wellplate
      rows: 8
      columns: 12
      length: 127.76
      width: 85.47
      height: 14.35
      calibration:
        a1:
          x: 61.645
          y: 59.86
        a2:
          x: 61.645
          y: 68.86
      x_offset: 9.0
      y_offset: 9.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as handle:
        handle.write(yaml_str)
        path = handle.name

    try:
        deck = load_deck_from_yaml(path)
        holder = deck["plate_holder"]

        assert isinstance(holder, WellPlateHolder)
        assert holder.name == "plate_holder"
        assert holder.length == pytest.approx(100.0)
        assert holder.width == pytest.approx(164.0)
        assert holder.height == pytest.approx(6.0)
        assert holder.labware_seat_height_from_bottom == pytest.approx(3.0)
        assert holder.well_plate_surface_height_from_bottom == pytest.approx(17.35)
        assert deck.resolve_coordinate("plate_holder.plate.A1").z == pytest.approx(27.35)
    finally:
        Path(path).unlink(missing_ok=True)


def test_sharc_deck_nested_plate_surface_resolves_to_plate_rim_z():
    deck = load_deck_from_yaml(FIXTURES / "configs/deck/mock_nested_plate_deck.yaml")
    holder = deck["plate_holder"]
    plate = deck.resolve_labware("plate_holder.plate")

    assert isinstance(holder, WellPlateHolder)
    assert isinstance(plate, WellPlate)
    assert holder.location == Coordinate3D(x=0.0, y=0.0, z=0.0)
    assert "location" in holder.iter_positions()
    assert deck.resolve_coordinate("plate_holder.plate.A1") == Coordinate3D(
        x=15.0, y=17.0, z=89.5,
    )
    assert deck.resolve_coordinate("plate_holder.plate.A2") == Coordinate3D(
        x=15.0, y=26.0, z=89.5,
    )
    assert deck.resolve_coordinate("plate_holder.plate.B1") == Coordinate3D(
        x=24.0, y=17.0, z=89.5,
    )


def test_deck_yaml_unknown_load_name_raises_clear_error():
    yaml_str = """
labware:
  broken:
    load_name: no_such_definition
    location: {x: 0.0, y: 0.0, z: 0.0}
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as handle:
        handle.write(yaml_str)
        path = handle.name

    try:
        with pytest.raises(Exception, match="no_such_definition"):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_deck_yaml_missing_definition_config_file_has_distinct_error(monkeypatch):
    monkeypatch.setattr(
        definition_registry,
        "_cache",
        {
            "labware": {
                "missing_config_definition": {
                    "module": "cubos.deck.labware.well_plate",
                    "class_name": "WellPlate",
                    "config": "does_not_exist.yaml",
                }
            }
        },
    )
    yaml_str = """
labware:
  broken:
    load_name: missing_config_definition
    calibration:
      a1: {x: 0.0, y: 0.0, z: 0.0}
      a2: {x: 9.0, y: 0.0, z: 0.0}
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as handle:
        handle.write(yaml_str)
        path = handle.name

    try:
        with pytest.raises(Exception) as exc_info:
            load_deck_from_yaml(path)
        message = str(exc_info.value)
        assert "Definition config file is missing" in message
        assert "Unknown `load_name" not in message
    finally:
        definition_registry._reset_cache()
        Path(path).unlink(missing_ok=True)


def test_tip_rack_definition_requires_per_deck_calibration_and_pickup_z():
    yaml_str = """
labware:
  tips:
    load_name: ursa_tip_rack
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as handle:
        handle.write(yaml_str)
        path = handle.name

    try:
        with pytest.raises(Exception) as exc_info:
            load_deck_from_yaml(path)
        message = str(exc_info.value)
        assert "definition provides geometry only" in message
        assert "calibration" in message
        assert "pickup_z" in message
    finally:
        Path(path).unlink(missing_ok=True)


def test_holder_slots_participate_in_bounds_validation():
    gantry = _make_gantry()
    holder = VialHolder(
        name="vial_holder",
        location=Coordinate3D(x=20.0, y=20.0, z=20.0),
        slots={
            "vial_1": LabwareSlot(
                location=Coordinate3D(x=301.0, y=20.0, z=20.0),
                supported_labware_types=("vial",),
            )
        },
    )
    deck = Deck({"vials": holder})

    violations = validate_deck_positions(gantry, deck)

    assert len(violations) == 1
    assert violations[0].labware_key == "vials"
    assert violations[0].position_id == "vial_1"
    assert violations[0].axis == "x"
    assert violations[0].bound_name == "x_max"
