"""Tests for deck and gantry bounds validation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cubos.gantry.instrument_mount import InstrumentedGantry
from cubos.deck.deck import Deck
from cubos.deck.labware.labware import Coordinate3D
from cubos.deck.labware.vial import Vial
from cubos.deck.labware.well_plate import WellPlate
from cubos.deck.labware.well_plate_holder import WellPlateHolder
from cubos.instruments.base_instrument import BaseInstrument
from cubos.gantry.gantry_config import GantryConfig, GantryType, OriginPolicy, WorkingVolume
from cubos.protocol_engine.protocol import Protocol, ProtocolStep
from cubos.validation.bounds import (
    collect_protocol_motion_targets,
    validate_deck_positions,
    validate_gantry_positions,
    validate_protocol_motion_bounds,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_gantry(
    x_min: float = 0.0,
    x_max: float = 300.0,
    y_min: float = 0.0,
    y_max: float = 200.0,
    z_min: float = 0.0,
    z_max: float = 80.0,
    factory_z_travel_mm: float = 90.0,
    safe_z: float | None = None,
    origin_policy: OriginPolicy = OriginPolicy.DECK_ORIGIN,
) -> GantryConfig:
    return GantryConfig(
        serial_port="/dev/ttyUSB0",
        gantry_type=GantryType.CUB_XL,
        factory_z_travel_mm=factory_z_travel_mm,
        working_volume=WorkingVolume(
            x_min=x_min, x_max=x_max,
            y_min=y_min, y_max=y_max,
            z_min=z_min, z_max=z_max,
        ),
        safe_z=safe_z,
        origin_policy=origin_policy,
    )


def _make_signed_gantry(*, safe_z: float | None = -5.0) -> GantryConfig:
    """Home-origin gantry matching the mock_panda_signed.yaml fixture bounds."""
    return _make_gantry(
        x_min=-400.0, x_max=0.0,
        y_min=-300.0, y_max=0.0,
        z_min=-100.0, z_max=0.0,
        factory_z_travel_mm=100.0,
        safe_z=safe_z,
        origin_policy=OriginPolicy.HOME_ORIGIN,
    )


def _make_vial(
    name: str = "test_vial",
    x: float = 30.0,
    y: float = 40.0,
    z: float = 20.0,
) -> Vial:
    return Vial(
        name=name,
        model_name="standard_vial",
        height=66.75,
        diameter=28.0,
        location=Coordinate3D(x=x, y=y, z=z),
        capacity_ul=1500.0,
        working_volume_ul=1200.0,
    )


def _make_plate(
    name: str = "test_plate",
    a1_x: float = 10.0,
    a1_y: float = 10.0,
    a1_z: float = 15.0,
    rows: int = 2,
    columns: int = 2,
    x_offset: float = 5.0,
    y_offset: float = 5.0,
) -> WellPlate:
    """Create a small well plate with derived wells."""
    wells = {}
    row_labels = [chr(65 + r) for r in range(rows)]
    for r_idx, row in enumerate(row_labels):
        for c_idx in range(columns):
            well_id = f"{row}{c_idx + 1}"
            wells[well_id] = Coordinate3D(
                x=a1_x + x_offset * c_idx,
                y=a1_y + y_offset * r_idx,
                z=a1_z,
            )
    return WellPlate(
        name=name,
        model_name="test_plate_model",
        length=50.0,
        width=30.0,
        height=14.0,
        rows=rows,
        columns=columns,
        wells=wells,
        capacity_ul=200.0,
        working_volume_ul=150.0,
    )


def _make_deck(**labware) -> Deck:
    return Deck(labware)


def _make_instrument(
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    depth: float = 0.0,
) -> BaseInstrument:
    instr = MagicMock(spec=BaseInstrument)
    instr.offset_x = offset_x
    instr.offset_y = offset_y
    instr.depth = depth
    instr.name = "mock_instrument"
    return instr


def _make_instrumented_gantry(*instruments: tuple) -> InstrumentedGantry:
    """Build a InstrumentedGantry with named instruments and a mock gantry."""
    gantry = MagicMock()
    instr_dict = {}
    for name, instr in instruments:
        instr_dict[name] = instr
    return InstrumentedGantry(controller=gantry, instruments=instr_dict)


def _make_protocol(command_name: str, **args) -> Protocol:
    return Protocol([
        ProtocolStep(
            index=0,
            command_name=command_name,
            handler=MagicMock(),
            args=args,
        ),
    ])


# ── Deck position validation ────────────────────────────────────────────


class TestValidateDeckPositions:

    def test_all_positions_within_bounds_passes(self):
        gantry = _make_gantry()
        deck = _make_deck(vial_1=_make_vial(x=30.0, y=40.0, z=20.0))
        violations = validate_deck_positions(gantry, deck)
        assert violations == []

    def test_well_plate_all_wells_within_bounds_passes(self):
        gantry = _make_gantry()
        plate = _make_plate(a1_x=10.0, a1_y=10.0, a1_z=15.0)
        deck = _make_deck(plate_1=plate)
        violations = validate_deck_positions(gantry, deck)
        assert violations == []

    def test_vial_outside_x_min_fails(self):
        gantry = _make_gantry(x_min=0.0)
        deck = _make_deck(vial_1=_make_vial(x=-0.001))
        violations = validate_deck_positions(gantry, deck)
        assert len(violations) == 1
        assert violations[0].labware_key == "vial_1"
        assert violations[0].position_id == "location"
        assert violations[0].axis == "x"
        assert violations[0].bound_name == "x_min"

    def test_vial_outside_x_max_fails(self):
        gantry = _make_gantry(x_max=300.0)
        deck = _make_deck(vial_1=_make_vial(x=300.001))
        violations = validate_deck_positions(gantry, deck)
        assert len(violations) == 1
        assert violations[0].axis == "x"
        assert violations[0].bound_name == "x_max"

    def test_well_plate_corner_well_outside_y_max_fails(self):
        gantry = _make_gantry(y_max=20.0)
        # B1 well will be at y = 10.0 + 15.0 = 25.0, outside y_max=20
        plate = _make_plate(a1_y=10.0, y_offset=15.0)
        deck = _make_deck(plate_1=plate)
        violations = validate_deck_positions(gantry, deck)
        assert len(violations) > 0
        lw_keys = {v.labware_key for v in violations}
        assert "plate_1" in lw_keys

    def test_well_plate_corner_well_outside_z_min_fails(self):
        gantry = _make_gantry(z_min=20.0)
        plate = _make_plate(a1_z=15.0)
        deck = _make_deck(plate_1=plate)
        violations = validate_deck_positions(gantry, deck)
        assert len(violations) > 0
        assert all(v.axis == "z" for v in violations)

    def test_position_exactly_on_boundary_passes(self):
        gantry = _make_gantry(x_min=0.0)
        deck = _make_deck(vial_1=_make_vial(x=0.0))
        violations = validate_deck_positions(gantry, deck)
        assert violations == []

    def test_position_epsilon_beyond_boundary_fails(self):
        gantry = _make_gantry(x_min=0.0)
        deck = _make_deck(vial_1=_make_vial(x=-0.001))
        violations = validate_deck_positions(gantry, deck)
        assert len(violations) == 1

    def test_empty_deck_passes(self):
        gantry = _make_gantry()
        deck = _make_deck()
        violations = validate_deck_positions(gantry, deck)
        assert violations == []

    def test_mixed_labware_one_out_of_bounds_fails(self):
        gantry = _make_gantry(x_min=0.0)
        vial_ok = _make_vial(name="ok_vial", x=100.0)
        vial_bad = _make_vial(name="bad_vial", x=-1.0)
        deck = _make_deck(ok=vial_ok, bad=vial_bad)
        violations = validate_deck_positions(gantry, deck)
        assert len(violations) == 1
        assert violations[0].labware_key == "bad"

    def test_multiple_violations_reported(self):
        gantry = _make_gantry(x_min=50.0, y_min=50.0)
        vial_bad_x = _make_vial(name="v1", x=20.0, y=80.0, z=20.0)
        vial_bad_y = _make_vial(name="v2", x=80.0, y=20.0, z=20.0)
        deck = _make_deck(v1=vial_bad_x, v2=vial_bad_y)
        violations = validate_deck_positions(gantry, deck)
        assert len(violations) == 2

    def test_violation_identifies_labware_and_position(self):
        gantry = _make_gantry(x_min=50.0)
        deck = _make_deck(my_vial=_make_vial(x=20.0))
        violations = validate_deck_positions(gantry, deck)
        assert violations[0].labware_key == "my_vial"
        assert violations[0].position_id == "location"
        assert violations[0].coordinate_type == "deck"
        assert violations[0].instrument_name is None


# ── Gantry position validation ──────────────────────────────────────────


class TestValidateGantryPositions:

    def test_all_gantry_positions_within_bounds_passes(self):
        gantry = _make_gantry()
        deck = _make_deck(vial_1=_make_vial(x=30.0, y=40.0, z=20.0))
        instr = _make_instrument(offset_x=5.0, offset_y=0.0, depth=0.0)
        instrumented_gantry = _make_instrumented_gantry(("instr_1", instr))
        violations = validate_gantry_positions(gantry, deck, instrumented_gantry)
        assert violations == []

    def test_instrument_offset_pushes_gantry_outside_x_max_fails(self):
        # vial at x=299.0, instrument offset_x=-5.0
        # gantry_x = 299.0 - (-5.0) = 304.0 > x_max=300.0
        gantry = _make_gantry(x_max=300.0)
        deck = _make_deck(vial_1=_make_vial(x=299.0, y=40.0, z=20.0))
        instr = _make_instrument(offset_x=-5.0)
        instrumented_gantry = _make_instrumented_gantry(("probe", instr))
        violations = validate_gantry_positions(gantry, deck, instrumented_gantry)
        assert len(violations) >= 1
        assert any(v.axis == "x" and v.bound_name == "x_max" for v in violations)

    def test_instrument_offset_pushes_gantry_outside_x_min_fails(self):
        # vial at x=5.0, instrument offset_x=10.0
        # gantry_x = 5.0 - 10.0 = -5.0 < x_min=0.0
        gantry = _make_gantry(x_min=0.0)
        deck = _make_deck(vial_1=_make_vial(x=5.0, y=40.0, z=20.0))
        instr = _make_instrument(offset_x=10.0)
        instrumented_gantry = _make_instrumented_gantry(("probe", instr))
        violations = validate_gantry_positions(gantry, deck, instrumented_gantry)
        assert len(violations) >= 1
        assert any(v.axis == "x" and v.bound_name == "x_min" for v in violations)

    def test_instrument_depth_pushes_gantry_outside_z_max_fails(self):
        # vial at z=75.0, instrument depth=10.0
        # gantry_z = 75.0 + 10.0 = 85.0 > z_max=80.0
        gantry = _make_gantry(z_max=80.0)
        deck = _make_deck(vial_1=_make_vial(x=30.0, y=40.0, z=75.0))
        instr = _make_instrument(depth=10.0)
        instrumented_gantry = _make_instrumented_gantry(("deep_instr", instr))
        violations = validate_gantry_positions(gantry, deck, instrumented_gantry)
        assert len(violations) >= 1
        assert any(v.axis == "z" and v.bound_name == "z_max" for v in violations)

    def test_gantry_position_exactly_on_boundary_passes(self):
        # vial at x=5.0, offset_x=5.0 -> gantry_x = 0.0 = x_min
        gantry = _make_gantry(x_min=0.0)
        deck = _make_deck(vial_1=_make_vial(x=5.0, y=40.0, z=20.0))
        instr = _make_instrument(offset_x=5.0)
        instrumented_gantry = _make_instrumented_gantry(("instr_1", instr))
        violations = validate_gantry_positions(gantry, deck, instrumented_gantry)
        assert violations == []

    def test_gantry_position_epsilon_beyond_boundary_fails(self):
        # vial at x=4.999, offset_x=5.0 -> gantry_x = -0.001 < x_min=0.0
        gantry = _make_gantry(x_min=0.0)
        deck = _make_deck(vial_1=_make_vial(x=4.999, y=40.0, z=20.0))
        instr = _make_instrument(offset_x=5.0)
        instrumented_gantry = _make_instrumented_gantry(("instr_1", instr))
        violations = validate_gantry_positions(gantry, deck, instrumented_gantry)
        assert len(violations) >= 1

    def test_zero_offset_instrument_gantry_equals_deck_position(self):
        gantry = _make_gantry()
        deck = _make_deck(vial_1=_make_vial(x=30.0, y=40.0, z=20.0))
        instr = _make_instrument(offset_x=0.0, offset_y=0.0, depth=0.0)
        instrumented_gantry = _make_instrumented_gantry(("instr_1", instr))
        violations = validate_gantry_positions(gantry, deck, instrumented_gantry)
        assert violations == []

    def test_multiple_instruments_one_ok_one_fails(self):
        gantry = _make_gantry(x_min=0.0)
        deck = _make_deck(vial_1=_make_vial(x=2.0, y=40.0, z=20.0))
        ok_instr = _make_instrument(offset_x=0.0)
        bad_instr = _make_instrument(offset_x=5.0)
        instrumented_gantry = _make_instrumented_gantry(("ok", ok_instr), ("bad", bad_instr))
        violations = validate_gantry_positions(gantry, deck, instrumented_gantry)
        assert len(violations) >= 1
        instr_names = {v.instrument_name for v in violations}
        assert "bad" in instr_names
        assert "ok" not in instr_names

    def test_error_identifies_instrument_labware_and_position(self):
        gantry = _make_gantry(x_min=0.0)
        deck = _make_deck(my_vial=_make_vial(x=2.0, y=40.0, z=20.0))
        instr = _make_instrument(offset_x=5.0)
        instrumented_gantry = _make_instrumented_gantry(("my_probe", instr))
        violations = validate_gantry_positions(gantry, deck, instrumented_gantry)
        assert violations[0].instrument_name == "my_probe"
        assert violations[0].labware_key == "my_vial"
        assert violations[0].position_id == "location"
        assert violations[0].coordinate_type == "gantry"

    def test_empty_deck_passes_gantry_validation(self):
        gantry = _make_gantry()
        deck = _make_deck()
        instr = _make_instrument(offset_x=-15.0)
        instrumented_gantry = _make_instrumented_gantry(("instr_1", instr))
        violations = validate_gantry_positions(gantry, deck, instrumented_gantry)
        assert violations == []

    def test_empty_board_no_instruments_passes(self):
        gantry = _make_gantry()
        deck = _make_deck(vial_1=_make_vial())
        instrumented_gantry = _make_instrumented_gantry()
        violations = validate_gantry_positions(gantry, deck, instrumented_gantry)
        assert violations == []

    def test_well_plate_gantry_validation_checks_all_wells(self):
        # 2x2 plate: A1.x=10, A2.x=15. instrument offset_x=15.
        # A1 gantry_x = 10 - 15 = -5 (out of bounds), A2 gantry_x = 15 - 15 = 0 (boundary).
        gantry = _make_gantry(x_min=0.0)
        plate = _make_plate(a1_x=10.0, a1_y=10.0, a1_z=15.0)
        deck = _make_deck(plate_1=plate)
        instr = _make_instrument(offset_x=15.0)
        instrumented_gantry = _make_instrumented_gantry(("big_offset", instr))
        violations = validate_gantry_positions(gantry, deck, instrumented_gantry)
        # At least one well should violate x_min.
        assert len(violations) > 0
        assert all(v.instrument_name == "big_offset" for v in violations)

class TestValidateProtocolMotionBounds:

    def test_planned_deck_move_validates_exact_carriage_ceiling_pose(self):
        gantry = _make_gantry(
            x_max=25.8, y_max=144.645, z_min=10.5, z_max=66.5,
            safe_z=66.5,
        )
        deck = Deck(
            {"plate": _make_plate(a1_x=25.803, a1_y=70.035, a1_z=38.5)},
            motion_planning={"clearance_mm": 2.0},
        )
        camera = _make_instrument(offset_x=0.0, offset_y=-46.5, depth=-114.964)
        instrumented_gantry = _make_instrumented_gantry(("camera", camera))
        protocol = _make_protocol(
            "move", instrument="camera", position="plate.A1",
        )

        violations = validate_protocol_motion_bounds(
            gantry, protocol, deck, instrumented_gantry,
        )

        assert len(violations) == 1
        assert violations[0].axis == "x"
        assert (violations[0].x, violations[0].y, violations[0].z) == pytest.approx(
            (25.803, 116.535, 66.5),
        )

    def test_planned_literal_move_keeps_instrument_depth_bounds(self):
        gantry = _make_gantry(z_min=10.5, z_max=66.5, safe_z=66.5)
        deck = Deck({}, motion_planning={"clearance_mm": 2.0})
        camera = _make_instrument(depth=-114.964)
        instrumented_gantry = _make_instrumented_gantry(("camera", camera))
        protocol = _make_protocol(
            "move", instrument="camera", position=[25.803, 70.035, 66.5],
        )

        violations = validate_protocol_motion_bounds(
            gantry, protocol, deck, instrumented_gantry,
        )

        assert len(violations) == 1
        assert violations[0].axis == "z"
        assert violations[0].z == pytest.approx(-48.464)

    def test_legacy_deck_move_keeps_safe_z_instrument_depth_bounds(self):
        gantry = _make_gantry(z_min=10.5, z_max=66.5, safe_z=66.5)
        deck = _make_deck(
            plate=_make_plate(a1_x=25.803, a1_y=70.035, a1_z=38.5),
        )
        camera = _make_instrument(depth=-114.964)
        instrumented_gantry = _make_instrumented_gantry(("camera", camera))
        protocol = _make_protocol(
            "move", instrument="camera", position="plate.A1",
        )

        violations = validate_protocol_motion_bounds(
            gantry, protocol, deck, instrumented_gantry,
        )

        assert len(violations) == 1
        assert violations[0].axis == "z"
        assert violations[0].z == pytest.approx(-48.464)

    def test_unused_out_of_bounds_labware_is_not_validated(self):
        gantry = _make_gantry(z_min=0.0, z_max=80.0, safe_z=80.0)
        deck = _make_deck(
            used=_make_vial(x=30.0, y=40.0, z=20.0),
            unused=_make_vial(x=-10.0, y=40.0, z=20.0),
        )
        instrumented_gantry = _make_instrumented_gantry(("probe", _make_instrument()))
        protocol = _make_protocol(
            "measure",
            instrument="probe",
            position="used",
            measurement_height=0.0,
        )

        assert validate_protocol_motion_bounds(gantry, protocol, deck, instrumented_gantry) == []

    def test_measure_validates_only_referenced_well(self):
        gantry = _make_gantry(y_max=20.0, safe_z=80.0)
        plate = _make_plate(a1_y=10.0, y_offset=15.0)
        deck = _make_deck(plate=plate)
        instrumented_gantry = _make_instrumented_gantry(("probe", _make_instrument()))
        protocol = _make_protocol(
            "measure",
            instrument="probe",
            position="plate.A1",
            measurement_height=0.0,
        )

        assert validate_protocol_motion_bounds(gantry, protocol, deck, instrumented_gantry) == []

        protocol = _make_protocol(
            "measure",
            instrument="probe",
            position="plate.B1",
            measurement_height=0.0,
        )
        violations = validate_protocol_motion_bounds(gantry, protocol, deck, instrumented_gantry)

        assert violations
        assert {v.position_id for v in violations} == {"B1.safe_z", "B1.action_z"}

    def test_scan_validates_every_well_it_expands_to(self):
        gantry = _make_gantry(y_max=20.0, safe_z=80.0)
        plate = _make_plate(a1_y=10.0, y_offset=15.0)
        deck = _make_deck(plate=plate)
        instrumented_gantry = _make_instrumented_gantry(("probe", _make_instrument()))
        protocol = _make_protocol(
            "scan",
            instrument="probe",
            plate="plate",
            method="measure",
            measurement_height=0.0,
            interwell_scan_height=1.0,
        )

        violations = validate_protocol_motion_bounds(gantry, protocol, deck, instrumented_gantry)

        assert violations
        assert any(v.position_id == "B1.action_z" for v in violations)
        assert any(v.position_id == "B2.action_z" for v in violations)

    def test_gantry_bounds_use_only_the_command_instrument(self):
        gantry = _make_gantry(x_min=0.0, safe_z=80.0)
        deck = _make_deck(vial=_make_vial(x=30.0, y=40.0, z=20.0))
        instrumented_gantry = _make_instrumented_gantry(
            ("used", _make_instrument(offset_x=0.0)),
            ("unused_bad_offset", _make_instrument(offset_x=100.0)),
        )
        protocol = _make_protocol(
            "measure",
            instrument="used",
            position="vial",
            measurement_height=0.0,
        )

        assert validate_protocol_motion_bounds(gantry, protocol, deck, instrumented_gantry) == []

    def test_holder_base_is_ignored_when_protocol_targets_nested_labware(self):
        gantry = _make_gantry(z_min=80.0, z_max=101.5, factory_z_travel_mm=101.5,
                              safe_z=101.5)
        plate = _make_plate(a1_x=15.0, a1_y=17.0, a1_z=89.5)
        holder = WellPlateHolder(
            name="plate_holder",
            location=Coordinate3D(x=0.0, y=0.0, z=0.0),
            contained_labware={"plate": plate},
        )
        deck = _make_deck(plate_holder=holder)
        instrumented_gantry = _make_instrumented_gantry(("probe", _make_instrument()))
        protocol = _make_protocol(
            "measure",
            instrument="probe",
            position="plate_holder.plate.A1",
            measurement_height=1.0,
        )

        assert validate_protocol_motion_bounds(gantry, protocol, deck, instrumented_gantry) == []

    def test_collector_reports_scan_safe_approach_and_action_targets(self):
        gantry = _make_gantry(safe_z=80.0)
        deck = _make_deck(plate=_make_plate(rows=1, columns=2))
        protocol = _make_protocol(
            "scan",
            instrument="probe",
            plate="plate",
            method="measure",
            measurement_height=0.0,
            interwell_scan_height=1.0,
        )

        targets = collect_protocol_motion_targets(gantry, protocol, deck)

        assert [target.position_id for target in targets] == [
            "A1.safe_z",
            "A1.approach_z",
            "A1.action_z",
            "A2.approach_z",
            "A2.action_z",
        ]

    def test_collector_reports_measure_indentation_limit_target(self):
        gantry = _make_gantry(safe_z=80.0)
        deck = _make_deck(plate=_make_plate(rows=1, columns=1, a1_z=20.0))
        protocol = _make_protocol(
            "measure",
            instrument="probe",
            position="plate.A1",
            measurement_height=-1.0,
            indentation_limit_height=-5.0,
        )

        targets = collect_protocol_motion_targets(gantry, protocol, deck)

        assert [target.position_id for target in targets] == [
            "A1.safe_z",
            "A1.action_z",
            "A1.indentation_limit_z",
        ]
        assert targets[-1].z == 15.0

    def test_collector_reports_detect_surface_worst_case_depth(self):
        """With detect_surface the deepest statically-checkable Z is the
        bottom of the search window plus the surface-relative limit:
        a1_z=20 + measurement_height(-1) - max_travel(5) + limit(-2) = 12."""
        gantry = _make_gantry(safe_z=80.0)
        deck = _make_deck(plate=_make_plate(rows=1, columns=1, a1_z=20.0))
        protocol = _make_protocol(
            "measure",
            instrument="probe",
            position="plate.A1",
            measurement_height=-1.0,
            indentation_limit_height=-2.0,
            method_kwargs={
                "detect_surface": True,
                "surface_search_max_travel": 5.0,
            },
        )

        targets = collect_protocol_motion_targets(gantry, protocol, deck)

        assert targets[-1].position_id == "A1.indentation_limit_z"
        assert targets[-1].z == 12.0


# ── Signed (home_origin) working-volume bounds ──────────────────────────


class TestSignedWorkingVolumeBounds:
    """Bounds checks for a home_origin gantry (non-positive working volume)."""

    def test_targets_on_every_negative_axis_are_accepted(self):
        gantry = _make_signed_gantry()
        deck = _make_deck(vial=_make_vial(x=-30.0, y=-40.0, z=-20.0))
        instrumented_gantry = _make_instrumented_gantry(("probe", _make_instrument()))
        protocol = _make_protocol(
            "measure", instrument="probe", position="vial", measurement_height=0.0,
        )

        assert validate_protocol_motion_bounds(gantry, protocol, deck, instrumented_gantry) == []

    def test_move_target_on_every_negative_axis_is_accepted(self):
        gantry = _make_signed_gantry()
        deck = _make_deck()
        instrumented_gantry = _make_instrumented_gantry(("probe", _make_instrument()))
        protocol = Protocol(
            [
                ProtocolStep(
                    index=0,
                    command_name="move",
                    handler=MagicMock(),
                    args={
                        "instrument": "probe",
                        "position": [-30.0, -40.0, -20.0],
                        "travel_z": -5.0,
                    },
                ),
            ],
        )

        assert validate_protocol_motion_bounds(gantry, protocol, deck, instrumented_gantry) == []

    def test_target_above_zero_is_rejected(self):
        gantry = _make_signed_gantry()
        deck = _make_deck(vial=_make_vial(x=10.0, y=-40.0, z=-20.0))
        instrumented_gantry = _make_instrumented_gantry(("probe", _make_instrument()))
        protocol = _make_protocol(
            "measure", instrument="probe", position="vial", measurement_height=0.0,
        )

        violations = validate_protocol_motion_bounds(gantry, protocol, deck, instrumented_gantry)

        assert violations
        assert any(v.axis == "x" and v.bound_name == "x_max" for v in violations)

    def test_target_below_configured_minimum_is_rejected(self):
        gantry = _make_signed_gantry()
        deck = _make_deck(vial=_make_vial(x=-30.0, y=-40.0, z=-150.0))
        instrumented_gantry = _make_instrumented_gantry(("probe", _make_instrument()))
        protocol = _make_protocol(
            "measure", instrument="probe", position="vial", measurement_height=0.0,
        )

        violations = validate_protocol_motion_bounds(gantry, protocol, deck, instrumented_gantry)

        assert violations
        assert any(v.axis == "z" and v.bound_name == "z_min" for v in violations)


class TestSignedTipRackPickupBounds:
    """pickup_z targets must be bounds-checked in both frame policies."""

    def _make_tip_rack(self, *, pickup_z: float) -> "TipRack":
        from cubos.deck.labware.tip_rack import TipRack

        return TipRack(
            name="tips",
            model_name="tip_rack",
            rows=1,
            columns=1,
            pickup_z=pickup_z,
            tip_length=10.0,
            tips={"A1": Coordinate3D(x=-30.0, y=-40.0, z=pickup_z)},
        )

    def test_signed_pickup_z_within_signed_volume_is_accepted(self):
        gantry = _make_signed_gantry()
        deck = _make_deck(tips=self._make_tip_rack(pickup_z=-20.0))
        instrumented_gantry = _make_instrumented_gantry(("pipette", _make_instrument()))
        protocol = _make_protocol("pick_up_tip", instrument="pipette", position="tips.A1")

        assert validate_protocol_motion_bounds(gantry, protocol, deck, instrumented_gantry) == []

    def test_out_of_volume_pickup_z_rejected_for_positive_config(self):
        gantry = _make_gantry(z_min=0.0, z_max=80.0, safe_z=80.0)
        deck = _make_deck(tips=self._make_tip_rack(pickup_z=150.0))
        instrumented_gantry = _make_instrumented_gantry(("pipette", _make_instrument()))
        protocol = _make_protocol("pick_up_tip", instrument="pipette", position="tips.A1")

        violations = validate_protocol_motion_bounds(gantry, protocol, deck, instrumented_gantry)

        assert violations
        assert any(v.axis == "z" for v in violations)

    def test_out_of_volume_pickup_z_rejected_for_signed_config(self):
        gantry = _make_signed_gantry()
        deck = _make_deck(tips=self._make_tip_rack(pickup_z=-150.0))
        instrumented_gantry = _make_instrumented_gantry(("pipette", _make_instrument()))
        protocol = _make_protocol("pick_up_tip", instrument="pipette", position="tips.A1")

        violations = validate_protocol_motion_bounds(gantry, protocol, deck, instrumented_gantry)

        assert violations
        assert any(v.axis == "z" and v.bound_name == "z_min" for v in violations)
