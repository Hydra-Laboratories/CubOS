import pytest
from unittest.mock import MagicMock

from cubos.instruments.base_instrument import BaseInstrument
from cubos.gantry.errors import LocationNotFound, MillConnectionError
from cubos.gantry.instrument_mount import InstrumentedGantry


def _mock_gantry(x=0.0, y=0.0, z=0.0):
    gantry = MagicMock()
    gantry.get_coordinates.return_value = {"x": x, "y": y, "z": z}
    return gantry


def _mock_instrument(
    name="mock",
    offset_x=0.0,
    offset_y=0.0,
    depth=0.0,
):
    instr = MagicMock(spec=BaseInstrument)
    instr.name = name
    instr.offset_x = offset_x
    instr.offset_y = offset_y
    instr.depth = depth
    instr.effective_depth = depth
    return instr


def _mock_labware(x=150.0, y=75.0, z=10.0):
    """Create a mock labware object with x, y, z deck position."""
    lw = MagicMock()
    lw.x = x
    lw.y = y
    lw.z = z
    return lw


# ─── Construction tests ──────────────────────────────────────────────────────

class TestInstrumentedGantryConstruction:

    def test_creates_with_gantry_only(self):
        gantry = _mock_gantry()
        instrumented_gantry = InstrumentedGantry(controller=gantry)
        assert instrumented_gantry.controller is gantry
        assert instrumented_gantry.instruments == {}

    def test_creates_with_instruments(self):
        gantry = _mock_gantry()
        pip = _mock_instrument("pipette", offset_x=10.0, offset_y=5.0)
        fm = _mock_instrument("filmetrics", offset_x=20.0, offset_y=0.0)
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"pipette": pip, "filmetrics": fm})
        assert len(instrumented_gantry.instruments) == 2
        assert instrumented_gantry.instruments["pipette"] is pip
        assert instrumented_gantry.instruments["filmetrics"] is fm

    def test_instruments_defaults_to_empty_dict(self):
        instrumented_gantry = InstrumentedGantry(controller=_mock_gantry())
        assert isinstance(instrumented_gantry.instruments, dict)
        assert len(instrumented_gantry.instruments) == 0

    def test_instruments_dict_is_mutable(self):
        instrumented_gantry = InstrumentedGantry(controller=_mock_gantry())
        instr = _mock_instrument("uvvis")
        instrumented_gantry.instruments["uvvis"] = instr
        assert instrumented_gantry.instruments["uvvis"] is instr

    def test_instrument_offsets_accessible(self):
        pip = _mock_instrument("pipette", offset_x=15.0, offset_y=3.5, depth=8.0)
        instrumented_gantry = InstrumentedGantry(controller=_mock_gantry(), instruments={"pipette": pip})
        assert instrumented_gantry.instruments["pipette"].offset_x == 15.0
        assert instrumented_gantry.instruments["pipette"].offset_y == 3.5
        assert instrumented_gantry.instruments["pipette"].depth == 8.0


# ─── move() tests ────────────────────────────────────────────────────────────

class TestInstrumentedGantryMove:

    def test_move_by_name_calls_gantry_move_to(self):
        gantry = _mock_gantry(x=90.0, y=45.0)
        pip = _mock_instrument("pipette", offset_x=10.0, offset_y=5.0, depth=2.0)
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"pipette": pip})

        instrumented_gantry.move("pipette", (100.0, 50.0, 20.0))

        gantry.move_to.assert_called_once_with(90.0, 45.0, 22.0, travel_z=None)

    def test_move_by_instance_calls_gantry_move_to(self):
        gantry = _mock_gantry(x=90.0, y=45.0)
        pip = _mock_instrument("pipette", offset_x=10.0, offset_y=5.0, depth=2.0)
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"pipette": pip})

        instrumented_gantry.move(pip, (100.0, 50.0, 20.0))

        gantry.move_to.assert_called_once_with(90.0, 45.0, 22.0, travel_z=None)

    def test_move_zero_offset_passes_position_through(self):
        gantry = _mock_gantry(x=200.0, y=100.0)
        instr = _mock_instrument("router", offset_x=0.0, offset_y=0.0, depth=0.0)
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"router": instr})

        instrumented_gantry.move("router", (200.0, 100.0, 10.0))

        gantry.move_to.assert_called_once_with(200.0, 100.0, 10.0, travel_z=None)

    def test_move_positive_offset(self):
        """Instrument mounted to the right (+x) of the router."""
        gantry = _mock_gantry(x=35.0, y=20.0)
        instr = _mock_instrument("sensor", offset_x=15.0, offset_y=10.0, depth=3.0)
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"sensor": instr})

        instrumented_gantry.move("sensor", (50.0, 30.0, 5.0))

        # gantry_x = 50 - 15 = 35, gantry_y = 30 - 10 = 20, gantry_z = 5 + 3 = 8
        gantry.move_to.assert_called_once_with(35.0, 20.0, 8.0, travel_z=None)

    def test_move_unknown_instrument_raises(self):
        instrumented_gantry = InstrumentedGantry(controller=_mock_gantry())
        with pytest.raises(KeyError, match="Unknown instrument 'nope'"):
            instrumented_gantry.move("nope", (0.0, 0.0, 0.0))

    def test_move_only_reads_position_and_moves(self):
        gantry = _mock_gantry()
        instr = _mock_instrument("pipette")
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"pipette": instr})

        instrumented_gantry.move("pipette", (0.0, 0.0, 0.0))

        gantry.move_to.assert_called_once()
        gantry.get_coordinates.assert_called_once()
        assert {c[0] for c in gantry.method_calls} == {"get_coordinates", "move_to"}

    def test_move_accepts_labware_object(self):
        gantry = _mock_gantry(x=140.0, y=70.0)
        instr = _mock_instrument("pipette", offset_x=10.0, offset_y=5.0, depth=2.0)
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"pipette": instr})
        lw = _mock_labware(x=150.0, y=75.0, z=10.0)

        instrumented_gantry.move("pipette", lw)

        gantry.move_to.assert_called_once_with(140.0, 70.0, 12.0, travel_z=None)

    def test_move_forwards_travel_z_plus_depth_to_gantry(self):
        """travel_z is an instrument-tip Z; gantry must receive it
        translated into gantry-frame by adding instrument depth —
        the same transform we apply to target z in +Z-up deck space."""
        gantry = _mock_gantry()
        instr = _mock_instrument("pipette", offset_x=0.0, offset_y=0.0, depth=4.0)
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"pipette": instr})

        instrumented_gantry.move("pipette", (50.0, 25.0, 10.0), travel_z=30.0)

        # gantry_z = tip_z + depth: target 10+4=14, travel 30+4=34.
        gantry.move_to.assert_called_once_with(50.0, 25.0, 14.0, travel_z=34.0)

    def test_move_uses_effective_depth_when_instrument_has_attached_geometry(self):
        """Attached pipette tips change the active tool depth at runtime."""
        gantry = _mock_gantry()
        instr = _mock_instrument("pipette", offset_x=0.0, offset_y=0.0, depth=4.0)
        instr.effective_depth = 44.0
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"pipette": instr})

        instrumented_gantry.move("pipette", (50.0, 25.0, 10.0), travel_z=30.0)

        gantry.move_to.assert_called_once_with(50.0, 25.0, 54.0, travel_z=74.0)

    def test_move_rejects_non_finite_travel_z(self):
        """travel_z flows straight through to the gantry/mill as raw
        G-code; an NaN/Inf here would emit `G01 Znan` to GRBL. Guard
        at the instrumented_gantry boundary."""
        gantry = _mock_gantry()
        instr = _mock_instrument("probe")
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"probe": instr})

        with pytest.raises(ValueError, match="non-finite travel_z"):
            instrumented_gantry.move("probe", (10.0, 20.0, 5.0), travel_z=float("nan"))
        with pytest.raises(ValueError, match="non-finite travel_z"):
            instrumented_gantry.move("probe", (10.0, 20.0, 5.0), travel_z=float("inf"))


# ─── object_position() tests ─────────────────────────────────────────────────

class TestInstrumentedGantryObjectPosition:

    def test_instrument_position_by_name(self):
        gantry = _mock_gantry(x=100.0, y=50.0, z=10.0)
        pip = _mock_instrument("pipette", offset_x=10.0, offset_y=5.0)
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"pipette": pip})

        pos = instrumented_gantry.object_position("pipette")

        assert pos == pytest.approx((110.0, 55.0))

    def test_instrument_position_by_instance(self):
        gantry = _mock_gantry(x=100.0, y=50.0, z=10.0)
        pip = _mock_instrument("pipette", offset_x=10.0, offset_y=5.0)
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"pipette": pip})

        pos = instrumented_gantry.object_position(pip)

        assert pos == pytest.approx((110.0, 55.0))

    def test_instrument_position_zero_offset(self):
        gantry = _mock_gantry(x=200.0, y=80.0)
        instr = _mock_instrument("router", offset_x=0.0, offset_y=0.0)
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"router": instr})

        pos = instrumented_gantry.object_position("router")

        assert pos == pytest.approx((200.0, 80.0))

    def test_instrument_position_reads_gantry_coordinates(self):
        gantry = _mock_gantry(x=50.0, y=25.0)
        instr = _mock_instrument("sensor")
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"sensor": instr})

        instrumented_gantry.object_position("sensor")

        gantry.get_coordinates.assert_called_once()

    def test_labware_position_from_xy_attributes(self):
        gantry = _mock_gantry()
        instrumented_gantry = InstrumentedGantry(controller=gantry)

        labware = MagicMock()
        labware.x = 150.0
        labware.y = 75.0

        pos = instrumented_gantry.object_position(labware)

        assert pos == pytest.approx((150.0, 75.0))
        gantry.get_coordinates.assert_not_called()

    def test_unknown_instrument_name_raises(self):
        instrumented_gantry = InstrumentedGantry(controller=_mock_gantry())
        with pytest.raises(KeyError, match="Unknown instrument 'nope'"):
            instrumented_gantry.object_position("nope")


# ─── connect/disconnect lifecycle tests ─────────────────────────────────────


class TestInstrumentedGantryConnectInstruments:

    def test_connect_calls_each_instrument(self):
        pip = _mock_instrument("pipette")
        uv = _mock_instrument("uvvis")
        instrumented_gantry = InstrumentedGantry(controller=_mock_gantry(), instruments={"pipette": pip, "uvvis": uv})

        instrumented_gantry.connect_instruments()

        pip.connect.assert_called_once()
        uv.connect.assert_called_once()

    def test_connect_empty_instruments_is_noop(self):
        instrumented_gantry = InstrumentedGantry(controller=_mock_gantry())
        instrumented_gantry.connect_instruments()

    def test_connect_propagates_exception(self):
        pip = _mock_instrument("pipette")
        pip.connect.side_effect = RuntimeError("no port")
        instrumented_gantry = InstrumentedGantry(controller=_mock_gantry(), instruments={"pipette": pip})

        with pytest.raises(RuntimeError, match="no port"):
            instrumented_gantry.connect_instruments()


class TestInstrumentedGantryDisconnectInstruments:

    def test_disconnect_calls_each_instrument(self):
        pip = _mock_instrument("pipette")
        uv = _mock_instrument("uvvis")
        instrumented_gantry = InstrumentedGantry(controller=_mock_gantry(), instruments={"pipette": pip, "uvvis": uv})

        instrumented_gantry.disconnect_instruments()

        pip.disconnect.assert_called_once()
        uv.disconnect.assert_called_once()

    def test_disconnect_continues_after_failure(self):
        pip = _mock_instrument("pipette")
        uv = _mock_instrument("uvvis")
        pip.disconnect.side_effect = RuntimeError("port stuck")
        instrumented_gantry = InstrumentedGantry(controller=_mock_gantry(), instruments={"pipette": pip, "uvvis": uv})

        instrumented_gantry.disconnect_instruments()

        pip.disconnect.assert_called_once()
        uv.disconnect.assert_called_once()

    def test_disconnect_empty_instruments_is_noop(self):
        instrumented_gantry = InstrumentedGantry(controller=_mock_gantry())
        instrumented_gantry.disconnect_instruments()


# ─── Raw move lift-before-XY tests ───────────────────────────────────────────


class TestRawMoveLiftsBeforeXY:
    def _ig(self, gantry, depth=0.0, safe_z=60.0, z_max=100.0):
        if z_max is not None:
            gantry.config = {"working_volume": {"z_max": z_max}}
        instr = _mock_instrument("tool", depth=depth)
        return InstrumentedGantry(controller=gantry, instruments={"tool": instr}, safe_z=safe_z)

    def test_xy_change_without_travel_z_lifts_to_ceiling(self):
        gantry = _mock_gantry(x=0.0, y=0.0, z=30.0)
        ig = self._ig(gantry, depth=10.0)
        ig.move("tool", (50.0, 20.0, 15.0))
        # tip travel_z = max(safe_z 60, z_max 100 - depth 10) = 90 -> gantry 100
        gantry.move_to.assert_called_once_with(50.0, 20.0, 25.0, travel_z=100.0)

    def test_deep_safe_z_wins_over_ceiling(self):
        gantry = _mock_gantry()
        ig = self._ig(gantry, depth=10.0, safe_z=95.0)
        ig.move("tool", (50.0, 20.0, 15.0))
        gantry.move_to.assert_called_once_with(50.0, 20.0, 25.0, travel_z=105.0)

    def test_z_only_move_is_sent_direct(self):
        gantry = _mock_gantry(x=50.0, y=20.0, z=60.0)
        ig = self._ig(gantry, depth=10.0)
        ig.move("tool", (50.0, 20.0, 15.0))
        gantry.move_to.assert_called_once_with(50.0, 20.0, 25.0, travel_z=None)

    def test_sub_tolerance_xy_jitter_counts_as_same_xy(self):
        gantry = _mock_gantry(x=50.004, y=19.996, z=60.0)
        ig = self._ig(gantry)
        ig.move("tool", (50.0, 20.0, 15.0))
        assert gantry.move_to.call_args.kwargs == {"travel_z": None}

    def test_explicit_travel_z_is_honored(self):
        gantry = _mock_gantry()
        ig = self._ig(gantry, depth=10.0)
        ig.move("tool", (50.0, 20.0, 15.0), travel_z=30.0)
        gantry.move_to.assert_called_once_with(50.0, 20.0, 25.0, travel_z=40.0)

    def test_unknown_position_lifts(self):
        gantry = _mock_gantry(x=50.0, y=20.0)
        gantry.get_coordinates.side_effect = LocationNotFound("no status")
        ig = self._ig(gantry)
        ig.move("tool", (50.0, 20.0, 15.0))
        assert gantry.move_to.call_args.kwargs == {"travel_z": 100.0}

    def test_unparseable_position_lifts(self):
        gantry = MagicMock()
        ig = self._ig(gantry)
        ig.move("tool", (50.0, 20.0, 15.0))
        assert gantry.move_to.call_args.kwargs == {"travel_z": 100.0}

    def test_position_missing_axis_lifts(self):
        gantry = _mock_gantry()
        gantry.get_coordinates.return_value = {"z": 10.0}
        ig = self._ig(gantry)
        ig.move("tool", (50.0, 20.0, 15.0))
        assert gantry.move_to.call_args.kwargs == {"travel_z": 100.0}

    def test_non_finite_position_lifts(self):
        gantry = _mock_gantry(x=float("nan"), y=20.0)
        ig = self._ig(gantry)
        ig.move("tool", (50.0, 20.0, 15.0))
        assert gantry.move_to.call_args.kwargs == {"travel_z": 100.0}

    def test_connection_error_propagates(self):
        gantry = _mock_gantry()
        gantry.get_coordinates.side_effect = MillConnectionError("serial dropped")
        ig = self._ig(gantry)
        with pytest.raises(MillConnectionError):
            ig.move("tool", (50.0, 20.0, 15.0))
        gantry.move_to.assert_not_called()

    def test_no_safe_z_and_no_volume_falls_back_to_direct(self, caplog):
        gantry = _mock_gantry()
        gantry.config = {}
        instr = _mock_instrument("tool")
        ig = InstrumentedGantry(controller=gantry, instruments={"tool": instr})
        with caplog.at_level("WARNING"):
            ig.move("tool", (50.0, 20.0, 15.0))
        gantry.move_to.assert_called_once_with(50.0, 20.0, 15.0, travel_z=None)
        assert "will not lift" in caplog.text

    def test_multi_tool_safe_travel_z_uses_effective_depth(self):
        gantry = _mock_gantry()
        gantry.config = {"working_volume": {"z_max": 100.0}}
        instr = _mock_instrument("tool", depth=10.0)
        instr.effective_depth = 40.0
        ig = InstrumentedGantry(controller=gantry, instruments={"tool": instr}, safe_z=20.0)
        assert ig.multi_tool_safe_travel_z("tool") == 60.0


# ─── move_to_labware tests ───────────────────────────────────────────────────
#
# move_to_labware issues one gantry.move_to with travel_z = approach Z.
# The gantry driver does the lift → XY → descent sequence internally
# from that single call. move_to_labware ends at the approach Z — it
# does NOT descend to the action Z; commands that need to engage
# (measure, aspirate, etc.) follow up with a raw instrumented_gantry.move() to
# descend. See test_measure_command and test_pipette_commands for
# descent behavior.


class TestInstrumentedGantryMoveToLabware:

    def test_single_gantry_call_with_travel_z(self):
        """move_to_labware travels XY at safe_z (absolute deck-frame)."""
        gantry = _mock_gantry()
        instr = _mock_instrument()
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"sensor": instr}, safe_z=85.0)
        instrumented_gantry.move_to_labware("sensor", _mock_labware(x=100, y=50, z=20))

        assert gantry.move_to.call_count == 1
        call = gantry.move_to.call_args
        assert call.args == (100.0, 50.0, 85.0)
        assert call.kwargs == {"travel_z": 85.0}

    def test_applies_instrument_xy_offsets_and_depth(self):
        """Instrument offsets shift gantry coords; depth shifts the
        gantry Z (tip Z + depth)."""
        gantry = _mock_gantry()
        instr = _mock_instrument(offset_x=10.0, offset_y=-5.0, depth=2.0)
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"probe": instr}, safe_z=15.0)
        instrumented_gantry.move_to_labware("probe", _mock_labware(x=100, y=50, z=30))

        call = gantry.move_to.call_args
        # tip Z = safe_z = 15; gantry Z = 15 + depth(2) = 17.
        # gantry x = 100 - 10 = 90; gantry y = 50 - (-5) = 55.
        assert call.args == (90.0, 55.0, 17.0)
        assert call.kwargs == {"travel_z": 17.0}

    def test_accepts_tuple_position(self):
        gantry = _mock_gantry()
        instr = _mock_instrument()
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"probe": instr}, safe_z=20.0)
        instrumented_gantry.move_to_labware("probe", (50.0, 40.0, 10.0))

        assert gantry.move_to.call_count == 1
        call = gantry.move_to.call_args
        assert call.args == (50.0, 40.0, 20.0)
        assert call.kwargs == {"travel_z": 20.0}

    def test_rejects_nan_position_z(self):
        """NaN in position z is caught (via InstrumentedGantry.move's validation)."""
        gantry = _mock_gantry()
        instr = _mock_instrument()
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"probe": instr}, safe_z=20.0)
        with pytest.raises(ValueError, match="non-finite"):
            instrumented_gantry.move_to_labware("probe", (10.0, 20.0, float("nan")))

    def test_rejects_infinite_position_x(self):
        gantry = _mock_gantry()
        instr = _mock_instrument()
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"probe": instr}, safe_z=20.0)
        with pytest.raises(ValueError, match="non-finite"):
            instrumented_gantry.move_to_labware("probe", (float("inf"), 20.0, 10.0))

    def test_unset_safe_z_raises(self):
        gantry = _mock_gantry()
        instr = _mock_instrument()
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"probe": instr})
        with pytest.raises(ValueError, match="safe_z"):
            instrumented_gantry.move_to_labware("probe", (10.0, 20.0, 5.0))

    def test_raw_move_rejects_non_finite_z(self):
        """Raw InstrumentedGantry.move (used for descent in commands) must also guard
        against NaN/Inf coords — otherwise a bad measurement_height could
        silently send the gantry to a non-finite Z."""
        gantry = _mock_gantry()
        instr = _mock_instrument()
        instrumented_gantry = InstrumentedGantry(controller=gantry, instruments={"probe": instr})
        with pytest.raises(ValueError, match="non-finite"):
            instrumented_gantry.move("probe", (10.0, 20.0, float("nan")))


# ─── Travel-ceiling tests ────────────────────────────────────────────────────

class TestTravelCeiling:
    def _controller_with_volume(self, z_max=167.848):
        controller = _mock_gantry()
        controller.config = {"working_volume": {"z_max": z_max}}
        return controller

    def test_travel_rides_working_volume_ceiling(self):
        controller = self._controller_with_volume()
        instr = _mock_instrument(depth=0.0)
        ig = InstrumentedGantry(
            controller=controller, instruments={"pipette": instr}, safe_z=97.848,
        )
        ig.move_to_labware("pipette", _mock_labware(z=46.0))
        args, kwargs = controller.move_to.call_args
        assert kwargs["travel_z"] == 167.848
        assert args[2] == 97.848

    def test_deep_instrument_still_travels_at_ceiling(self):
        controller = self._controller_with_volume()
        instr = _mock_instrument(depth=70.0)
        ig = InstrumentedGantry(
            controller=controller, instruments={"pipette": instr}, safe_z=97.848,
        )
        ig.move_to_labware("pipette", _mock_labware())
        _, kwargs = controller.move_to.call_args
        assert kwargs["travel_z"] == 167.848

    def test_without_working_volume_falls_back_to_safe_z(self):
        controller = _mock_gantry()
        controller.config = {}
        instr = _mock_instrument(depth=5.0)
        ig = InstrumentedGantry(
            controller=controller, instruments={"probe": instr}, safe_z=60.0,
        )
        ig.move_to_labware("probe", _mock_labware())
        _, kwargs = controller.move_to.call_args
        assert kwargs["travel_z"] == 65.0
