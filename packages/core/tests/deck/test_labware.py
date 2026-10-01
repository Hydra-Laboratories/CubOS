import pytest

from pydantic import ValidationError

from cubos.deck import (
    WellPlate,
    Vial,
    Coordinate3D,
    generate_wells_from_offsets,
)


def test_well_plate_requires_non_empty_name():
    """WellPlate requires a non-empty name on the concrete class."""
    with pytest.raises(ValidationError):
        WellPlate(
            name="",
            model_name="test_model",
            length=127.71,
            width=85.43,
            height=14.10,
            rows=8,
            columns=12,
            wells={"A1": Coordinate3D(x=10.0, y=10.0, z=15.0)},
            capacity_ul=200.0,
            working_volume_ul=150.0,
        )

    with pytest.raises(ValidationError):
        Vial(
            name="  ",
            model_name="test_model",
            height=66.75,
            diameter=28.0,
            location=Coordinate3D(x=30.0, y=40.0, z=20.0),
            capacity_ul=1500.0,
            working_volume_ul=1200.0,
        )


def test_well_plate_get_location_success_and_failure():
    """WellPlate get_location delegates to the well ID mapping."""
    plate = WellPlate(
        name="SBS_96",
        model_name="test_model",
        length=127.71,
        width=85.43,
        height=14.10,
        rows=1,
        columns=1,
        wells={"A1": Coordinate3D(x=10.0, y=20.0, z=15.0)},
        capacity_ul=200.0,
        working_volume_ul=150.0,
    )

    center = plate.get_location("A1")
    assert isinstance(center, Coordinate3D)
    assert center.x == pytest.approx(10.0)
    assert center.y == pytest.approx(20.0)
    assert center.z == pytest.approx(15.0)

    with pytest.raises(KeyError, match="Unknown well ID 'B1'"):
        plate.get_location("B1")

    with pytest.raises(KeyError, match="location_id is required"):
        plate.get_location()


def test_vial_get_location_success_and_failure():
    """Single-vial get_location supports default and name lookup."""
    vial = Vial(
        name="vial_1",
        model_name="test_model",
        height=66.75,
        diameter=28.0,
        location=Coordinate3D(x=30.0, y=40.0, z=20.0),
        capacity_ul=1500.0,
        working_volume_ul=1200.0,
    )

    assert vial.get_location() == Coordinate3D(x=30.0, y=40.0, z=20.0)
    assert vial.get_location("vial_1") == Coordinate3D(x=30.0, y=40.0, z=20.0)

    with pytest.raises(ValidationError):
        Vial(
            name="vial_1",
            model_name="test_model",
            height=66.75,
            diameter=28.0,
            capacity_ul=1500.0,
            working_volume_ul=1200.0,
        )

    with pytest.raises(KeyError, match="Unknown location ID 'B1'"):
        vial.get_location("B1")
    with pytest.raises(KeyError, match="Unknown location ID 'A1'"):
        vial.get_location("A1")


def test_well_plate_sbs_96_dimensions_and_well_lookup():
    """WellPlate captures dimensions and resolves wells by ID."""
    wells = {
        "A1": Coordinate3D(x=10.0, y=10.0, z=15.0),
        "B1": Coordinate3D(x=10.0, y=20.0, z=15.0),
    }

    plate = WellPlate(
        name="SBS_96",
        model_name="test_model",
        length=127.71,
        width=85.43,
        height=14.10,
        rows=2,
        columns=1,
        wells=wells,
        capacity_ul=200.0,
        working_volume_ul=150.0,
    )

    assert plate.name == "SBS_96"
    assert plate.length == pytest.approx(127.71)
    assert plate.width == pytest.approx(85.43)
    assert plate.height == pytest.approx(14.10)
    assert plate.rows == 2
    assert plate.columns == 1

    # Well lookup using ergonomic API
    a1_center = plate.get_well_center("A1")
    assert a1_center.x == pytest.approx(10.0)
    assert a1_center.y == pytest.approx(10.0)
    assert a1_center.z == pytest.approx(15.0)

    # Falling back to base Labware API should also work
    assert plate.get_location("A1") == a1_center

    # Initial position for a well plate should be A1
    initial = plate.get_initial_position()
    assert initial == a1_center

    # Invalid well ID should raise a clear error
    with pytest.raises(KeyError, match="Unknown well ID 'Z9'"):
        plate.get_well_center("Z9")


def test_well_plate_requires_positive_dimensions_and_wells():
    """WellPlate validates dimensions and requires at least one well."""
    with pytest.raises(ValidationError):
        WellPlate(
            name="invalid_plate",
            model_name="test_model",
            length=-1.0,
            width=85.43,
            height=14.10,
            rows=8,
            columns=12,
            wells={},
            capacity_ul=200.0,
            working_volume_ul=150.0,
        )

    # Missing A1 should also fail, since A1 is the anchor position
    with pytest.raises(ValidationError):
        WellPlate(
            name="missing_a1",
            model_name="test_model",
            length=127.71,
            width=85.43,
            height=14.10,
            rows=8,
            columns=12,
            wells={"B1": Coordinate3D(x=10.0, y=20.0, z=15.0)},
            capacity_ul=200.0,
            working_volume_ul=150.0,
        )


def test_vial_requires_positive_diameter():
    """Vial validates diameter must be positive."""
    with pytest.raises(ValidationError):
        Vial(
            name="invalid_vial",
            model_name="test_model",
            height=-20.0,
            diameter=0.0,
            location=Coordinate3D(x=-30.0, y=-40.0, z=-20.0),
            capacity_ul=1500.0,
            working_volume_ul=1200.0,
        )


def test_well_plate_extra_field_rejected():
    """WellPlate rejects unknown extra fields (strict schema)."""
    wells = {"A1": Coordinate3D(x=0.0, y=0.0, z=15.0)}
    with pytest.raises(ValidationError):
        WellPlate(
            name="x",
            model_name="test_model",
            length=127.71,
            width=85.43,
            height=14.10,
            rows=8,
            columns=12,
            wells=wells,
            capacity_ul=200.0,
            working_volume_ul=150.0,
            unknown_field=1,
        )


def test_well_plate_volume_required_and_working_le_capacity():
    """WellPlate requires capacity_ul and working_volume_ul; working_volume_ul <= capacity_ul."""
    wells = {"A1": Coordinate3D(x=0.0, y=0.0, z=15.0)}
    with pytest.raises(ValidationError):
        WellPlate(
            name="x",
            model_name="test_model",
            length=127.71,
            width=85.43,
            height=14.10,
            rows=8,
            columns=12,
            wells=wells,
            capacity_ul=200.0,
            working_volume_ul=250.0,
        )
    plate = WellPlate(
        name="x",
        model_name="test_model",
        length=127.71,
        width=85.43,
        height=14.10,
        rows=1,
        columns=1,
        wells=wells,
        capacity_ul=200.0,
        working_volume_ul=150.0,
    )
    assert plate.capacity_ul == 200.0
    assert plate.working_volume_ul == 150.0


def test_vial_extra_field_rejected():
    """Vial rejects unknown extra fields (strict schema)."""
    with pytest.raises(ValidationError):
        Vial(
            name="v",
            model_name="test_model",
            height=66.0,
            diameter=28.0,
            location=Coordinate3D(x=30.0, y=40.0, z=20.0),
            capacity_ul=1500.0,
            working_volume_ul=1200.0,
            unknown_field=1,
        )


def test_vial_volume_required_and_working_le_capacity():
    """Vial requires capacity_ul and working_volume_ul; working_volume_ul <= capacity_ul."""
    with pytest.raises(ValidationError):
        Vial(
            name="v",
            model_name="test_model",
            height=66.0,
            diameter=28.0,
            location=Coordinate3D(x=30.0, y=40.0, z=20.0),
            capacity_ul=1000.0,
            working_volume_ul=1200.0,
        )


def test_generate_wells_from_offsets():
    """Generate a small 2x2 well layout from A1 anchor and offsets."""
    row_labels = ["A", "B"]
    column_indices = [1, 2]
    a1_center = Coordinate3D(x=0.0, y=0.0, z=15.0)

    wells = generate_wells_from_offsets(
        row_labels=row_labels,
        column_indices=column_indices,
        a1_center=a1_center,
        x_offset=10.0,
        y_offset=5.0,
        rounding_decimals=3,
    )

    # Expect 4 wells: A1, A2, B1, B2
    assert set(wells.keys()) == {"A1", "A2", "B1", "B2"}

    # A1 is the anchor
    assert wells["A1"].x == pytest.approx(0.0)
    assert wells["A1"].y == pytest.approx(0.0)
    assert wells["A1"].z == pytest.approx(15.0)

    # A2: one column step in X
    assert wells["A2"].x == pytest.approx(10.0)
    assert wells["A2"].y == pytest.approx(0.0)

    # B1: one row step in Y
    assert wells["B1"].x == pytest.approx(0.0)
    assert wells["B1"].y == pytest.approx(5.0)

    # B2: both row and column offsets applied
    assert wells["B2"].x == pytest.approx(10.0)
    assert wells["B2"].y == pytest.approx(5.0)


def test_coordinate3d_rejects_non_finite_values():
    """Coordinate3D must reject NaN and infinite values."""
    with pytest.raises(ValidationError):
        Coordinate3D(x=float("nan"), y=0.0, z=0.0)
    with pytest.raises(ValidationError):
        Coordinate3D(x=0.0, y=float("inf"), z=0.0)
    with pytest.raises(ValidationError):
        Coordinate3D(x=0.0, y=0.0, z=float("-inf"))


def test_well_plate_requires_exact_rows_columns_well_count():
    """WellPlate wells mapping count must match rows * columns exactly."""
    with pytest.raises(ValidationError):
        WellPlate(
            name="count_mismatch",
            model_name="test_model",
            length=127.71,
            width=85.43,
            height=14.10,
            rows=8,
            columns=12,
            wells={"A1": Coordinate3D(x=0.0, y=0.0, z=15.0)},
            capacity_ul=200.0,
            working_volume_ul=150.0,
        )


def test_well_plate_rows_limited_to_26():
    """WellPlate rows are limited to A-Z."""
    with pytest.raises(ValidationError):
        WellPlate(
            name="too_many_rows",
            model_name="test_model",
            length=127.71,
            width=85.43,
            height=14.10,
            rows=27,
            columns=1,
            wells={"A1": Coordinate3D(x=0.0, y=0.0, z=15.0)},
            capacity_ul=200.0,
            working_volume_ul=150.0,
        )
