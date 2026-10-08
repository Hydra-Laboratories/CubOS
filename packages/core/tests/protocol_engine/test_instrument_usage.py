"""Tests for required_instrument_names: which instruments a protocol needs."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from cubos.gantry.instrument_mount import InstrumentedGantry
from cubos.instruments.lighting.interface import LightingInstrument
from cubos.protocol_engine.instrument_usage import (
    NO_INSTRUMENT_COMMANDS,
    PIPETTE_COMMANDS,
    required_instrument_names,
)
from cubos.protocol_engine.protocol import Protocol
from cubos.protocol_engine.registry import CommandRegistry
from cubos.protocol_engine.runtime import ProtocolStep


def _step(index: int, command_name: str, **args) -> ProtocolStep:
    return ProtocolStep(
        index=index, command_name=command_name, handler=lambda **_: None, args=args,
    )


def _gantry(instruments: dict | None = None) -> InstrumentedGantry:
    return InstrumentedGantry(controller=Mock(), instruments=instruments or {})


class TestExplicitInstrumentField:

    @pytest.mark.parametrize(
        "command_name",
        ["move", "measure", "scan", "capture", "decap", "cap", "cure", "rinse", "set_lights"],
    )
    def test_reads_instrument_arg(self, command_name):
        protocol = Protocol([_step(0, command_name, instrument="asmi")])
        assert required_instrument_names(protocol, _gantry()) == {"asmi"}

    def test_multiple_steps_union_distinct_instruments(self):
        protocol = Protocol([
            _step(0, "move", instrument="asmi"),
            _step(1, "measure", instrument="asmi"),
            _step(2, "move", instrument="pipette"),
        ])
        assert required_instrument_names(protocol, _gantry()) == {"asmi", "pipette"}


class TestNoInstrumentCommands:

    @pytest.mark.parametrize("command_name", sorted(NO_INSTRUMENT_COMMANDS))
    def test_contributes_nothing(self, command_name):
        protocol = Protocol([_step(0, command_name)])
        assert required_instrument_names(protocol, _gantry()) == set()


class TestPipetteFamily:

    @pytest.mark.parametrize("command_name", sorted(PIPETTE_COMMANDS))
    def test_resolves_to_fixed_pipette_key(self, command_name):
        protocol = Protocol([_step(0, command_name)])
        assert required_instrument_names(protocol, _gantry()) == {"pipette"}


class TestImageWell:

    def test_explicit_lights_adds_camera_and_lights(self):
        protocol = Protocol([
            _step(0, "image_well", camera="cam", well="plate.A1", image_height=1.0, lights="ring"),
        ])
        assert required_instrument_names(protocol, _gantry()) == {"cam", "ring"}

    def test_lights_none_adds_only_camera(self):
        protocol = Protocol([
            _step(0, "image_well", camera="cam", well="plate.A1", image_height=1.0, lights="none"),
        ])
        assert required_instrument_names(protocol, _gantry()) == {"cam"}

    def test_omitted_lights_resolves_sole_lighting_instrument(self):
        lighting = Mock(spec=LightingInstrument)
        gantry = _gantry({"lights": lighting, "cam": Mock()})
        protocol = Protocol([
            _step(0, "image_well", camera="cam", well="plate.A1", image_height=1.0),
        ])
        assert required_instrument_names(protocol, gantry) == {"cam", "lights"}

    def test_omitted_lights_with_no_lighting_instrument_adds_only_camera(self):
        gantry = _gantry({"cam": Mock()})
        protocol = Protocol([
            _step(0, "image_well", camera="cam", well="plate.A1", image_height=1.0),
        ])
        assert required_instrument_names(protocol, gantry) == {"cam"}

    def test_omitted_lights_with_ambiguous_lighting_adds_only_camera(self):
        # _resolve_lighting raises at execution time when there's more than
        # one lighting instrument and `lights` wasn't given; nothing needs
        # to be connected on this step's behalf for a step that will abort.
        gantry = _gantry({
            "lights_a": Mock(spec=LightingInstrument),
            "lights_b": Mock(spec=LightingInstrument),
            "cam": Mock(),
        })
        protocol = Protocol([
            _step(0, "image_well", camera="cam", well="plate.A1", image_height=1.0),
        ])
        assert required_instrument_names(protocol, gantry) == {"cam"}


class TestEmptyProtocol:

    def test_no_steps_needs_nothing(self):
        assert required_instrument_names(Protocol([]), _gantry()) == set()

    def test_protocol_without_steps_attribute_needs_nothing(self):
        assert required_instrument_names(object(), _gantry()) == set()


class TestEveryRegisteredCommandIsClassified:
    """Guards against a new command silently falling through unaccounted.

    A command not in NO_INSTRUMENT_COMMANDS/PIPETTE_COMMANDS, and not
    "image_well", must declare an `instrument` field on its schema --
    otherwise `required_instrument_names` would connect nothing for it and
    a protocol run could reach an unconnected instrument mid-execution.
    """

    def test_every_command_is_classified_or_has_instrument_field(self):
        import cubos.protocol_engine.commands  # noqa: F401 -- registers commands

        registry = CommandRegistry.instance()
        unclassified = []
        for name in registry.command_names:
            if name in NO_INSTRUMENT_COMMANDS or name in PIPETTE_COMMANDS:
                continue
            if name == "image_well":
                continue
            schema_fields = registry.get(name).schema.model_fields
            if "instrument" not in schema_fields:
                unclassified.append(name)
        assert unclassified == []
