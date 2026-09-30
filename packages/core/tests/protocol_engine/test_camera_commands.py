"""Tests for the capture and image_well protocol commands."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cubos.data.data_store import DataStore
from cubos.deck.labware.labware import Coordinate3D
from cubos.instruments.camera.exceptions import CameraCaptureError
from cubos.instruments.camera.vendors.flir import FlirCamera
from cubos.instruments.lighting.vendors.pawduino import PawduinoLighting
from cubos.protocol_engine.commands.camera import capture, image_well
from cubos.protocol_engine.errors import ProtocolExecutionError
from cubos.protocol_engine.runtime import ProtocolContext

SAFE_Z = 60.0
WELL = Coordinate3D(x=92.0, y=62.0, z=26.0)


class FakeGantry:
    """InstrumentedGantry double tracing motion in order."""

    def __init__(self, instruments, safe_z=SAFE_Z):
        self.instruments = instruments
        self.safe_z = safe_z
        self.trace: list = []

    def move_to_labware(self, instrument, position):
        self.trace.append(("approach", (position.x, position.y)))

    def move(self, instrument, position, travel_z=None):
        self.trace.append(("move", position, travel_z))


class FakeDeck:
    def __init__(self, coord=WELL):
        self.coord = coord

    def resolve_coordinate(self, target):
        if target.startswith("plate."):
            return self.coord
        raise KeyError(f"No labware {target!r} on deck.")

    def resolve_labware_target(self, target):
        labware_key, _, location_id = target.partition(".")
        return SimpleNamespace(
            labware_key=labware_key,
            labware_name=labware_key,
            location_id=location_id or None,
        )


@pytest.fixture(autouse=True)
def _images_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CUBOS_IMAGES_DIR", str(tmp_path / "images"))
    return tmp_path / "images"


def _camera():
    camera = FlirCamera(offline=True)
    camera.connect()
    return camera


def _lights():
    lights = PawduinoLighting(offline=True)
    lights.connect()
    return lights


def _context(instruments, data_store=None, campaign_id=None):
    return ProtocolContext(
        gantry=FakeGantry(instruments),
        deck=FakeDeck(),
        data_store=data_store,
        campaign_id=campaign_id,
    )


class TestCapture:
    def test_capture_writes_file_and_returns_path(self, _images_dir):
        saved = capture(_context({"cam": _camera()}), instrument="cam",
                        label="hello")
        assert saved.endswith(".tiff")
        assert (_images_dir / "adhoc").exists()

    def test_capture_persists_against_position(self, tmp_path):
        data_store = DataStore(tmp_path / "test.db")
        campaign_id = data_store.create_campaign("imaging test")
        context = _context({"cam": _camera()}, data_store, campaign_id)
        saved = capture(context, instrument="cam", label="a1",
                        position="plate.A1")
        rows = data_store._conn.execute(
            "SELECT image_path FROM camera_measurements",
        ).fetchall()
        assert [row[0] for row in rows] == [saved]
        experiment = data_store._conn.execute(
            "SELECT labware_key, well_id FROM experiments",
        ).fetchone()
        assert tuple(experiment) == ("plate", "A1")

    def test_capture_without_position_saves_file_only(self, tmp_path):
        data_store = DataStore(tmp_path / "test.db")
        campaign_id = data_store.create_campaign("imaging test")
        context = _context({"cam": _camera()}, data_store, campaign_id)
        capture(context, instrument="cam")
        rows = data_store._conn.execute(
            "SELECT COUNT(*) FROM camera_measurements",
        ).fetchone()
        assert rows[0] == 0

    def test_wrong_instrument_type(self):
        with pytest.raises(ProtocolExecutionError, match="not a"):
            capture(_context({"lights": _lights()}), instrument="lights")

    def test_unknown_instrument(self):
        with pytest.raises(ProtocolExecutionError, match="No instrument"):
            capture(_context({}), instrument="cam")

    def test_camera_failure_fails_step(self):
        camera = FlirCamera(offline=False)  # not connected -> capture raises
        with pytest.raises(ProtocolExecutionError, match="capture"):
            capture(_context({"cam": camera}), instrument="cam")


class TestImageWell:
    def test_standard_sequence(self):
        camera, lights = _camera(), _lights()
        context = _context({"cam": camera, "lights": lights})
        light_log = []
        original = lights.set_channel

        def logging_set_channel(channel, brightness):
            light_log.append((channel, brightness))
            original(channel, brightness)

        lights.set_channel = logging_set_channel

        saved = image_well(context, camera="cam", well="plate.B1",
                           image_height=30.0, lights="lights", label="b1")
        assert len(saved) == 1
        # Lights: white 5% around the capture, everything off afterwards.
        assert light_log == [("white", 5)]
        assert lights.status().channels == {"white": 0, "contact": 0}
        trace = context.gantry.trace
        assert trace[0] == ("approach", (WELL.x, WELL.y))
        assert trace[1] == ("move", (WELL.x, WELL.y, WELL.z + 30.0), None)
        # Final motion: retract to safe_z.
        assert trace[-1] == ("move", (WELL.x, WELL.y, SAFE_Z), SAFE_Z)

    def test_curvature_z_stack(self):
        context = _context({"cam": _camera(), "lights": _lights()})
        saved = image_well(context, camera="cam", well="plate.B1",
                           image_height=30.0, lights="lights", label="b1",
                           mode="curvature", z_steps=3, z_step_mm=0.5)
        assert len(saved) == 3
        # Labels encode the absolute deck-frame Z of each plane
        # (WELL.z=26.0 + relative plane 29.5 = 55.5), not the relative offset.
        assert any("z=55-500mm" in path for path in saved)
        planes = [
            entry[1][2] - WELL.z
            for entry in context.gantry.trace
            if entry[0] == "move" and entry[2] is None
        ]
        assert planes == [30.0, 29.5, 29.0]

    def test_capture_failure_continues_and_retracts(self):
        camera = _camera()

        def failing_capture(*args, **kwargs):
            raise CameraCaptureError("sensor gone")

        camera.capture = failing_capture
        lights = _lights()
        context = _context({"cam": camera, "lights": lights})
        saved = image_well(context, camera="cam", well="plate.B1",
                           image_height=30.0, lights="lights")
        assert saved == []
        assert lights.status().channels == {"white": 0, "contact": 0}
        assert context.gantry.trace[-1] == ("move", (WELL.x, WELL.y, SAFE_Z), SAFE_Z)

    def test_works_without_lights(self):
        context = _context({"cam": _camera()})
        saved = image_well(context, camera="cam", well="plate.B1",
                           image_height=30.0)
        assert len(saved) == 1

    def test_unknown_mode(self):
        with pytest.raises(ProtocolExecutionError, match="unknown mode"):
            image_well(_context({"cam": _camera()}), camera="cam",
                       well="plate.B1", image_height=30.0, mode="fancy")

    def test_unresolvable_well(self):
        with pytest.raises(ProtocolExecutionError, match="cannot resolve"):
            image_well(_context({"cam": _camera()}), camera="cam",
                       well="nowhere.Z9", image_height=30.0)

    def test_motion_failure_raises_but_lights_off(self):
        camera, lights = _camera(), _lights()
        context = _context({"cam": camera, "lights": lights})

        def exploding_move(instrument, position, travel_z=None):
            raise RuntimeError("limit hit")

        context.gantry.move = exploding_move
        with pytest.raises(RuntimeError, match="limit hit"):
            image_well(context, camera="cam", well="plate.B1",
                       image_height=30.0, lights="lights")
        assert lights.status().channels == {"white": 0, "contact": 0}


class TestPathBuilder:
    def test_default_images_dir_without_override(self, monkeypatch):
        from cubos.protocol_engine.commands.camera import default_images_dir

        monkeypatch.delenv("CUBOS_IMAGES_DIR", raising=False)
        assert default_images_dir() == Path.home() / ".cubos" / "images"

    def test_collision_gets_numeric_suffix(self, monkeypatch):
        import cubos.protocol_engine.commands.camera as camera_module
        from cubos.protocol_engine.commands.camera import build_image_path

        monkeypatch.setattr(
            camera_module.time, "strftime", lambda fmt: "frozen",
        )
        context = _context({})
        first = build_image_path(context, "shot", "cam")
        first.touch()
        second = build_image_path(context, "shot", "cam")
        assert second.name == "shot_frozen_001.tiff"


class TestPersistFailure:
    def test_persist_failure_logs_and_continues(self, tmp_path):
        data_store = DataStore(tmp_path / "test.db")
        campaign_id = data_store.create_campaign("imaging test")
        context = _context({"cam": _camera()}, data_store, campaign_id)

        def exploding_resolve(target):
            raise RuntimeError("deck registry corrupt")

        context.deck.resolve_labware_target = exploding_resolve
        saved = capture(context, instrument="cam", position="plate.A1")
        # File saved, step succeeded, nothing recorded.
        assert Path(saved).exists()
        rows = data_store._conn.execute(
            "SELECT COUNT(*) FROM camera_measurements",
        ).fetchone()
        assert rows[0] == 0


class TestImageWellValidation:
    def test_non_finite_image_height(self):
        with pytest.raises(ProtocolExecutionError, match="image_height"):
            image_well(_context({"cam": _camera()}), camera="cam",
                       well="plate.B1", image_height=float("nan"))

    def test_curvature_z_steps_must_be_positive(self):
        with pytest.raises(ProtocolExecutionError, match="z_steps"):
            image_well(_context({"cam": _camera()}), camera="cam",
                       well="plate.B1", image_height=30.0,
                       mode="curvature", z_steps=0)

    def test_curvature_z_step_mm_must_be_nonnegative(self):
        with pytest.raises(ProtocolExecutionError, match="z_step_mm"):
            image_well(_context({"cam": _camera()}), camera="cam",
                       well="plate.B1", image_height=30.0,
                       mode="curvature", z_step_mm=-0.1)


class TestImageWellFailurePaths:
    def test_lighting_failure_skips_capture_and_continues(self):
        from cubos.instruments.lighting.exceptions import LightingCommandError

        lights = _lights()

        def failing_set_channel(channel, brightness):
            raise LightingCommandError("board gone")

        lights.set_channel = failing_set_channel
        context = _context({"cam": _camera(), "lights": lights})
        saved = image_well(context, camera="cam", well="plate.B1",
                           image_height=30.0, lights="lights")
        assert saved == []
        assert context.gantry.trace[-1] == ("move", (WELL.x, WELL.y, SAFE_Z), SAFE_Z)

    def test_no_safe_z_skips_retract(self):
        context = _context({"cam": _camera()})
        context.gantry.safe_z = None
        saved = image_well(context, camera="cam", well="plate.B1",
                           image_height=30.0)
        assert len(saved) == 1
        assert all(entry[2] is None for entry in context.gantry.trace
                   if entry[0] == "move")

    def test_retract_failure_is_logged_not_raised(self):
        context = _context({"cam": _camera()})
        original_move = context.gantry.move

        def move_failing_retract(instrument, position, travel_z=None):
            if travel_z is not None:
                raise RuntimeError("limit on retract")
            original_move(instrument, position, travel_z)

        context.gantry.move = move_failing_retract
        saved = image_well(context, camera="cam", well="plate.B1",
                           image_height=30.0)
        assert len(saved) == 1


class TestSummaries:
    def test_set_lights_summaries(self):
        from cubos.protocol_engine.commands import _summaries

        assert "off" in _summaries.set_lights(
            {"instrument": "lights", "all_off": True})
        assert "white 5%" in _summaries.set_lights(
            {"instrument": "lights", "channel": "white", "brightness": 5})
        assert "a1" in _summaries.capture({"instrument": "cam", "label": "a1"})
        assert "standard" in _summaries.image_well(
            {"camera": "cam", "well": "plate.B1"})


class TestLightsAutoDiscovery:
    def test_defaults_to_single_lighting_instrument(self):
        lights = _lights()
        context = _context({"cam": _camera(), "lights": lights})
        image_well(context, camera="cam", well="plate.B1", image_height=30.0)
        # The auto-discovered lights were used and turned back off.
        assert lights.status().channels == {"white": 0, "contact": 0}

    def test_multiple_lighting_instruments_require_explicit_name(self):
        context = _context({
            "cam": _camera(), "lights_a": _lights(), "lights_b": _lights(),
        })
        with pytest.raises(ProtocolExecutionError, match="lights_a, lights_b"):
            image_well(context, camera="cam", well="plate.B1",
                       image_height=30.0)

    def test_none_opts_out(self):
        lights = _lights()

        def exploding_set_channel(channel, brightness):
            raise AssertionError("lights must not be used")

        lights.set_channel = exploding_set_channel
        context = _context({"cam": _camera(), "lights": lights})
        saved = image_well(context, camera="cam", well="plate.B1",
                           image_height=30.0, lights="none")
        assert len(saved) == 1

    def test_brightness_out_of_range_raises(self):
        with pytest.raises(ProtocolExecutionError, match="brightness"):
            image_well(
                _context({"cam": _camera(), "lights": _lights()}),
                camera="cam", well="plate.B1", image_height=30.0,
                lights="lights", brightness=150,
            )

    def test_unknown_light_channel_raises(self):
        with pytest.raises(ProtocolExecutionError, match="unknown light"):
            image_well(
                _context({"cam": _camera(), "lights": _lights()}),
                camera="cam", well="plate.B1", image_height=30.0,
                lights="lights", light="ultraviolet",
            )

    def test_brightness_snaps_to_nearest_supported_level(self):
        lights = _lights()
        levels_used = []
        original = lights.set_channel

        def logging_set_channel(channel, brightness):
            levels_used.append(brightness)
            original(channel, brightness)

        lights.set_channel = logging_set_channel
        context = _context({"cam": _camera(), "lights": lights})

        image_well(context, camera="cam", well="plate.B1", image_height=30.0,
                  lights="lights", light="white", brightness=7)

        # 7% isn't a supported white level; nearest supported is 5%.
        assert levels_used == [5]

    def test_light_off_still_captures_without_setting_channel(self):
        lights = _lights()
        original = lights.set_channel

        def exploding_set_channel(channel, brightness):
            raise AssertionError("set_channel should not be called when light='off'")

        lights.set_channel = exploding_set_channel
        context = _context({"cam": _camera(), "lights": lights})

        saved = image_well(context, camera="cam", well="plate.B1",
                           image_height=30.0, lights="lights", light="off")

        assert len(saved) == 1
        lights.set_channel = original
