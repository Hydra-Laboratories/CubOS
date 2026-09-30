"""Tests for GantrySession.connected_gantry_config."""

from cubos.gantry.session import GantrySession


def test_none_while_disconnected():
    assert GantrySession().connected_gantry_config is None


def test_returns_deep_copy():
    session = GantrySession()
    session._connected_gantry_config = {"instruments": {"lights": {"port": "x"}}}
    copy_one = session.connected_gantry_config
    copy_one["instruments"]["lights"]["port"] = "mutated"
    assert session._connected_gantry_config["instruments"]["lights"]["port"] == "x"
