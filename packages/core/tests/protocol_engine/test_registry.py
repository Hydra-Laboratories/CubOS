"""Tests for the @protocol_command decorator and CommandRegistry."""

import importlib
import inspect
from typing import get_args, get_origin

import pytest
from pydantic import ValidationError

from cubos.protocol_engine.registry import (
    CommandRegistry,
    _build_schema_from_signature,
    protocol_command,
)


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Reset the singleton before and after each test."""
    CommandRegistry.reset()
    yield
    CommandRegistry.reset()


# ─── Registration ────────────────────────────────────────────────────────────


def test_duplicate_command_name_raises():
    @protocol_command("dup")
    def first(context) -> None:
        pass

    with pytest.raises(ValueError, match="already registered"):

        @protocol_command("dup")
        def second(context) -> None:
            pass


def test_get_unknown_command_raises():
    reg = CommandRegistry.instance()
    with pytest.raises(KeyError, match="Unknown protocol command 'nope'"):
        reg.get("nope")


def test_get_unknown_command_lists_available():
    @protocol_command("alpha")
    def alpha(context) -> None:
        pass

    @protocol_command("beta")
    def beta(context) -> None:
        pass

    reg = CommandRegistry.instance()
    with pytest.raises(KeyError, match="alpha.*beta"):
        reg.get("nope")


# ─── Schema generation ───────────────────────────────────────────────────────


def test_schema_forbids_extra_fields():
    def cmd(context, a: str) -> None:
        pass

    schema = _build_schema_from_signature("cmd", cmd)
    with pytest.raises(ValidationError, match="Extra inputs"):
        schema.model_validate({"a": "ok", "b": "extra"})


def test_schema_requires_all_parameters():
    def cmd(context, a: str, b: int) -> None:
        pass

    schema = _build_schema_from_signature("cmd", cmd)
    with pytest.raises(ValidationError):
        schema.model_validate({"a": "only_a"})


def test_schema_resolves_postponed_typing_annotations():
    from cubos.protocol_engine.commands.pipette import serial_transfer

    schema = _build_schema_from_signature("serial_transfer", serial_transfer)
    result = schema.model_validate({
        "source": "vial_1",
        "plate": "plate_1",
        "axis": "A",
        "volumes": [1.0, 2.0],
    })

    assert result.volumes == [1.0, 2.0]


def _reload_real_command_registry() -> CommandRegistry:
    modules = [
        "cubos.protocol_engine.commands.home",
        "cubos.protocol_engine.commands.measure",
        "cubos.protocol_engine.commands.move",
        "cubos.protocol_engine.commands.pause",
        "cubos.protocol_engine.commands.pipette",
        "cubos.protocol_engine.commands.scan",
    ]
    CommandRegistry.reset()
    for module_name in modules:
        importlib.reload(importlib.import_module(module_name))
    return CommandRegistry.instance()


def _sample_value(annotation):
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is list:
        return [_sample_value(args[0] if args else str)]
    if origin is dict:
        return {}
    if origin is not None and type(None) in args:
        non_none = next(arg for arg in args if arg is not type(None))
        return _sample_value(non_none)
    if annotation is str:
        return "plate_1.A1"
    if annotation is float:
        return 1.0
    if annotation is int:
        return 1
    return "value"


def test_every_registered_command_schema_validates_required_fields():
    registry = _reload_real_command_registry()

    for command_name in registry.command_names:
        registered = registry.get(command_name)
        signature = inspect.signature(registered.handler)
        args = {}
        for name, parameter in signature.parameters.items():
            if name in {"self", "context"}:
                continue
            if parameter.default is inspect.Parameter.empty:
                field = registered.schema.model_fields[name]
                args[name] = _sample_value(field.annotation)

        registered.schema.model_validate(args)
