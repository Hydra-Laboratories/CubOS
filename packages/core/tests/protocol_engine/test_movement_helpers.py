"""Tests for the shared movement helpers used by engaging commands."""

from unittest.mock import MagicMock

import pytest

from cubos.deck.labware.labware import Coordinate3D
from cubos.protocol_engine.commands._movement import (
    _assert_finite_number,
    engage_at_labware,
    unpack_xyz,
)


class TestUnpackXyz:

    def test_tuple(self):
        assert unpack_xyz((1.0, 2.0, 3.0)) == (1.0, 2.0, 3.0)

    def test_list(self):
        assert unpack_xyz([4.0, 5.0, 6.0]) == (4.0, 5.0, 6.0)

    def test_coordinate3d(self):
        assert unpack_xyz(Coordinate3D(x=1.5, y=2.5, z=3.5)) == (1.5, 2.5, 3.5)

    def test_object_with_xyz_attrs(self):
        obj = MagicMock()
        obj.x, obj.y, obj.z = 10.0, 20.0, 30.0
        assert unpack_xyz(obj) == (10.0, 20.0, 30.0)


class TestAssertFiniteNumber:

    def test_accepts_int_and_float(self):
        _assert_finite_number(0, field_name="x", source="test")
        _assert_finite_number(1.5, field_name="x", source="test")
        _assert_finite_number(-1.0, field_name="x", source="test")

    @pytest.mark.parametrize("bad", ["", "1.0", "abc", float("nan"), float("inf"), True, False, None])
    def test_rejects_non_finite_or_wrong_type(self, bad):
        with pytest.raises(ValueError, match="must be a finite number"):
            _assert_finite_number(bad, field_name="x", source="test")


def _mock_ctx_with_well(well_z=14.10):
    """Build a context whose deck.resolve_coordinate returns a coord at *well_z*.

    The well/labware coordinate's Z is the labware-surface reference Z
    that ``engage_at_labware`` adds ``measurement_height`` to.
    """
    instr = MagicMock()
    board = MagicMock()
    board.instruments = {"sensor": instr}
    deck = MagicMock()
    deck.resolve_coordinate.return_value = Coordinate3D(x=10.0, y=20.0, z=well_z)
    ctx = MagicMock()
    ctx.gantry = board
    ctx.deck = deck
    return ctx, instr


class TestEngageAtLabware:

    def test_descends_to_well_z_plus_offset(self):
        ctx, _ = _mock_ctx_with_well(well_z=14.10)
        _, _, well_z, action_z = engage_at_labware(
            ctx, "sensor", "plate.A1",
            measurement_height=2.0, command_label="measure",
        )
        assert well_z == pytest.approx(14.10)
        assert action_z == pytest.approx(16.10)
        ctx.gantry.move_to_labware.assert_called_once()
        ctx.gantry.move.assert_called_once_with("sensor", (10.0, 20.0, 16.10))

    def test_negative_offset_descends_below_surface(self):
        ctx, _ = _mock_ctx_with_well(well_z=14.10)
        _, _, well_z, action_z = engage_at_labware(
            ctx, "sensor", "plate.A1",
            measurement_height=-1.0, command_label="measure",
        )
        assert well_z == pytest.approx(14.10)
        assert action_z == pytest.approx(13.10)

    @pytest.mark.parametrize("bad", ["", "1.0", float("nan"), True])
    def test_rejects_non_finite_measurement_height(self, bad):
        ctx, _ = _mock_ctx_with_well(well_z=14.10)
        with pytest.raises(ValueError, match="finite number"):
            engage_at_labware(
                ctx, "sensor", "plate.A1",
                measurement_height=bad, command_label="measure",
            )
