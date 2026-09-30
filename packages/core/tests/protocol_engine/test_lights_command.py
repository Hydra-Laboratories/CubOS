"""Tests for the set_lights protocol command and the run-end lights-off."""

from __future__ import annotations

import pytest

from cubos.gantry.instrument_mount import InstrumentedGantry
from cubos.instruments.camera.vendors.mount_only import MountOnlyCamera
from cubos.instruments.lighting.vendors.pawduino import PawduinoLighting
from cubos.protocol_engine.commands.lights import set_lights
from cubos.protocol_engine.errors import ProtocolExecutionError
from cubos.protocol_engine.runtime import ProtocolContext


class FakeGantry:
    def __init__(self, instruments):
        self.instruments = instruments
        self.safe_z = 100.0


def _context(instruments):
    return ProtocolContext(gantry=FakeGantry(instruments), deck=None)


def _lights():
    lights = PawduinoLighting(offline=True)
    lights.connect()
    return lights


class TestSetLights:
    def test_sets_channel(self):
        lights = _lights()
        set_lights(_context({"lights": lights}), instrument="lights",
                   channel="white", brightness=25)
        assert lights.status().channels["white"] == 25

    def test_all_off(self):
        lights = _lights()
        lights.set_channel("contact", 50)
        set_lights(_context({"lights": lights}), instrument="lights",
                   all_off=True)
        assert lights.status().channels == {"white": 0, "contact": 0}

    def test_zero_brightness_turns_channel_off(self):
        lights = _lights()
        lights.set_channel("white", 25)
        set_lights(_context({"lights": lights}), instrument="lights",
                   channel="white", brightness=0)
        assert lights.status().channels["white"] == 0

    def test_all_off_excludes_channel_args(self):
        with pytest.raises(ProtocolExecutionError, match="cannot be combined"):
            set_lights(_context({"lights": _lights()}), instrument="lights",
                       channel="white", brightness=5, all_off=True)

    def test_requires_channel_and_brightness(self):
        with pytest.raises(ProtocolExecutionError, match="all_off"):
            set_lights(_context({"lights": _lights()}), instrument="lights",
                       channel="white")

    def test_unknown_instrument(self):
        with pytest.raises(ProtocolExecutionError, match="No instrument"):
            set_lights(_context({}), instrument="lights", all_off=True)

    def test_wrong_instrument_type(self):
        camera = MountOnlyCamera(offline=True)
        with pytest.raises(ProtocolExecutionError, match="not a\nLightingInstrument".replace("\n", " ")):
            set_lights(_context({"cam": camera}), instrument="cam", all_off=True)

    def test_unsupported_level_wrapped(self):
        with pytest.raises(ProtocolExecutionError, match="does not support"):
            set_lights(_context({"lights": _lights()}), instrument="lights",
                       channel="white", brightness=42)


class TestRunEndLightsOff:
    def test_disconnect_instruments_turns_lights_off(self):
        lights = _lights()
        lights.set_channel("contact", 50)
        gantry = InstrumentedGantry(
            controller=None,
            instruments={"lights": lights, "cam": MountOnlyCamera(offline=True)},
        )
        gantry.disconnect_instruments()
        assert lights.status().channels == {"white": 0, "contact": 0}


class TestSetLightsErrorWrap:
    def test_all_off_failure_wrapped(self):
        from cubos.instruments.lighting.exceptions import LightingCommandError

        lights = _lights()

        def failing_all_off():
            raise LightingCommandError("board gone")

        lights.all_off = failing_all_off
        with pytest.raises(ProtocolExecutionError, match="board gone"):
            set_lights(_context({"lights": lights}), instrument="lights",
                       all_off=True)


class TestDisconnectLightsOffFailure:
    def test_all_off_failure_logged_and_disconnect_proceeds(self):
        lights = _lights()
        disconnected = []

        def failing_all_off():
            raise RuntimeError("dead board")

        lights.all_off = failing_all_off
        original_disconnect = lights.disconnect
        lights.disconnect = lambda: disconnected.append(True) or original_disconnect()
        gantry = InstrumentedGantry(controller=None, instruments={"lights": lights})
        gantry.disconnect_instruments()
        assert disconnected == [True]
