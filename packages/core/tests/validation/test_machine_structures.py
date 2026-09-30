"""Machine-structure collision validation for protocol motion."""

from __future__ import annotations

from unittest.mock import MagicMock

from cubos.gantry.instrument_mount import InstrumentedGantry
from cubos.deck.deck import Deck
from cubos.deck.labware.labware import Coordinate3D
from cubos.deck.labware.well_plate import WellPlate
from cubos.gantry.gantry_config import (
    GantryConfig,
    GantryType,
    WorkingVolume,
)
from cubos.protocol_engine.protocol import Protocol, ProtocolStep
from cubos.validation.protocol_semantics import validate_protocol_semantics


def _gantry(
    *,
    gantry_type: GantryType = GantryType.CUB_XL,
    x_max: float = 600.0,
    z_max: float = 160.0,
    safe_z: float | None = None,
) -> GantryConfig:
    return GantryConfig(
        serial_port="/dev/ttyUSB0",
        gantry_type=gantry_type,
        factory_z_travel_mm=z_max,
        working_volume=WorkingVolume(
            x_min=0.0,
            x_max=x_max,
            y_min=0.0,
            y_max=300.0,
            z_min=0.0,
            z_max=z_max,
        ),
        safe_z=safe_z,
    )


def _instrument(*, name: str = "asmi", offset_x: float = 0.0):
    instr = MagicMock()
    instr.name = name
    instr.offset_x = offset_x
    instr.offset_y = 0.0
    instr.depth = 0.0
    return instr


def _board() -> InstrumentedGantry:
    return InstrumentedGantry(
        controller=MagicMock(),
        instruments={"asmi": _instrument()},
    )


def _deck(*, well_z: float = 40.0) -> Deck:
    return Deck({
        "plate": WellPlate(
            name="plate",
            model_name="test_plate",
            length=127.71,
            width=85.43,
            height=14.10,
            rows=1,
            columns=1,
            wells={"A1": Coordinate3D(x=500.0, y=150.0, z=well_z)},
            capacity_ul=200.0,
            working_volume_ul=150.0,
        )
    })


def _protocol(*steps: ProtocolStep) -> Protocol:
    return Protocol(list(steps))


def _move_step(
    index: int,
    *,
    position,
    instrument: str = "asmi",
    travel_z: float | None = None,
) -> ProtocolStep:
    args = {"instrument": instrument, "position": position}
    if travel_z is not None:
        args["travel_z"] = travel_z
    return ProtocolStep(
        index=index,
        command_name="move",
        handler=lambda *a, **k: None,
        args=args,
    )


def _home_step(index: int) -> ProtocolStep:
    return ProtocolStep(
        index=index,
        command_name="home",
        handler=lambda *a, **k: None,
        args={},
    )


def test_cub_xl_low_pose_inside_right_rail_fails():
    protocol = _protocol(_move_step(0, position=(500.0, 150.0, 50.0)))

    violations = validate_protocol_semantics(
        protocol, _board(), _deck(), _gantry(),
    )

    assert any("Cub XL right X-max rail" in v.message for v in violations), violations


def test_cub_xl_high_pose_inside_right_rail_xy_passes():
    protocol = _protocol(_move_step(0, position=(500.0, 150.0, 81.0)))

    assert validate_protocol_semantics(
        protocol, _board(), _deck(), _gantry(),
    ) == []


def test_cub_type_does_not_apply_cub_xl_right_rail():
    protocol = _protocol(_move_step(0, position=(500.0, 150.0, 50.0)))

    assert validate_protocol_semantics(
        protocol,
        _board(),
        _deck(),
        _gantry(gantry_type=GantryType.CUB),
    ) == []


def test_cub_xl_travel_segment_crossing_right_rail_at_low_z_fails():
    protocol = _protocol(
        _move_step(0, position=(400.0, 150.0, 120.0)),
        _move_step(1, position=(560.0, 150.0, 120.0), travel_z=50.0),
    )

    violations = validate_protocol_semantics(
        protocol, _board(), _deck(), _gantry(),
    )

    assert any(
        "travel segment" in v.message and "Cub XL right X-max rail" in v.message
        for v in violations
    ), violations


def test_home_over_rail_passes_but_lowering_while_over_rail_fails():
    gantry = _gantry(x_max=400.0, z_max=130.0)
    instrumented_gantry = InstrumentedGantry(
        controller=MagicMock(),
        instruments={
            "asmi": _instrument(name="asmi"),
            "pipette": _instrument(name="pipette", offset_x=100.0),
        },
    )
    protocol = _protocol(
        _home_step(0),
        _move_step(
            1,
            instrument="pipette",
            position=(460.0, 150.0, 120.0),
            travel_z=80.0,
        ),
    )

    violations = validate_protocol_semantics(protocol, instrumented_gantry, _deck(), gantry)

    messages = [violation.message for violation in violations]
    assert not any("home pose" in message for message in messages), messages
    assert any("travel_z lift/lower" in message for message in messages), messages
    assert any("Cub XL right X-max rail" in message for message in messages), messages


def test_home_over_rail_at_rail_height_fails():
    gantry = _gantry(x_max=400.0, z_max=80.0)
    instrumented_gantry = InstrumentedGantry(
        controller=MagicMock(),
        instruments={"pipette": _instrument(name="pipette", offset_x=100.0)},
    )
    protocol = _protocol(_home_step(0))

    violations = validate_protocol_semantics(protocol, instrumented_gantry, _deck(), gantry)

    assert any("home pose" in v.message for v in violations), violations
    assert any("Cub XL right X-max rail" in v.message for v in violations), violations


def test_deck_target_move_validates_safe_z_pose():
    protocol = _protocol(_move_step(0, position="plate.A1"))

    violations = validate_protocol_semantics(
        protocol,
        _board(),
        _deck(),
        _gantry(safe_z=50.0),
    )

    assert any("safe_z" in v.message for v in violations), violations


def test_scan_action_z_inside_machine_structure_fails():
    protocol = Protocol([
        ProtocolStep(
            index=0,
            command_name="scan",
            handler=lambda *a, **k: None,
            args={
                "plate": "plate",
                "instrument": "asmi",
                "method": "indentation",
                "measurement_height": 10.0,
                "interwell_scan_height": 80.0,
                "indentation_limit_height": -20.0,
                "method_kwargs": {"step_size": 0.1},
            },
        )
    ])

    violations = validate_protocol_semantics(
        protocol, _board(), _deck(), _gantry(),
    )

    assert any("action_z" in v.message for v in violations), violations
