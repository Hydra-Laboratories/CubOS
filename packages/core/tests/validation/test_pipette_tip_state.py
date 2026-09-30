"""Pipette tip-state validation and attached-tip bounds coverage."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cubos.gantry.instrument_mount import InstrumentedGantry
from cubos.deck.deck import Deck
from cubos.deck.labware.labware import Coordinate3D
from cubos.deck.labware.tip_rack import DEFAULT_TIP_LENGTH_MM, TipRack
from cubos.deck.labware.vial import Vial
from cubos.deck.labware.well_plate import WellPlate
from cubos.gantry.gantry_config import (
    GantryConfig,
    GantryType,
    WorkingVolume,
)
from cubos.protocol_engine.protocol import Protocol, ProtocolStep
from cubos.validation.bounds import validate_protocol_motion_bounds
from cubos.validation.protocol_semantics import validate_protocol_semantics


DEFAULT_TEST_TIP_LENGTH = DEFAULT_TIP_LENGTH_MM


def _gantry(
    *,
    z_max: float = 160.0,
    safe_z: float = 85.0,
    x_max: float = 300.0,
    y_max: float = 260.0,
) -> GantryConfig:
    return GantryConfig(
        serial_port="/dev/ttyUSB0",
        gantry_type=GantryType.CUB_XL,
        factory_z_travel_mm=z_max,
        working_volume=WorkingVolume(
            x_min=0.0,
            x_max=x_max,
            y_min=0.0,
            y_max=y_max,
            z_min=0.0,
            z_max=z_max,
        ),
        safe_z=safe_z,
    )


def _pipette(*, depth: float = 5.0, offset_x: float = 0.0, offset_y: float = 0.0):
    pipette = MagicMock()
    pipette.name = "pipette"
    pipette.offset_x = offset_x
    pipette.offset_y = offset_y
    pipette.depth = depth
    return pipette


def _board(*, depth: float = 5.0, offset_x: float = 0.0, offset_y: float = 0.0) -> InstrumentedGantry:
    return InstrumentedGantry(
        controller=MagicMock(),
        instruments={
            "pipette": _pipette(depth=depth, offset_x=offset_x, offset_y=offset_y),
        },
    )


def _tip_rack(
    *,
    tip_length: float = DEFAULT_TEST_TIP_LENGTH,
    a1_present: bool = True,
    tip_x: float = 50.0,
    tip_y: float = 30.0,
) -> TipRack:
    return TipRack(
        name="tips",
        model_name="test_tip_rack",
        rows=1,
        columns=2,
        pickup_z=42.0,
        drop_z=32.0,
        tip_length=tip_length,
        location=Coordinate3D(x=tip_x, y=tip_y, z=42.0),
        length=12.0,
        width=8.0,
        height=20.0,
        tips={
            "A1": Coordinate3D(x=tip_x, y=tip_y, z=42.0),
            "A2": Coordinate3D(x=tip_x + 8.0, y=tip_y, z=42.0),
        },
        tip_present={"A1": a1_present, "A2": True},
    )


def _plate() -> WellPlate:
    return WellPlate(
        name="plate",
        model_name="test_plate",
        length=20.0,
        width=10.0,
        height=14.0,
        rows=1,
        columns=2,
        wells={
            "A1": Coordinate3D(x=90.0, y=30.0, z=12.0),
            "A2": Coordinate3D(x=98.0, y=30.0, z=12.0),
        },
        capacity_ul=200.0,
        working_volume_ul=150.0,
    )


def _waste() -> Vial:
    return Vial(
        name="waste",
        model_name="test_waste",
        height=40.0,
        diameter=20.0,
        location=Coordinate3D(x=120.0, y=30.0, z=35.0),
        capacity_ul=1000.0,
        working_volume_ul=900.0,
    )


def _deck(
    *,
    tip_length: float = DEFAULT_TEST_TIP_LENGTH,
    a1_present: bool = True,
    tip_x: float = 50.0,
    tip_y: float = 30.0,
) -> Deck:
    return Deck({
        "tips": _tip_rack(
            tip_length=tip_length,
            a1_present=a1_present,
            tip_x=tip_x,
            tip_y=tip_y,
        ),
        "plate": _plate(),
        "waste": _waste(),
    })


def _step(index: int, command: str, **args) -> ProtocolStep:
    return ProtocolStep(
        index=index,
        command_name=command,
        handler=lambda *a, **k: None,
        args=args,
    )


def _protocol(*steps: ProtocolStep) -> Protocol:
    return Protocol(list(steps))


def test_transfer_without_prior_tip_pickup_violates():
    protocol = _protocol(
        _step(
            0,
            "transfer",
            source="plate.A1",
            destination="plate.A2",
            volume_ul=25.0,
        ),
    )

    violations = validate_protocol_semantics(
        protocol, _board(), _deck(), _gantry(),
    )

    assert any("requires an attached pipette tip" in v.message for v in violations)


def test_tip_rack_defaults_to_opentrons_300ul_tip_length():
    rack = _tip_rack()

    assert rack.tip_length == DEFAULT_TEST_TIP_LENGTH


def test_valid_tip_pickup_transfer_and_drop_sequence_passes():
    protocol = _protocol(
        _step(0, "pick_up_tip", position="tips.A1"),
        _step(
            1,
            "transfer",
            source="plate.A1",
            destination="plate.A2",
            volume_ul=25.0,
        ),
        _step(2, "drop_tip", position="waste"),
    )

    assert validate_protocol_semantics(
        protocol, _board(), _deck(), _gantry(),
    ) == []
    assert validate_protocol_motion_bounds(
        _gantry(), protocol, _deck(), _board(),
    ) == []


def test_pickup_from_used_tip_slot_violates():
    protocol = _protocol(_step(0, "pick_up_tip", position="tips.A1"))

    violations = validate_protocol_semantics(
        protocol, _board(), _deck(a1_present=False), _gantry(),
    )

    assert any("tips.A1" in v.message and "not available" in v.message for v in violations)


def test_tip_rack_rejects_zero_tip_length_at_construction():
    """A collision-critical dimension must not silently accept 0; the
    pydantic field enforces ``tip_length > 0`` so misconfigured decks
    fail at load time, not at pickup."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        _tip_rack(tip_length=0.0)


def test_second_pickup_without_drop_violates():
    protocol = _protocol(
        _step(0, "pick_up_tip", position="tips.A1"),
        _step(1, "pick_up_tip", position="tips.A2"),
    )

    violations = validate_protocol_semantics(
        protocol, _board(), _deck(), _gantry(),
    )

    assert any("already has an attached pipette tip" in v.message for v in violations)


def test_drop_tip_without_attached_tip_violates():
    protocol = _protocol(_step(0, "drop_tip", position="waste"))

    violations = validate_protocol_semantics(
        protocol, _board(), _deck(), _gantry(),
    )

    assert any("requires an attached pipette tip" in v.message for v in violations)


def test_pickup_requires_tip_rack_target():
    protocol = _protocol(_step(0, "pick_up_tip", position="plate.A1"))

    violations = validate_protocol_semantics(
        protocol, _board(), _deck(), _gantry(),
    )

    assert any("must target a TipRack" in v.message for v in violations)


def test_pickup_requires_explicit_tip_slot():
    protocol = _protocol(_step(0, "pick_up_tip", position="tips"))

    violations = validate_protocol_semantics(
        protocol, _board(), _deck(), _gantry(),
    )

    assert any("must include an explicit tip slot" in v.message for v in violations)


def test_attached_tip_extension_is_checked_against_working_volume_by_semantics():
    protocol = _protocol(
        _step(0, "pick_up_tip", position="tips.A1"),
        _step(
            1,
            "transfer",
            source="plate.A1",
            destination="plate.A2",
            volume_ul=25.0,
        ),
    )

    # Cub XL Sterling-like Z geometry: safe_z 85 plus mounted pipette
    # depth -17 plus a 59.3 mm attached tip requires gantry z=127.3.
    violations = validate_protocol_semantics(
        protocol,
        _board(depth=-17.0),
        _deck(),
        _gantry(z_max=127.0, safe_z=85.0, x_max=293.0, y_max=264.0),
    )

    assert any("safe_z" in v.message and "outside working volume" in v.message for v in violations)


def test_attached_tip_extension_is_checked_by_protocol_motion_bounds():
    protocol = _protocol(
        _step(0, "pick_up_tip", position="tips.A1"),
        _step(
            1,
            "transfer",
            source="plate.A1",
            destination="plate.A2",
            volume_ul=25.0,
        ),
    )

    violations = validate_protocol_motion_bounds(
        _gantry(z_max=127.0, safe_z=85.0, x_max=293.0, y_max=264.0),
        protocol,
        _deck(),
        _board(depth=-17.0),
    )

    assert any(
        violation.instrument_name == "pipette"
        and violation.axis == "z"
        and violation.bound_name == "z_max"
        for violation in violations
    )


def test_home_with_attached_tip_over_right_rail_fails():
    protocol = _protocol(
        _step(0, "pick_up_tip", position="tips.A1"),
        _step(1, "home"),
    )

    violations = validate_protocol_semantics(
        protocol,
        _board(depth=0.0, offset_x=100.0),
        _deck(tip_x=140.0),
        _gantry(x_max=400.0, y_max=260.0, z_max=130.0, safe_z=85.0),
    )

    assert any(
        "home pose" in v.message
        and "Cub XL right X-max rail" in v.message
        and "(500.0, 260.0, 70.7)" in v.message
        for v in violations
    ), violations


def test_home_over_right_rail_passes_after_drop_tip_clears_extension():
    protocol = _protocol(
        _step(0, "pick_up_tip", position="tips.A1"),
        _step(1, "drop_tip", position="waste"),
        _step(2, "home"),
    )

    violations = validate_protocol_semantics(
        protocol,
        _board(depth=0.0, offset_x=100.0),
        _deck(tip_x=140.0),
        _gantry(x_max=400.0, y_max=260.0, z_max=150.0, safe_z=85.0),
    )

    assert not any("home pose" in v.message for v in violations), violations


def test_invalid_drop_target_does_not_clear_tip_before_home_validation():
    protocol = _protocol(
        _step(0, "pick_up_tip", position="tips.A1"),
        _step(1, "drop_tip", position="missing_waste"),
        _step(2, "home"),
    )

    violations = validate_protocol_semantics(
        protocol,
        _board(depth=0.0, offset_x=100.0),
        _deck(tip_x=140.0),
        _gantry(x_max=400.0, y_max=260.0, z_max=130.0, safe_z=85.0),
    )

    assert any("missing_waste" in v.message for v in violations), violations
    assert any(
        "home pose" in v.message
        and "Cub XL right X-max rail" in v.message
        and "(500.0, 260.0, 70.7)" in v.message
        for v in violations
    ), violations


def test_pipette_dispense_height_participates_in_attached_tip_bounds():
    protocol = _protocol(
        _step(0, "pick_up_tip", position="tips.A1"),
        _step(
            1,
            "transfer",
            source="plate.A1",
            destination="plate.A2",
            destination_height=20.0,
            volume_ul=25.0,
        ),
    )

    violations = validate_protocol_motion_bounds(
        _gantry(z_max=60.0, safe_z=40.0),
        protocol,
        _deck(),
        _board(depth=5.0),
    )

    assert any(
        violation.position_id == "A2.action_z"
        and violation.axis == "z"
        and violation.bound_name == "z_max"
        for violation in violations
    )


def test_non_finite_pipette_height_names_offending_field():
    protocol = _protocol(
        _step(0, "pick_up_tip", position="tips.A1"),
        _step(
            1,
            "transfer",
            source="plate.A1",
            destination="plate.A2",
            destination_height=float("nan"),
            volume_ul=25.0,
        ),
    )

    violations = validate_protocol_semantics(
        protocol, _board(), _deck(), _gantry(),
    )

    assert any("destination_height must be a finite number" in v.message for v in violations)
