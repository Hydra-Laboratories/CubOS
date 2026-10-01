"""Tests for Protocol, ProtocolStep, and ProtocolContext runtime classes."""

import logging
from unittest.mock import MagicMock

import pytest

import cubos.protocol_engine.setup as protocol_setup
from cubos.protocol_engine.protocol import Protocol, ProtocolSetup
from cubos.protocol_engine.runtime import ProtocolContext, ProtocolStep


def _mock_context():
    return ProtocolContext(
        gantry=MagicMock(),
        deck=MagicMock(),
        logger=logging.getLogger("test_protocol"),
    )


# ─── ProtocolStep ────────────────────────────────────────────────────────────


class TestProtocolStep:


    def test_execute_scopes_fluid_operation_key_to_campaign_and_step(self):
        observed = []

        def handler(context):
            observed.append(context.fluid_operation_key("transfer"))

        step = ProtocolStep(index=3, command_name="transfer", handler=handler, args={})
        ctx = _mock_context()
        ctx.campaign_id = 17

        step.execute(ctx)

        assert observed == ["campaign:17:step:3:transfer:transfer"]
        assert ctx.active_step_index is None
        assert ctx.active_step_command is None

    def test_execute_restores_operation_scope_after_handler_failure(self):
        def handler(_context):
            raise RuntimeError("boom")

        step = ProtocolStep(index=4, command_name="transfer", handler=handler, args={})
        ctx = _mock_context()
        ctx.active_step_index = 99
        ctx.active_step_command = "outer"
        ctx.active_substep = "A1"

        with pytest.raises(RuntimeError, match="boom"):
            step.execute(ctx)

        assert ctx.active_step_index == 99
        assert ctx.active_step_command == "outer"
        assert ctx.active_substep == "A1"

    def test_manual_fluid_operation_keys_are_unique(self):
        ctx = _mock_context()
        ctx.campaign_id = 8

        assert ctx.fluid_operation_key("transfer") == (
            "campaign:8:manual:1:transfer"
        )
        assert ctx.fluid_operation_key("transfer") == (
            "campaign:8:manual:2:transfer"
        )


# ─── Protocol ────────────────────────────────────────────────────────────────


class TestProtocol:

    def test_run_executes_all_steps_in_order(self):
        call_order = []
        handler_a = MagicMock(side_effect=lambda ctx, **kw: call_order.append("a"))
        handler_b = MagicMock(side_effect=lambda ctx, **kw: call_order.append("b"))

        steps = [
            ProtocolStep(index=0, command_name="a", handler=handler_a, args={"x": "1"}),
            ProtocolStep(index=1, command_name="b", handler=handler_b, args={"y": "2"}),
        ]
        protocol = Protocol(steps=steps)
        ctx = _mock_context()
        protocol.execute(ctx)

        assert call_order == ["a", "b"]


    def test_protocol_repr(self):
        steps = [
            ProtocolStep(index=0, command_name="move", handler=MagicMock(), args={}),
            ProtocolStep(index=1, command_name="aspirate", handler=MagicMock(), args={}),
        ]
        assert repr(Protocol(steps=steps)) == "Protocol([move, aspirate])"

    def test_protocol_steps_property_returns_copy(self):
        steps = [ProtocolStep(index=0, command_name="a", handler=MagicMock(), args={})]
        protocol = Protocol(steps=steps)
        returned = protocol.steps
        returned.append(ProtocolStep(index=1, command_name="b", handler=MagicMock(), args={}))
        assert len(protocol) == 1


# ─── Protocol.validate() / Protocol.run() ────────────────────────────────────


class TestProtocolValidate:

    def test_validate_without_setup_raises(self):
        protocol = Protocol(steps=[])
        with pytest.raises(ValueError, match="no setup metadata"):
            protocol.validate()

    def test_validate_with_setup_runs_offline_setup_path(self, monkeypatch):
        calls = {}

        def fake_setup_protocol(gantry_path, deck_path, protocol, **kwargs):
            calls["gantry_path"] = gantry_path
            calls["deck_path"] = deck_path
            calls["protocol"] = protocol
            calls["kwargs"] = kwargs
            return (protocol, MagicMock())

        monkeypatch.setattr(protocol_setup, "setup_protocol", fake_setup_protocol)

        protocol = Protocol(
            steps=[],
            setup=ProtocolSetup(gantry_path="g.yaml", deck_path="d.yaml"),
        )
        protocol.validate()

        assert calls["gantry_path"] == "g.yaml"
        assert calls["deck_path"] == "d.yaml"
        assert calls["protocol"] is protocol
        # Offline: validation must request mock_mode and never touch hardware.
        assert calls["kwargs"].get("mock_mode") is True


class TestProtocolRun:

    def test_run_without_setup_raises(self):
        protocol = Protocol(steps=[])
        with pytest.raises(ValueError, match="setup metadata"):
            protocol.run()

    def test_run_with_setup_delegates_to_run_on_hardware(self, monkeypatch):
        calls = {}

        def fake_run_on_hardware(gantry_path, deck_path, protocol, **kwargs):
            calls["args"] = (gantry_path, deck_path, protocol)
            calls["kwargs"] = kwargs
            return ["ok"]

        monkeypatch.setattr(protocol_setup, "run_on_hardware", fake_run_on_hardware)

        protocol = Protocol(
            steps=[],
            setup=ProtocolSetup(gantry_path="g.yaml", deck_path="d.yaml"),
        )
        results = protocol.run()

        assert results == ["ok"]
        assert calls["args"] == ("g.yaml", "d.yaml", protocol)
        assert calls["kwargs"] == {
            "data_store": None,
            "campaign_description": None,
            "protocol_config": None,
            "fluid_state_id": None,
            "initial_fluids": None,
        }


    def test_run_forwards_resumable_fluid_state_inputs(self, monkeypatch):
        passed = {}

        def fake_run_on_hardware(gantry_path, deck_path, protocol, **kwargs):
            passed.update(kwargs)
            return []

        monkeypatch.setattr(protocol_setup, "run_on_hardware", fake_run_on_hardware)
        protocol = Protocol(
            steps=[],
            setup=ProtocolSetup(gantry_path="g.yaml", deck_path="d.yaml"),
        )

        protocol.run(
            fluid_state_id=42,
            initial_fluids={"fluids": {}},
        )

        assert passed["fluid_state_id"] == 42
        assert passed["initial_fluids"] == {"fluids": {}}
