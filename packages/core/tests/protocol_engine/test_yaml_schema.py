"""Tests for protocol YAML schema validation."""

import pytest
from pydantic import ValidationError

from cubos.protocol_engine.registry import CommandRegistry, protocol_command
from cubos.protocol_engine.yaml_schema import ProtocolYamlSchema


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Reset and populate registry with a test 'move' command."""
    CommandRegistry.reset()

    @protocol_command("move")
    def move(context, instrument: str, position: str) -> None:
        pass

    yield
    CommandRegistry.reset()


# ─── Top-level validation ───────────────────────────────────────────────────


def test_extra_top_level_key_fails():
    data = {
        "protocol": [],
        "extra_key": "bad",
    }
    with pytest.raises(ValidationError):
        ProtocolYamlSchema.model_validate(data)


def test_missing_protocol_key_fails():
    with pytest.raises(ValidationError):
        ProtocolYamlSchema.model_validate({"other": []})


# ─── Step-level validation ───────────────────────────────────────────────────


def test_unknown_command_name_fails():
    data = {
        "protocol": [
            {"unknown_cmd": {"a": "b"}},
        ]
    }
    with pytest.raises((ValidationError, KeyError)):
        ProtocolYamlSchema.model_validate(data)


def test_extra_command_args_fail():
    data = {
        "protocol": [
            {"move": {"instrument": "pipette", "position": "plate_1.A1", "extra": "bad"}},
        ]
    }
    with pytest.raises(ValidationError):
        ProtocolYamlSchema.model_validate(data)


def test_missing_required_args_fail():
    data = {
        "protocol": [
            {"move": {"instrument": "pipette"}},
        ]
    }
    with pytest.raises(ValidationError):
        ProtocolYamlSchema.model_validate(data)


def test_multi_command_per_step_fails():
    data = {
        "protocol": [
            {"move": {"instrument": "pipette", "position": "p.A1"}, "other": {}},
        ]
    }
    with pytest.raises(ValidationError, match="exactly one command"):
        ProtocolYamlSchema.model_validate(data)


def test_non_dict_args_fail():
    data = {
        "protocol": [
            {"move": "plate_1.A1"},
        ]
    }
    with pytest.raises(ValidationError, match="must be a mapping"):
        ProtocolYamlSchema.model_validate(data)


def test_command_with_null_args_normalised_to_empty_dict():
    """Register a no-arg command and verify null args parse as empty dict."""
    @protocol_command("home")
    def home(context) -> None:
        pass

    data = {
        "protocol": [
            {"home": None},
        ]
    }
    schema = ProtocolYamlSchema.model_validate(data)
    assert schema.protocol[0].command == "home"
    assert schema.protocol[0].args == {}
