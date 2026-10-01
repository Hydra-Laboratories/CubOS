"""Tests for optional per-well attributes on ``WellPlate``.

``well_attributes`` is an open mapping, optional by design: no plate is
ever required to declare it, no key is reserved, and every plate that
omits it stays fully addressable with an empty mapping.
"""

from __future__ import annotations


import pytest
from pydantic import ValidationError

from cubos.deck.labware.labware import Coordinate3D
from cubos.deck.labware.well_plate import WellPlate


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_plate(**overrides) -> WellPlate:
    kwargs = {
        "name": "plate_1",
        "model_name": "test_plate",
        "length": 127.76,
        "width": 85.47,
        "height": 14.35,
        "well_depth": 10.67,
        "rows": 1,
        "columns": 2,
        "wells": {
            "A1": Coordinate3D(x=0.0, y=0.0, z=-5.0),
            "A2": Coordinate3D(x=9.0, y=0.0, z=-5.0),
        },
        "capacity_ul": 200.0,
        "working_volume_ul": 150.0,
    }
    kwargs.update(overrides)
    return WellPlate(**kwargs)


# ─── The bag is open ─────────────────────────────────────────────────────────


def test_well_attributes_accept_arbitrary_scalar_keys_and_types():
    plate = _make_plate(
        well_attributes={
            "diameter": 6.86,
            "bottom": "flat",
            "rows_addressable": 8,
            "conductive": True,
        }
    )

    assert plate.well_attributes["diameter"] == pytest.approx(6.86)
    assert plate.well_attributes["bottom"] == "flat"
    assert plate.well_attributes["rows_addressable"] == 8
    assert plate.well_attributes["conductive"] is True


def test_unknown_attribute_keys_are_stored_not_rejected():
    # The bag is deliberately open — the schema does not police key names.
    plate = _make_plate(well_attributes={"totally_made_up_key": 1.0})

    assert plate.well_attributes["totally_made_up_key"] == pytest.approx(1.0)


@pytest.mark.parametrize("value", [{"nested": 1}, [1, 2, 3], None])
def test_non_scalar_attribute_values_rejected(value):
    # Open on keys, closed on shape: values stay flat scalars so the bag
    # cannot grow into an unvalidated nested structure.
    with pytest.raises(ValidationError):
        _make_plate(well_attributes={"diameter": value})


# ─── Deck YAML round-trip ────────────────────────────────────────────────────


TOP_LEVEL_PLATE_YAML = """
labware:
  plate_1:
    type: well_plate
    name: plate_1
    model_name: attribute_plate
    rows: 2
    columns: 2
    well_depth: 10.67
    well_attributes:
      diameter: 6.86
      bottom: flat
    calibration:
      a1: { x: 10.0, y: 20.0, z: -5.0 }
      a2: { x: 19.0, y: 20.0, z: -5.0 }
    x_offset: 9.0
    y_offset: 9.0
    capacity_ul: 200.0
    working_volume_ul: 150.0
"""


NESTED_PLATE_YAML = """
labware:
  plate_holder:
    type: well_plate_holder
    name: plate_holder
    location:
      x: 221.75
      y: 78.5
      z: 183.0
    well_plate:
      model_name: nested_attribute_plate
      rows: 2
      columns: 2
      well_attributes:
        diameter: 6.86
        bottom: v
      calibration:
        a1: { x: 221.75, y: 78.5 }
        a2: { x: 230.75, y: 78.5 }
      x_offset: 9.0
      y_offset: 9.0
"""


NESTED_PLATE_WITHOUT_ATTRIBUTES_YAML = """
labware:
  plate_holder:
    type: well_plate_holder
    name: plate_holder
    location:
      x: 221.75
      y: 78.5
      z: 183.0
    well_plate:
      model_name: plain_nested_plate
      rows: 2
      columns: 2
      calibration:
        a1: { x: 221.75, y: 78.5 }
        a2: { x: 230.75, y: 78.5 }
      x_offset: 9.0
      y_offset: 9.0
"""


# ─── load_name expansion carries attributes ──────────────────────────────────


SBS96_LOAD_NAME_YAML = """
labware:
  plate_1:
    load_name: sbs_96_wellplate
    calibration:
      a1: { x: -17.88, y: -42.23, z: -20.0 }
      a2: { x: -8.88, y: -42.23, z: -20.0 }
"""
