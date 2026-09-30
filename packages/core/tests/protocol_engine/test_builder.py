"""Tests for the Python protocol builder and shared compilation."""

from __future__ import annotations


import pytest
from cubos.protocol_engine.builder import ProtocolBuilder, wells
from cubos.protocol_engine.compiler import CommandCall, compile_protocol
from cubos.protocol_engine.protocol import ProtocolSetup


def _step_signature(protocol):
    return [
        (step.index, step.command_name, step.args)
        for step in protocol.steps
    ]


def test_compile_protocol_uses_registry_validation_and_omits_defaults():
    protocol = compile_protocol([
        CommandCall(
            command="pause",
            args={"seconds": 0.5},
        ),
    ])

    assert _step_signature(protocol) == [(0, "pause", {"seconds": 0.5})]


def test_compile_protocol_rejects_unknown_command_through_registry():
    with pytest.raises(KeyError, match="Unknown protocol command"):
        compile_protocol([CommandCall(command="unknown_cmd", args={})])


def test_compile_protocol_rejects_invalid_args_through_pydantic():
    with pytest.raises(ValueError, match=r"step 0 \(move\).*instrument"):
        compile_protocol([
            CommandCall(command="move", args={"position": "plate_1.A1"}),
        ])


def test_compile_protocol_extra_field_uses_loader_style_rename_hint():
    with pytest.raises(ValueError, match="indentation_limit_height") as exc_info:
        compile_protocol([
            CommandCall(
                command="scan",
                args={
                    "plate": "plate_1",
                    "instrument": "asmi",
                    "method": "indentation",
                    "measurement_height": -1.0,
                    "interwell_scan_height": 10.0,
                    "indentation_limit": 5.0,
                },
            ),
        ])

    assert "step 0 (scan)" in str(exc_info.value)
    assert "How to fix:" in str(exc_info.value)


def test_wells_returns_row_major_targets():
    assert wells("plate", rows="A:D", columns=range(1, 7)) == [
        "plate.A1",
        "plate.A2",
        "plate.A3",
        "plate.A4",
        "plate.A5",
        "plate.A6",
        "plate.B1",
        "plate.B2",
        "plate.B3",
        "plate.B4",
        "plate.B5",
        "plate.B6",
        "plate.C1",
        "plate.C2",
        "plate.C3",
        "plate.C4",
        "plate.C5",
        "plate.C6",
        "plate.D1",
        "plate.D2",
        "plate.D3",
        "plate.D4",
        "plate.D5",
        "plate.D6",
    ]


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ("AA:C", "Row ranges must look like"),
        ("D:A", "Row ranges must be ascending"),
        ("A,,C", "empty label"),
        ("", "Rows cannot be empty"),
    ],
)
def test_wells_rejects_invalid_row_specs(rows, message):
    with pytest.raises(ValueError, match=message):
        wells("plate", rows=rows, columns=[1])


def test_wells_accepts_iterable_rows():
    assert wells("plate", rows=["a", "b"], columns=[1, "2"]) == [
        "plate.A1",
        "plate.A2",
        "plate.B1",
        "plate.B2",
    ]


def test_builder_supports_registered_commands_without_typed_wrapper():
    protocol = (
        ProtocolBuilder()
        .add_command("breakpoint", message="Continue?")
        .build()
    )

    assert _step_signature(protocol) == [
        (0, "breakpoint", {"message": "Continue?"}),
    ]


def test_builder_rejects_duplicate_command_arguments():
    with pytest.raises(ValueError, match="Duplicate arguments.*instrument"):
        ProtocolBuilder().add_command(
            "move",
            {"instrument": "pipette", "position": "plate_1.A1"},
            instrument="asmi",
        )


def test_builder_compiles_serial_transfer_through_registry():
    protocol = (
        ProtocolBuilder()
        .add_command(
            "serial_transfer",
            source="vial_1",
            plate="plate_1",
            axis="A",
            volumes=[5.0, 10.0],
        )
        .build()
    )

    assert _step_signature(protocol) == [
        (
            0,
            "serial_transfer",
            {
                "source": "vial_1",
                "plate": "plate_1",
                "axis": "A",
                "volumes": [5.0, 10.0],
            },
        ),
    ]


def test_builder_omits_defaults_unless_explicitly_set():
    implicit_default = ProtocolBuilder().add_pause(1.0).build()
    explicit_default = ProtocolBuilder().add_pause(1.0, reason="").build()

    assert implicit_default.steps[0].args == {"seconds": 1.0}
    assert explicit_default.steps[0].args == {"seconds": 1.0, "reason": ""}


def test_builder_measure_wrapper_preserves_optional_arguments():
    protocol = (
        ProtocolBuilder()
        .add_measure(
            instrument="asmi",
            position="plate_1.A1",
            measurement_height=1.0,
            method="indentation",
            indentation_limit_height=-3.0,
            method_kwargs={"speed": 0.5},
        )
        .build()
    )

    assert _step_signature(protocol) == [
        (
            0,
            "measure",
            {
                "instrument": "asmi",
                "position": "plate_1.A1",
                "measurement_height": 1.0,
                "method": "indentation",
                "indentation_limit_height": -3.0,
                "method_kwargs": {"speed": 0.5},
            },
        ),
    ]


def test_builder_scan_wrapper_preserves_optional_arguments():
    protocol = (
        ProtocolBuilder()
        .add_scan(
            plate="plate_1",
            instrument="asmi",
            method="indentation",
            measurement_height=1.0,
            interwell_scan_height=5.0,
            indentation_limit_height=-2.0,
            delay_s=0.25,
            method_kwargs={"repetitions": 2},
        )
        .build()
    )

    assert _step_signature(protocol) == [
        (
            0,
            "scan",
            {
                "plate": "plate_1",
                "instrument": "asmi",
                "method": "indentation",
                "measurement_height": 1.0,
                "interwell_scan_height": 5.0,
                "indentation_limit_height": -2.0,
                "delay_s": 0.25,
                "method_kwargs": {"repetitions": 2},
            },
        ),
    ]


def test_builder_mutation_after_build_does_not_mutate_built_protocol():
    protocol_builder = ProtocolBuilder()
    protocol_builder.add_position("park", [1.0, 2.0, 3.0])
    protocol_builder.add_move(
        instrument="pipette",
        position="park",
        travel_z=10.0,
    )

    protocol = protocol_builder.build()

    protocol_builder.add_position("park", [9.0, 9.0, 9.0])
    protocol_builder.add_home()

    assert protocol.positions == {"park": [1.0, 2.0, 3.0]}
    assert _step_signature(protocol) == [
        (
            0,
            "move",
            {
                "instrument": "pipette",
                "position": "park",
                "travel_z": 10.0,
            },
        ),
    ]


def test_builder_add_positions_deep_copies_and_validates_positions():
    positions = {"park": [1, 2, 3]}
    builder = ProtocolBuilder().add_positions(positions)
    positions["park"][0] = 99

    protocol = builder.add_move(instrument="pipette", position="park").build()

    assert protocol.positions == {"park": [1.0, 2.0, 3.0]}


def test_with_setup_attaches_setup_metadata():
    protocol = (
        ProtocolBuilder.with_setup(
            gantry_path="packages/core/configs/gantry/cub_xl_asmi.yaml",
            deck_path="packages/core/configs/deck/asmi_deck.yaml",
        )
        .add_home()
        .build()
    )

    assert protocol.setup == ProtocolSetup(
        gantry_path="packages/core/configs/gantry/cub_xl_asmi.yaml",
        deck_path="packages/core/configs/deck/asmi_deck.yaml",
    )


def test_builder_without_setup_has_no_setup_metadata():
    protocol = ProtocolBuilder().add_home().build()
    assert protocol.setup is None


def test_builder_rejects_two_element_named_position_cleanly():
    with pytest.raises(ValueError, match="exactly three finite XYZ"):
        (
            ProtocolBuilder()
            .add_position("bad", [1.0, 2.0])
            .add_move(instrument="pipette", position="bad")
            .build()
        )


def test_builder_rejects_nan_named_position_cleanly():
    with pytest.raises(ValueError, match="finite float"):
        (
            ProtocolBuilder()
            .add_position("bad", [1.0, float("nan"), 3.0])
            .add_move(instrument="pipette", position="bad")
            .build()
        )
