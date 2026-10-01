"""Tests for strict deck YAML loading and labware object mapping."""

import tempfile
from pathlib import Path

import pytest

from pydantic import ValidationError

from cubos.deck import (
    WellPlate,
    Coordinate3D,
    TipRack,
    Wall,
    derive_wells_preview,
    resolve_load_names,
)
from cubos.deck.loader import (
    DeckLoaderError,
    _resolve_plate_orientation,
    load_deck_from_yaml,
    load_deck_from_yaml_safe,
)
from cubos.deck.yaml_schema import WellPlateYamlEntry, _YamlCalibrationPoints, _YamlPoint3D


# ----- Valid deck YAML fixtures -----

VALID_DECK_ONE_PLATE_ONE_VIAL = """
labware:
  plate_1:
    type: well_plate
    name: opentrons_96_well_20ml
    model_name: opentrons_96_well_20ml
    rows: 8
    columns: 12
    length: 127.71
    width: 85.43
    height: 14.10
    calibration:
      a1:
        x: -10.0
        y: -10.0
        z: -15.0
      a2:
        x: -1.0
        y: -10.0
        z: -15.0
    x_offset: 9.0
    y_offset: 9.0
    capacity_ul: 200.0
    working_volume_ul: 150.0
  vial_1:
    type: vial
    name: standard_vial_rack
    model_name: standard_1_5ml_vial
    height: 66.75
    diameter: 28.0
    location:
      x: -30.0
      y: -40.0
      z: -20.0
    capacity_ul: 1500.0
    working_volume_ul: 1200.0
"""


def test_duplicate_yaml_key_in_deck_file_names_file_and_key(tmp_path):
    path = tmp_path / "duplicate_deck.yaml"
    path.write_text(
        """
labware:
  plate:
    type: vial
    name: first
    model_name: vial
    height: 10.0
    diameter: 5.0
    location: {x: 1.0, y: 2.0, z: 3.0}
    capacity_ul: 10.0
    working_volume_ul: 5.0
  plate:
    type: vial
    name: second
    model_name: vial
    height: 10.0
    diameter: 5.0
    location: {x: 4.0, y: 5.0, z: 6.0}
    capacity_ul: 10.0
    working_volume_ul: 5.0
""",
        encoding="utf-8",
    )

    with pytest.raises(Exception) as exc_info:
        load_deck_from_yaml(path)

    message = str(exc_info.value)
    assert str(path) in message
    assert "duplicate YAML key 'plate'" in message


def test_public_derive_wells_preview_rejects_bad_orientation():
    entry = WellPlateYamlEntry.model_validate({
        "type": "well_plate",
        "name": "preview_plate",
        "rows": 1,
        "columns": 2,
        "calibration": {
            "a1": {"x": 10.0, "y": 20.0, "z": 5.0},
            "a2": {"x": 18.0, "y": 20.0, "z": 5.0},
        },
        "x_offset": 9.0,
        "y_offset": 8.0,
    })

    with pytest.raises(ValueError, match="A2 must match"):
        derive_wells_preview(entry, resolved_z=5.0)


def test_public_resolve_load_names_unknown_definition_raises_clear_error():
    with pytest.raises(DeckLoaderError, match="Unknown `load_name"):
        resolve_load_names({
            "labware": {
                "thing": {
                    "load_name": "missing_definition",
                }
            }
        })


def test_vial_dead_volume_defaults_to_zero_and_loads_when_specified():
    yaml = """
labware:
  vial_default:
    type: vial
    name: vial_default
    height: 66.75
    diameter: 28.0
    location: {x: 30.0, y: 40.0, z: 30.0}
    capacity_ul: 1500.0
    working_volume_ul: 1200.0
  vial_dead:
    type: vial
    name: vial_dead
    height: 66.75
    diameter: 28.0
    location: {x: 60.0, y: 40.0, z: 30.0}
    capacity_ul: 1500.0
    working_volume_ul: 1200.0
    dead_volume_ul: 75.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        result = load_deck_from_yaml(path)
        assert result["vial_default"].dead_volume_ul == pytest.approx(0.0)
        assert result["vial_dead"].dead_volume_ul == pytest.approx(75.0)
    finally:
        Path(path).unlink(missing_ok=True)


def test_vial_dead_volume_above_working_volume_rejected():
    yaml = """
labware:
  vial_1:
    type: vial
    name: vial_1
    height: 66.75
    diameter: 28.0
    location: {x: 30.0, y: 40.0, z: 30.0}
    capacity_ul: 1500.0
    working_volume_ul: 1200.0
    dead_volume_ul: 1300.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        with pytest.raises(Exception, match="dead_volume_ul"):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_vial_grid_dead_volume_propagates_to_every_vial():
    yaml = """
labware:
  reagents:
    type: vial_grid
    name: reagents
    rows: 1
    columns: 2
    calibration:
      a1: {x: 10.0, y: 20.0, z: 30.0}
      a2: {x: 20.0, y: 20.0, z: 30.0}
    x_offset: 10.0
    y_offset: 10.0
    vial_height: 40.0
    vial_diameter: 12.0
    capacity_ul: 500.0
    working_volume_ul: 400.0
    vial_dead_volume_ul: 25.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        result = load_deck_from_yaml(path)
        grid = result["reagents"]
        for vial in grid.vials.values():
            assert vial.dead_volume_ul == pytest.approx(25.0)
    finally:
        Path(path).unlink(missing_ok=True)


def test_raw_z_works_without_factory_z_travel_mm() -> None:
    yaml = """
labware:
  vial_1:
    type: vial
    name: standard_vial_rack
    model_name: standard_1_5ml_vial
    height: 66.75
    diameter: 28.0
    location:
      x: 30.0
      y: 40.0
      z: 20.0
    capacity_ul: 1500.0
    working_volume_ul: 1200.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        result = load_deck_from_yaml(path)
        vial = result["vial_1"]
        assert vial.location.z == pytest.approx(20.0)
    finally:
        Path(path).unlink(missing_ok=True)


def test_explicit_z_does_not_require_factory_z_travel_mm() -> None:
    yaml = """
labware:
  vial_1:
    type: vial
    name: standard_vial_rack
    model_name: standard_1_5ml_vial
    height: 66.75
    diameter: 28.0
    location:
      x: 30.0
      y: 40.0
      z: 30.0
    capacity_ul: 1500.0
    working_volume_ul: 1200.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        result = load_deck_from_yaml(path)
        assert result["vial_1"].location.z == pytest.approx(30.0)
    finally:
        Path(path).unlink(missing_ok=True)


# ----- Calibration anchor as the single source of truth for surface Z -----


def test_well_plate_missing_calibration_z_raises():
    """The legacy ``height`` z-hint shorthand was removed when the
    dimensional ``height`` field took over the name. A well plate that
    omits ``calibration.a1.z`` must raise a clear error pointing at the
    calibration anchor."""
    yaml = """
labware:
  plate_1:
    type: well_plate
    name: small
    model_name: small
    rows: 2
    columns: 2
    length: 20.0
    width: 20.0
    height: 10.0
    calibration:
      a1: { x: 0.0, y: 0.0 }
      a2: { x: 10.0, y: 0.0 }
    x_offset: 9.0
    y_offset: 9.0
"""
    path = _write_yaml = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    path.write(yaml); path.close()
    try:
        with pytest.raises(ValueError, match="calibration anchor"):
            load_deck_from_yaml(path.name)
    finally:
        Path(path.name).unlink(missing_ok=True)


def test_vial_missing_location_z_raises():
    yaml = """
labware:
  vial_1:
    type: vial
    name: standard_vial_rack
    model_name: standard_1_5ml_vial
    height: 66.75
    diameter: 28.0
    location:
      x: 30.0
      y: 40.0
    capacity_ul: 1500.0
    working_volume_ul: 1200.0
"""
    path = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    path.write(yaml); path.close()
    try:
        with pytest.raises(ValueError, match="calibration anchor"):
            load_deck_from_yaml(path.name)
    finally:
        Path(path.name).unlink(missing_ok=True)


# ----- Two-point calibration orientations -----

def test_calibration_horizontal_increasing_columns():
    """A2.x > A1.x, A2.y == A1.y: columns along +X."""
    yaml = """
labware:
  p:
    type: well_plate
    name: small
    model_name: small
    rows: 2
    columns: 2
    length: 20.0
    width: 20.0
    height: 10.0
    calibration:
      a1: { x: 0.0, y: 0.0, z: -5.0 }
      a2: { x: 10.0, y: 0.0, z: -5.0 }
    x_offset: 10.0
    y_offset: 8.0
    capacity_ul: 100.0
    working_volume_ul: 80.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        result = load_deck_from_yaml(path)
        plate = result["p"]
        assert plate.get_well_center("A1").x == pytest.approx(0.0)
        assert plate.get_well_center("A1").y == pytest.approx(0.0)
        assert plate.get_well_center("A2").x == pytest.approx(10.0)
        assert plate.get_well_center("A2").y == pytest.approx(0.0)
        assert plate.get_well_center("B1").x == pytest.approx(0.0)
        assert plate.get_well_center("B1").y == pytest.approx(-8.0)
    finally:
        Path(path).unlink(missing_ok=True)


def test_calibration_horizontal_decreasing_columns():
    """A2.x < A1.x, A2.y == A1.y: A2 determines columns along -X."""
    yaml = """
labware:
  p:
    type: well_plate
    name: small
    model_name: small
    rows: 2
    columns: 2
    length: 20.0
    width: 20.0
    height: 10.0
    calibration:
      a1: { x: 10.0, y: 0.0, z: -5.0 }
      a2: { x: 0.0, y: 0.0, z: -5.0 }
    x_offset: 10.0
    y_offset: 8.0
    capacity_ul: 100.0
    working_volume_ul: 80.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        result = load_deck_from_yaml(path)
        plate = result["p"]
        assert plate.get_well_center("A1").x == pytest.approx(10.0)
        assert plate.get_well_center("A2").x == pytest.approx(0.0)
        assert plate.get_well_center("A2").y == pytest.approx(0.0)
    finally:
        Path(path).unlink(missing_ok=True)


def test_calibration_vertical_increasing_rows():
    """A2.y > A1.y, A2.x == A1.x: column direction is Y; A2 at (a1.x, a1.y + y_offset)."""
    yaml = """
labware:
  p:
    type: well_plate
    name: small
    model_name: small
    rows: 2
    columns: 2
    length: 20.0
    width: 20.0
    height: 10.0
    calibration:
      a1: { x: 0.0, y: 0.0, z: -5.0 }
      a2: { x: 0.0, y: 8.0, z: -5.0 }
    x_offset: 10.0
    y_offset: 8.0
    capacity_ul: 100.0
    working_volume_ul: 80.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        result = load_deck_from_yaml(path)
        plate = result["p"]
        assert plate.get_well_center("A1").x == pytest.approx(0.0)
        assert plate.get_well_center("A1").y == pytest.approx(0.0)
        assert plate.get_well_center("A2").x == pytest.approx(0.0)
        assert plate.get_well_center("A2").y == pytest.approx(8.0)
        assert plate.get_well_center("B1").x == pytest.approx(10.0)
        assert plate.get_well_center("B1").y == pytest.approx(0.0)
    finally:
        Path(path).unlink(missing_ok=True)


def test_calibration_vertical_decreasing_rows():
    """A2.y < A1.y, A2.x == A1.x."""
    yaml = """
labware:
  p:
    type: well_plate
    name: small
    model_name: small
    rows: 2
    columns: 2
    length: 20.0
    width: 20.0
    height: 10.0
    calibration:
      a1: { x: 0.0, y: 8.0, z: -5.0 }
      a2: { x: 0.0, y: 0.0, z: -5.0 }
    x_offset: 10.0
    y_offset: 8.0
    capacity_ul: 100.0
    working_volume_ul: 80.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        result = load_deck_from_yaml(path)
        plate = result["p"]
        assert plate.get_well_center("A1").y == pytest.approx(8.0)
        assert plate.get_well_center("A2").y == pytest.approx(0.0)
        assert plate.get_well_center("A2").x == pytest.approx(0.0)
    finally:
        Path(path).unlink(missing_ok=True)


def test_calibration_diagonal_fails():
    """A1 and A2 with both x and y different (diagonal) must fail."""
    yaml = """
labware:
  p:
    type: well_plate
    name: small
    model_name: small
    rows: 2
    columns: 2
    length: 20.0
    width: 20.0
    height: 10.0
    calibration:
      a1: { x: 0.0, y: 0.0, z: -5.0 }
      a2: { x: 10.0, y: 5.0, z: -5.0 }
    x_offset: 10.0
    y_offset: 8.0
    capacity_ul: 100.0
    working_volume_ul: 80.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        with pytest.raises(ValidationError, match="axis.aligned|diagonal|orientation"):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_safe_loader_returns_clean_error_message():
    """Safe loader raises DeckLoaderError with concise fix guidance."""
    yaml = """
labware:
  p:
    type: well_plate
    name: small
    model_name: small
    rows: 2
    columns: 2
    length: 20.0
    width: 20.0
    height: 10.0
    calibration:
      a1: { x: 0.0, y: 0.0, z: -5.0 }
      a2: { x: 10.0, y: 5.0, z: -5.0 }
    x_offset: 10.0
    y_offset: 8.0
    capacity_ul: 100.0
    working_volume_ul: 80.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        with pytest.raises(DeckLoaderError) as exc_info:
            load_deck_from_yaml_safe(path)
        message = str(exc_info.value)
        assert message.startswith("❌")
        assert "How to fix:" in message
        assert "axis-aligned" in message or "shares either the same x or the same y" in message
    finally:
        Path(path).unlink(missing_ok=True)


def test_safe_loader_yaml_parse_error_has_clean_message():
    """Safe loader reports YAML parse issues without traceback noise."""
    bad_yaml = "labware:\n  plate_1:\n    type: well_plate\n    calibration: [\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(bad_yaml)
        path = f.name
    try:
        with pytest.raises(DeckLoaderError) as exc_info:
            load_deck_from_yaml_safe(path)
        message = str(exc_info.value)
        assert message.startswith("❌")
        assert "parse error" in message.lower()
        assert "How to fix:" in message
    finally:
        Path(path).unlink(missing_ok=True)


def test_safe_loader_missing_file_has_clean_message():
    """Safe loader reports missing-file errors as DeckLoaderError."""
    missing_path = "/tmp/this_file_does_not_exist_12345.yaml"
    with pytest.raises(DeckLoaderError) as exc_info:
        load_deck_from_yaml_safe(missing_path)
    message = str(exc_info.value)
    assert message.startswith("❌")
    assert "deck loader error" in message.lower()
    assert "How to fix:" in message


def test_zero_offsets_fail_schema_validation():
    """x/y offsets must be positive in well plate schema."""
    yaml = """
labware:
  p:
    type: well_plate
    name: x
    model_name: x
    rows: 8
    columns: 12
    length: 127.71
    width: 85.43
    height: 14.10
    calibration:
      a1: { x: 0.0, y: 0.0, z: -15.0 }
      a2: { x: 9.0, y: 0.0, z: -15.0 }
    x_offset: 0.0
    y_offset: 9.0
    capacity_ul: 200.0
    working_volume_ul: 150.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        with pytest.raises(ValidationError):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("x_offset", "y_offset"),
    [(-9.0, 9.0), (9.0, -9.0)],
)
def test_negative_offsets_fail_schema_validation(x_offset, y_offset):
    """Offset fields are spacing magnitudes and must not be negative."""
    yaml = f"""
labware:
  p:
    type: well_plate
    name: x
    model_name: x
    rows: 8
    columns: 12
    length: 127.71
    width: 85.43
    height: 14.10
    calibration:
      a1: {{ x: 0.0, y: 0.0, z: -15.0 }}
      a2: {{ x: 9.0, y: 0.0, z: -15.0 }}
    x_offset: {x_offset}
    y_offset: {y_offset}
    capacity_ul: 200.0
    working_volume_ul: 150.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        with pytest.raises(ValidationError, match="greater than 0"):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_calibration_identical_points_fails():
    """A1 and A2 identical must fail."""
    yaml = """
labware:
  p:
    type: well_plate
    name: small
    model_name: small
    rows: 2
    columns: 2
    length: 20.0
    width: 20.0
    height: 10.0
    calibration:
      a1: { x: 0.0, y: 0.0, z: -5.0 }
      a2: { x: 0.0, y: 0.0, z: -5.0 }
    x_offset: 10.0
    y_offset: 8.0
    capacity_ul: 100.0
    working_volume_ul: 80.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        with pytest.raises(ValidationError, match="identical|degenerate|same"):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


# ----- Missing required fields -----

def test_missing_top_level_labware_fails():
    """Deck YAML without 'labware' key fails."""
    yaml = "other: 1\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        with pytest.raises(ValidationError):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_missing_well_plate_required_field_fails():
    """Well plate entry missing e.g. 'rows' or 'calibration' fails."""
    yaml = """
labware:
  p:
    type: well_plate
    name: x
    model_name: x
    columns: 12
    length: 127.71
    width: 85.43
    height: 14.10
    calibration:
      a1: { x: 0.0, y: 0.0, z: -15.0 }
      a2: { x: 9.0, y: 0.0, z: -15.0 }
    x_offset: 9.0
    y_offset: 9.0
    capacity_ul: 200.0
    working_volume_ul: 150.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        with pytest.raises(ValidationError):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_missing_vial_required_field_fails():
    """Vial entry missing e.g. 'location' fails."""
    yaml = """
labware:
  v:
    type: vial
    name: v1
    model_name: m1
    height: 66.0
    diameter: 28.0
    capacity_ul: 1500.0
    working_volume_ul: 1200.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        with pytest.raises(ValidationError):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


# ----- Extra fields -----

def test_extra_top_level_field_fails():
    """Unknown top-level key in deck YAML fails."""
    yaml = """
labware: {}
gantry: {}
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        with pytest.raises(ValidationError):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_extra_labware_entry_field_fails():
    """Unknown key inside a labware entry fails."""
    yaml = """
labware:
  p:
    type: well_plate
    name: x
    model_name: x
    rows: 8
    columns: 12
    length: 127.71
    width: 85.43
    height: 14.10
    calibration:
      a1: { x: 0.0, y: 0.0, z: -15.0 }
      a2: { x: 9.0, y: 0.0, z: -15.0 }
    x_offset: 9.0
    y_offset: 9.0
    capacity_ul: 200.0
    working_volume_ul: 150.0
    unknown_field: 1
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        with pytest.raises(ValidationError):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_labware_key_with_dot_is_rejected_at_load_time():
    yaml = """
labware:
  plate.1:
    type: vial
    name: dotted
    model_name: vial
    height: 10.0
    diameter: 5.0
    location: {x: 1.0, y: 2.0, z: 3.0}
    capacity_ul: 10.0
    working_volume_ul: 5.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        with pytest.raises(ValidationError, match="cannot contain"):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


# ----- Type coercion and invalid types -----


def test_non_coercible_type_fails():
    """rows: 'eight' (non-numeric string) fails."""
    yaml = """
labware:
  p:
    type: well_plate
    name: x
    model_name: x
    rows: eight
    columns: 12
    length: 127.71
    width: 85.43
    height: 14.10
    calibration:
      a1: { x: 0.0, y: 0.0, z: -15.0 }
      a2: { x: 9.0, y: 0.0, z: -15.0 }
    x_offset: 9.0
    y_offset: 9.0
    capacity_ul: 200.0
    working_volume_ul: 150.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        with pytest.raises(ValidationError):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


# ----- Volume validation -----

def test_working_volume_exceeds_capacity_fails():
    """working_volume_ul > capacity_ul must fail."""
    yaml = """
labware:
  p:
    type: well_plate
    name: x
    model_name: x
    rows: 8
    columns: 12
    length: 127.71
    width: 85.43
    height: 14.10
    calibration:
      a1: { x: 0.0, y: 0.0, z: -15.0 }
      a2: { x: 9.0, y: 0.0, z: -15.0 }
    x_offset: 9.0
    y_offset: 9.0
    capacity_ul: 200.0
    working_volume_ul: 250.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        with pytest.raises(ValidationError):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_negative_capacity_fails():
    """capacity_ul <= 0 fails."""
    yaml = """
labware:
  p:
    type: well_plate
    name: x
    model_name: x
    rows: 8
    columns: 12
    length: 127.71
    width: 85.43
    height: 14.10
    calibration:
      a1: { x: 0.0, y: 0.0, z: -15.0 }
      a2: { x: 9.0, y: 0.0, z: -15.0 }
    x_offset: 9.0
    y_offset: 9.0
    capacity_ul: 0.0
    working_volume_ul: 0.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        path = f.name
    try:
        with pytest.raises(ValidationError):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


# ----- _resolve_plate_orientation unit tests -----


def _make_entry(
    a1_x=0.0, a1_y=0.0, a2_x=10.0, a2_y=0.0,
    x_offset=10.0, y_offset=8.0, z=-5.0,
) -> WellPlateYamlEntry:
    """Build a minimal WellPlateYamlEntry for orientation tests."""
    return WellPlateYamlEntry(
        name="t", model_name="t",
        rows=2, columns=2,
        length=20.0, width=20.0, height=10.0,
        calibration=_YamlCalibrationPoints(
            a1=_YamlPoint3D(x=a1_x, y=a1_y, z=z),
            a2=_YamlPoint3D(x=a2_x, y=a2_y, z=z),
        ),
        x_offset=x_offset, y_offset=y_offset,
        capacity_ul=100.0, working_volume_ul=80.0,
    )


class TestResolvePlateOrientation:


    def test_negative_offsets_fail_schema_validation(self):
        with pytest.raises(ValidationError, match="greater than 0"):
            _make_entry(a1_x=10.0, a1_y=0.0, a2_x=0.0, a2_y=0.0,
                        x_offset=-10.0, y_offset=8.0)
        with pytest.raises(ValidationError, match="greater than 0"):
            _make_entry(a1_x=10.0, a1_y=0.0, a2_x=0.0, a2_y=0.0,
                        x_offset=10.0, y_offset=-8.0)


    def test_negative_y_column_step(self):
        entry = _make_entry(a1_x=0.0, a1_y=8.0, a2_x=0.0, a2_y=0.0,
                            x_offset=10.0, y_offset=8.0)
        orient = _resolve_plate_orientation(entry)
        assert orient.col_delta_y == pytest.approx(-8.0)
        assert orient.col_delta_x == pytest.approx(0.0)

    def test_negative_y_column_step_allows_positive_offset_magnitude(self):
        entry = _make_entry(a1_x=0.0, a1_y=8.0, a2_x=0.0, a2_y=0.0,
                            x_offset=10.0, y_offset=8.0)
        orient = _resolve_plate_orientation(entry)
        assert orient.col_delta_y == pytest.approx(-8.0)
        assert orient.col_delta_x == pytest.approx(0.0)

    def test_mismatched_x_offset_raises(self):
        entry = _make_entry(a1_x=0.0, a1_y=0.0, a2_x=10.0, a2_y=0.0,
                            x_offset=5.0, y_offset=8.0)
        with pytest.raises(ValueError, match="delta x magnitude must equal x_offset magnitude"):
            _resolve_plate_orientation(entry)

    def test_mismatched_y_offset_raises(self):
        entry = _make_entry(a1_x=0.0, a1_y=0.0, a2_x=0.0, a2_y=8.0,
                            x_offset=10.0, y_offset=4.0)
        with pytest.raises(ValueError, match="delta y magnitude must equal y_offset magnitude"):
            _resolve_plate_orientation(entry)


# ----- TipRack dimension forwarding -----

TIPRACK_WITH_EXPLICIT_DIMS = """
labware:
  rack:
    type: tip_rack
    name: test_rack
    rows: 1
    columns: 2
    length: 130.0
    width: 5.0
    height: 40.0
    pickup_z: 30.0
    tip_length: 59.3
    calibration:
      a1:
        x: 10.0
        y: 50.0
      a2:
        x: 110.0
        y: 50.0
    x_offset: 100.0
    y_offset: 1.0
"""

TIPRACK_WITHOUT_EXPLICIT_DIMS = """
labware:
  rack:
    type: tip_rack
    name: test_rack
    rows: 1
    columns: 2
    pickup_z: 30.0
    tip_length: 59.3
    calibration:
      a1:
        x: 10.0
        y: 50.0
      a2:
        x: 110.0
        y: 50.0
    x_offset: 100.0
    y_offset: 1.0
"""


class TestTipRackDimensionForwarding:


    def test_omitted_dimensions_auto_derived(self):
        """When dimensions are omitted, TipRack should auto-derive from tip positions."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(TIPRACK_WITHOUT_EXPLICIT_DIMS)
            path = f.name
        try:
            deck = load_deck_from_yaml(path)
            rack = deck["rack"]
            assert isinstance(rack, TipRack)
            # Auto-derived: length from tip spread (110-10=100), width clamped to 1.0
            assert rack.length == pytest.approx(100.0)
            assert rack.width == pytest.approx(1.0)
            # height auto-derives to 1.0 when drop_z is not provided
            assert rack.height == pytest.approx(1.0)
            assert rack.tip_length == pytest.approx(59.3)
        finally:
            Path(path).unlink(missing_ok=True)


TIPRACK_WITH_SIGNED_PICKUP_Z = """
labware:
  rack:
    type: tip_rack
    name: test_rack
    rows: 1
    columns: 1
    pickup_z: -20.0
    drop_z: -15.0
    tip_length: 59.3
    calibration:
      a1:
        x: -110.0
        y: -50.0
      a2:
        x: -10.0
        y: -50.0
    x_offset: 100.0
    y_offset: 1.0
"""


class TestTipRackSignedPickupZ:

    def test_signed_pickup_and_drop_z_accepted_by_schema(self):
        """home_origin decks need negative pickup_z/drop_z; gt=0 was removed."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(TIPRACK_WITH_SIGNED_PICKUP_Z)
            path = f.name
        try:
            deck = load_deck_from_yaml(path)
            rack = deck["rack"]
            assert isinstance(rack, TipRack)
            assert rack.pickup_z == pytest.approx(-20.0)
            assert rack.tips["A1"].z == pytest.approx(-20.0)
        finally:
            Path(path).unlink(missing_ok=True)


# ----- Wall labware -----

VALID_WALL = """
labware:
  front_wall:
    type: wall
    name: front_wall
    corner_1: { x: 96.0, y: 155.0, z: 0.0 }
    corner_2: { x: 226.0, y: 160.0, z: 40.0 }
"""


class TestWallLabware:

    def test_wall_loads_with_correct_corners(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(VALID_WALL)
            path = f.name
        try:
            deck = load_deck_from_yaml(path)
            wall = deck["front_wall"]
            assert isinstance(wall, Wall)
            assert wall.corner_1.x == pytest.approx(96.0)
            assert wall.corner_1.y == pytest.approx(155.0)
            assert wall.corner_1.z == pytest.approx(0.0)
            assert wall.corner_2.x == pytest.approx(226.0)
            assert wall.corner_2.y == pytest.approx(160.0)
            assert wall.corner_2.z == pytest.approx(40.0)
        finally:
            Path(path).unlink(missing_ok=True)


    def test_wall_bounding_box_properties(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(VALID_WALL)
            path = f.name
        try:
            deck = load_deck_from_yaml(path)
            wall = deck["front_wall"]
            assert wall.x_min == pytest.approx(96.0)
            assert wall.x_max == pytest.approx(226.0)
            assert wall.y_min == pytest.approx(155.0)
            assert wall.y_max == pytest.approx(160.0)
            assert wall.z_min == pytest.approx(0.0)
            assert wall.z_max == pytest.approx(40.0)
        finally:
            Path(path).unlink(missing_ok=True)


    def test_wall_inverted_corners_fails(self):
        yaml_str = """
labware:
  bad_wall:
    type: wall
    name: bad_wall
    corner_1: { x: 200.0, y: 50.0, z: 0.0 }
    corner_2: { x: 100.0, y: 55.0, z: 40.0 }
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_str)
            path = f.name
        try:
            with pytest.raises(Exception, match="corner_1.x must be < corner_2.x"):
                load_deck_from_yaml(path)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_wall_missing_corner_z_has_actionable_message(self):
        yaml_str = """
labware:
  bad_wall:
    type: wall
    name: bad_wall
    corner_1: { x: 10.0, y: 20.0 }
    corner_2: { x: 110.0, y: 25.0 }
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_str)
            path = f.name
        try:
            with pytest.raises(ValidationError) as exc_info:
                load_deck_from_yaml(path)
            message = str(exc_info.value)
            assert "corner_1.z" in message
            assert "corner_2.z" in message
            assert "explicit Z" in message
        finally:
            Path(path).unlink(missing_ok=True)

    def test_wall_rejects_extra_fields(self):
        yaml_str = """
labware:
  bad_wall:
    type: wall
    name: bad_wall
    corner_1: { x: 10.0, y: 20.0, z: 0.0 }
    corner_2: { x: 110.0, y: 25.0, z: 40.0 }
    slots:
      s1:
        location: { x: 15.0, y: 25.0, z: 5.0 }
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_str)
            path = f.name
        try:
            with pytest.raises(Exception):
                load_deck_from_yaml(path)
        finally:
            Path(path).unlink(missing_ok=True)


# ----- well_depth tests -----

WELL_DEPTH_DECK_YAML = """
labware:
  plate_1:
    type: well_plate
    name: deep_well_test
    rows: 8
    columns: 12
    height: 14.35
    well_depth: 10.67
    calibration:
      a1: { x: 10.0, y: 10.0, z: 25.9 }
      a2: { x: 19.0, y: 10.0, z: 25.9 }
    x_offset: 9.0
    y_offset: 9.0
"""


def test_well_plate_well_depth_negative_is_rejected():
    """Strictly negative inside depth is nonsensical and must fail."""
    bad_yaml = WELL_DEPTH_DECK_YAML.replace("well_depth: 10.67", "well_depth: -1.0")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(bad_yaml)
        path = f.name
    try:
        with pytest.raises(ValidationError, match="well_depth"):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_well_plate_well_depth_zero_is_rejected():
    """Boundary case: `gt=0` (not `ge=0`) means zero must also fail.

    Pinning the boundary explicitly so `gt=0 -> ge=0` regressions are caught.
    """
    bad_yaml = WELL_DEPTH_DECK_YAML.replace("well_depth: 10.67", "well_depth: 0")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(bad_yaml)
        path = f.name
    try:
        with pytest.raises(ValidationError, match="well_depth"):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_well_plate_well_depth_cannot_exceed_height():
    """Inside depth must fit within outer plate height.

    Catches the realistic miscalibration bug (e.g. swapped values) that the
    `gt=0` per-field check alone would let through. Uses a YAML that sets a
    sensible outer `height` and a clearly-too-large `well_depth`.
    """
    bad_yaml = WELL_DEPTH_DECK_YAML.replace("well_depth: 10.67", "well_depth: 50.0")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(bad_yaml)
        path = f.name
    try:
        with pytest.raises(ValidationError, match=r"well_depth.*height"):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_well_plate_direct_construction_rejects_negative_well_depth():
    """The runtime `WellPlate` model also enforces positivity, not just the
    YAML schema. Guards against future test-only or programmatic constructors
    bypassing schema validation.
    """
    a1 = Coordinate3D(x=10.0, y=10.0, z=25.9)
    wells = {f"{r}{c}": a1 for r in "ABCDEFGH" for c in range(1, 13)}
    with pytest.raises(ValidationError, match="well_depth"):
        WellPlate(
            name="bad",
            rows=8, columns=12,
            wells=wells,
            well_depth=-1.0,
        )


def test_nested_well_plate_carries_well_depth():
    """Wellplates declared inside a holder must also carry `well_depth`
    through to the WellPlate model.

    The top-level `_build_well_plate` auto-wires fields via
    `_entry_kwargs_for_model`, but `_build_nested_well_plate` is an explicit
    constructor — without this test, dropping the field there would not be
    caught by any other case.
    """
    yaml_str = """
labware:
  plate_holder:
    type: well_plate_holder
    name: plate_holder
    location: { x: 221.75, y: 78.5, z: 183.0 }
    well_plate:
      model_name: panda_96_wellplate
      rows: 2
      columns: 2
      well_depth: 9.5
      calibration:
        a1: { x: 221.75, y: 78.5 }
        a2: { x: 230.75, y: 78.5 }
      x_offset: 9.0
      y_offset: 9.0
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_str)
        path = f.name
    try:
        deck = load_deck_from_yaml(path)
        holder = deck["plate_holder"]
        nested = holder.contained_labware["plate"]
        assert isinstance(nested, WellPlate)
        assert nested.well_depth == pytest.approx(9.5)
    finally:
        Path(path).unlink(missing_ok=True)
