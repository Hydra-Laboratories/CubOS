"""Tests for pipette protocol commands."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from cubos.data.data_store import DataStore
from cubos.deck.deck import Deck
from cubos.deck.labware.labware import Coordinate3D
from cubos.deck.labware.tip_rack import DEFAULT_TIP_LENGTH_MM, TipRack
from cubos.deck.labware.vial import Vial
from cubos.deck.labware.vial_grid import VialGrid
from cubos.deck.labware.vial_holder import VialHolder
from cubos.deck.labware.well_plate import WellPlate
from cubos.deck.labware.well_plate_holder import WellPlateHolder
from cubos.protocol_engine.errors import ProtocolExecutionError
from cubos.protocol_engine.runtime import ProtocolContext


# ─── Helpers ──────────────────────────────────────────────────────────────────


PIPETTE_HEIGHT_MM = 14.10


def _mock_context(
    resolve_return: Coordinate3D | None = None,
    has_pipette: bool = True,
    height: float | None = PIPETTE_HEIGHT_MM,
) -> ProtocolContext:
    coord = resolve_return or Coordinate3D(x=100.0, y=50.0, z=PIPETTE_HEIGHT_MM)

    board = MagicMock()
    deck = MagicMock()
    deck.resolve_coordinate.return_value = coord
    labware = MagicMock(height=height)
    deck.__getitem__ = MagicMock(return_value=labware)

    if has_pipette:
        pipette = MagicMock()
        pipette.aspirate.return_value = MagicMock(success=True, volume_ul=100.0)
        pipette.dispense.return_value = MagicMock(success=True, volume_ul=100.0)
        pipette.mix.return_value = MagicMock(success=True, volume_ul=50.0, cycles=3)
        # Default: pipette's instrument-config measurement_height is 0 (well surface).
        board.instruments = {"pipette": pipette}
    else:
        board.instruments = {}

    return ProtocolContext(
        gantry=board,
        deck=deck,
        logger=logging.getLogger("test_pipette_commands"),
    )


def _get_pipette(ctx: ProtocolContext) -> MagicMock:
    return ctx.gantry.instruments["pipette"]


# ─── _parse_position tests ───────────────────────────────────────────────────


class TestParsePosition:

    def test_plate_and_well(self):
        from cubos.protocol_engine.commands.pipette import _parse_position

        assert _parse_position("plate_1.A1") == ("plate_1", "A1")

    def test_vial_no_well(self):
        from cubos.protocol_engine.commands.pipette import _parse_position

        assert _parse_position("vial_1") == ("vial_1", None)

    def test_nested_labware_path_uses_last_split(self):
        from cubos.protocol_engine.commands.pipette import _parse_position

        assert _parse_position("holder.plate.A1") == ("holder.plate", "A1")

    def test_return_type(self):
        from cubos.protocol_engine.commands.pipette import _parse_position

        result = _parse_position("plate_1.B2")
        assert isinstance(result, tuple)
        assert isinstance(result[0], str)
        assert isinstance(result[1], str)

        result_none = _parse_position("vial_1")
        assert result_none[1] is None


# ─── aspirate tests ──────────────────────────────────────────────────────────


class TestAspirateCommand:

    def test_resolves_position_via_deck(self):
        from cubos.protocol_engine.commands.pipette import aspirate

        ctx = _mock_context()
        aspirate(ctx, position="plate_1.A1", volume_ul=100.0)
        ctx.deck.resolve_coordinate.assert_called_once_with("plate_1.A1")

    def test_moves_then_aspirates(self):
        from cubos.protocol_engine.commands.pipette import aspirate

        ctx = _mock_context()
        call_order = []
        ctx.gantry.move_to_labware.side_effect = lambda *a, **kw: call_order.append("move")
        _get_pipette(ctx).aspirate.side_effect = lambda *a: call_order.append("aspirate")

        aspirate(ctx, position="plate_1.A1", volume_ul=100.0)
        assert call_order == ["move", "aspirate"]

    def test_passes_volume_and_speed(self):
        from cubos.protocol_engine.commands.pipette import aspirate

        ctx = _mock_context()
        aspirate(ctx, position="plate_1.A1", volume_ul=75.0, speed=25.0)
        _get_pipette(ctx).aspirate.assert_called_once_with(75.0, 25.0)

    def test_default_speed(self):
        from cubos.protocol_engine.commands.pipette import aspirate

        ctx = _mock_context()
        aspirate(ctx, position="plate_1.A1", volume_ul=100.0)
        _get_pipette(ctx).aspirate.assert_called_once_with(100.0, 50.0)

    def test_moves_pipette_to_resolved_coord(self):
        from cubos.protocol_engine.commands.pipette import aspirate

        coord = Coordinate3D(x=10.0, y=20.0, z=75.0)
        ctx = _mock_context(resolve_return=coord)
        aspirate(ctx, position="plate_1.A1", volume_ul=100.0)
        ctx.gantry.move_to_labware.assert_called_once_with("pipette", coord)

    def test_descends_to_well_bottom_after_approach(self):
        """aspirate descends to the labware reference Z by default."""
        from cubos.protocol_engine.commands.pipette import aspirate

        coord = Coordinate3D(x=10.0, y=20.0, z=PIPETTE_HEIGHT_MM)
        ctx = _mock_context(resolve_return=coord)
        aspirate(ctx, position="plate_1.A1", volume_ul=100.0)
        ctx.gantry.move.assert_called_once_with(
            "pipette", (10.0, 20.0, PIPETTE_HEIGHT_MM),
        )

    def test_approach_then_descend_then_aspirate(self):
        """Ordering: approach (move_to_labware) -> descent (move) -> aspirate."""
        from cubos.protocol_engine.commands.pipette import aspirate

        ctx = _mock_context()
        order = []
        ctx.gantry.move_to_labware.side_effect = lambda *a, **k: order.append("approach")
        ctx.gantry.move.side_effect = lambda *a, **k: order.append("descent")
        _get_pipette(ctx).aspirate.side_effect = lambda *a: order.append("aspirate")

        aspirate(ctx, position="plate_1.A1", volume_ul=100.0)
        assert order == ["approach", "descent", "aspirate"]

    def test_raises_when_no_pipette(self):
        from cubos.protocol_engine.commands.pipette import aspirate

        ctx = _mock_context(has_pipette=False)
        with pytest.raises(ProtocolExecutionError, match="[Nn]o pipette"):
            aspirate(ctx, position="plate_1.A1", volume_ul=100.0)

    def test_rejects_standalone_aspirate_when_fluid_tracking_is_active(self):
        from cubos.protocol_engine.commands.pipette import aspirate

        ctx = _mock_context()
        ctx.data_store = MagicMock()
        ctx.fluid_state_id = 3
        ctx.campaign_id = 4

        with pytest.raises(ProtocolExecutionError, match="Standalone aspirate"):
            aspirate(ctx, position="plate_1.A1", volume_ul=25.0)

        ctx.gantry.move_to_labware.assert_not_called()
        ctx.gantry.instruments["pipette"].aspirate.assert_not_called()


# ─── dispense tests ──────────────────────────────────────────────────────────


class TestDispenseCommand:

    def test_resolves_position_via_deck(self):
        from cubos.protocol_engine.commands.pipette import dispense

        ctx = _mock_context()
        dispense(ctx, position="plate_1.A1", volume_ul=100.0)
        ctx.deck.resolve_coordinate.assert_called_once_with("plate_1.A1")

    def test_moves_then_dispenses(self):
        from cubos.protocol_engine.commands.pipette import dispense

        ctx = _mock_context()
        call_order = []
        ctx.gantry.move_to_labware.side_effect = lambda *a, **kw: call_order.append("move")
        _get_pipette(ctx).dispense.side_effect = lambda *a: call_order.append("dispense")

        dispense(ctx, position="plate_1.A1", volume_ul=100.0)
        assert call_order == ["move", "dispense"]

    def test_passes_volume_and_speed(self):
        from cubos.protocol_engine.commands.pipette import dispense

        ctx = _mock_context()
        dispense(ctx, position="plate_1.A1", volume_ul=80.0, speed=30.0)
        _get_pipette(ctx).dispense.assert_called_once_with(80.0, 30.0)

    def test_default_speed(self):
        from cubos.protocol_engine.commands.pipette import dispense

        ctx = _mock_context()
        dispense(ctx, position="plate_1.A1", volume_ul=100.0)
        _get_pipette(ctx).dispense.assert_called_once_with(100.0, 50.0)

    def test_raises_when_no_pipette(self):
        from cubos.protocol_engine.commands.pipette import dispense

        ctx = _mock_context(has_pipette=False)
        with pytest.raises(ProtocolExecutionError, match="[Nn]o pipette"):
            dispense(ctx, position="plate_1.A1", volume_ul=100.0)


# ─── blowout tests ───────────────────────────────────────────────────────────


class TestBlowoutCommand:

    def test_resolves_position_via_deck(self):
        from cubos.protocol_engine.commands.pipette import blowout

        ctx = _mock_context()
        blowout(ctx, position="plate_1.A1")
        ctx.deck.resolve_coordinate.assert_called_once_with("plate_1.A1")

    def test_moves_then_blows_out(self):
        from cubos.protocol_engine.commands.pipette import blowout

        ctx = _mock_context()
        call_order = []
        ctx.gantry.move_to_labware.side_effect = lambda *a, **kw: call_order.append("move")
        _get_pipette(ctx).blowout.side_effect = lambda *a: call_order.append("blowout")

        blowout(ctx, position="plate_1.A1")
        assert call_order == ["move", "blowout"]

    def test_passes_speed(self):
        from cubos.protocol_engine.commands.pipette import blowout

        ctx = _mock_context()
        blowout(ctx, position="plate_1.A1", speed=25.0)
        _get_pipette(ctx).blowout.assert_called_once_with(25.0)

    def test_default_speed(self):
        from cubos.protocol_engine.commands.pipette import blowout

        ctx = _mock_context()
        blowout(ctx, position="plate_1.A1")
        _get_pipette(ctx).blowout.assert_called_once_with(50.0)

    def test_raises_when_no_pipette(self):
        from cubos.protocol_engine.commands.pipette import blowout

        ctx = _mock_context(has_pipette=False)
        with pytest.raises(ProtocolExecutionError, match="[Nn]o pipette"):
            blowout(ctx, position="plate_1.A1")


# ─── mix tests ────────────────────────────────────────────────────────────────


class TestMixCommand:

    def test_resolves_position_via_deck(self):
        from cubos.protocol_engine.commands.pipette import mix

        ctx = _mock_context()
        mix(ctx, position="plate_1.A1", volume_ul=50.0)
        ctx.deck.resolve_coordinate.assert_called_once_with("plate_1.A1")

    def test_moves_then_mixes(self):
        from cubos.protocol_engine.commands.pipette import mix

        ctx = _mock_context()
        call_order = []
        ctx.gantry.move_to_labware.side_effect = lambda *a, **kw: call_order.append("move")
        _get_pipette(ctx).mix.side_effect = lambda *a, **kw: call_order.append("mix")

        mix(ctx, position="plate_1.A1", volume_ul=50.0)
        assert call_order == ["move", "mix"]

    def test_passes_volume_cycles_speed_and_engaged_tip(self):
        from cubos.protocol_engine.commands.pipette import mix

        ctx = _mock_context()
        mix(ctx, position="plate_1.A1", volume_ul=50.0, cycles=5, speed=20.0, height=-3.0)
        _get_pipette(ctx).mix.assert_called_once_with(
            50.0, 5, 20.0,
            gantry=ctx.gantry, position=(100.0, 50.0, PIPETTE_HEIGHT_MM - 3.0),
        )

    def test_default_cycles_and_speed(self):
        from cubos.protocol_engine.commands.pipette import mix

        ctx = _mock_context()
        mix(ctx, position="plate_1.A1", volume_ul=50.0)
        _get_pipette(ctx).mix.assert_called_once_with(
            50.0, 3, 50.0,
            gantry=ctx.gantry, position=(100.0, 50.0, PIPETTE_HEIGHT_MM),
        )

    def test_raises_when_no_pipette(self):
        from cubos.protocol_engine.commands.pipette import mix

        ctx = _mock_context(has_pipette=False)
        with pytest.raises(ProtocolExecutionError, match="[Nn]o pipette"):
            mix(ctx, position="plate_1.A1", volume_ul=50.0)

    def test_tracked_mix_journals_before_motion_and_applies_after_success(self):
        from cubos.protocol_engine.commands.pipette import mix

        ctx = _mock_context()
        ctx.campaign_id = 7
        ctx.fluid_state_id = 11
        ctx.deck.resolve_labware_target.return_value = SimpleNamespace(
            labware_key="plate", location_id="A1",
        )
        events = []
        store = MagicMock()
        store.begin_fluid_mix.side_effect = (
            lambda *args, **kwargs: events.append("begin") or True
        )
        store.complete_fluid_mix.side_effect = (
            lambda operation_key: events.append("complete")
        )
        ctx.data_store = store
        ctx.gantry.move_to_labware.side_effect = (
            lambda *args, **kwargs: events.append("move")
        )
        _get_pipette(ctx).mix.side_effect = lambda *args, **kwargs: events.append("mix")

        mix(
            ctx,
            position="plate.A1",
            volume_ul=25.0,
            cycles=4,
            speed=20.0,
        )

        assert events == ["begin", "move", "mix", "complete"]
        store.begin_fluid_mix.assert_called_once()
        args = store.begin_fluid_mix.call_args.args
        assert args[0] == 11
        assert args[1].endswith(":mix")
        assert (args[2].labware_key, args[2].location_id) == ("plate", "A1")
        assert args[3:] == (25.0, 4, 20.0, 0.0)
        assert store.begin_fluid_mix.call_args.kwargs == {"campaign_id": 7}

    def test_tracked_mix_skips_exact_already_applied_replay_without_motion(self):
        from cubos.protocol_engine.commands.pipette import mix

        ctx = _mock_context()
        ctx.campaign_id = 7
        ctx.fluid_state_id = 11
        ctx.data_store = MagicMock()
        ctx.deck.resolve_labware_target.return_value = SimpleNamespace(
            labware_key="plate", location_id="A1",
        )
        ctx.data_store.begin_fluid_mix.return_value = False

        assert mix(ctx, position="plate.A1", volume_ul=25.0) is None
        ctx.gantry.move_to_labware.assert_not_called()
        _get_pipette(ctx).mix.assert_not_called()
        ctx.data_store.complete_fluid_mix.assert_not_called()

    def test_tracked_mix_failure_requires_reconciliation(self):
        from cubos.protocol_engine.commands.pipette import mix

        ctx = _mock_context()
        ctx.campaign_id = 7
        ctx.fluid_state_id = 11
        ctx.data_store = MagicMock()
        ctx.deck.resolve_labware_target.return_value = SimpleNamespace(
            labware_key="plate", location_id="A1",
        )
        ctx.data_store.begin_fluid_mix.return_value = True
        _get_pipette(ctx).mix.side_effect = RuntimeError("mix uncertain")

        with pytest.raises(RuntimeError, match="mix uncertain"):
            mix(ctx, position="plate.A1", volume_ul=25.0)

        ctx.data_store.mark_fluid_reconciliation_required.assert_called_once()
        ctx.data_store.complete_fluid_mix.assert_not_called()


# ─── pick_up_tip tests ───────────────────────────────────────────────────────


def _tip_rack_context(*, has_pipette: bool = True) -> tuple[ProtocolContext, TipRack, MagicMock]:
    rack = TipRack(
        name="tips",
        model_name="test_tip_rack",
        rows=1,
        columns=1,
        pickup_z=42.0,
        drop_z=32.0,
        tip_length=DEFAULT_TIP_LENGTH_MM,
        location=Coordinate3D(x=10.0, y=20.0, z=42.0),
        length=1.0,
        width=1.0,
        height=10.0,
        tips={"A1": Coordinate3D(x=10.0, y=20.0, z=42.0)},
    )
    board = MagicMock()
    pipette = MagicMock() if has_pipette else None
    board.instruments = {"pipette": pipette} if has_pipette else {}
    ctx = ProtocolContext(gantry=board, deck=Deck({"tips": rack}))
    return ctx, rack, pipette


def _two_tip_rack_context(*, has_pipette: bool = True) -> tuple[ProtocolContext, TipRack, MagicMock]:
    rack = TipRack(
        name="tips",
        model_name="test_tip_rack",
        rows=1,
        columns=2,
        pickup_z=42.0,
        drop_z=32.0,
        tip_length=DEFAULT_TIP_LENGTH_MM,
        location=Coordinate3D(x=10.0, y=20.0, z=42.0),
        length=8.0,
        width=1.0,
        height=10.0,
        tips={
            "A1": Coordinate3D(x=10.0, y=20.0, z=42.0),
            "A2": Coordinate3D(x=18.0, y=20.0, z=42.0),
        },
    )
    board = MagicMock()
    pipette = MagicMock() if has_pipette else None
    board.instruments = {"pipette": pipette} if has_pipette else {}
    ctx = ProtocolContext(gantry=board, deck=Deck({"tips": rack}))
    return ctx, rack, pipette


class TestPickUpTipCommand:

    def test_moves_then_picks_up(self):
        from cubos.protocol_engine.commands.pipette import pick_up_tip

        ctx, _rack, pipette = _tip_rack_context()
        call_order = []
        ctx.gantry.move_to_labware.side_effect = lambda *a, **kw: call_order.append("move")
        pipette.pick_up_tip.side_effect = lambda *a: call_order.append("pick_up_tip")

        pick_up_tip(ctx, position="tips.A1")
        assert call_order == ["move", "pick_up_tip"]

    def test_passes_speed(self):
        from cubos.protocol_engine.commands.pipette import pick_up_tip

        ctx, _rack, pipette = _tip_rack_context()
        pick_up_tip(ctx, position="tips.A1", speed=10.0)
        pipette.pick_up_tip.assert_called_once_with(10.0)

    def test_default_speed(self):
        from cubos.protocol_engine.commands.pipette import pick_up_tip

        ctx, _rack, pipette = _tip_rack_context()
        pick_up_tip(ctx, position="tips.A1")
        pipette.pick_up_tip.assert_called_once_with(50.0)

    def test_sets_attached_tip_extension_and_consumes_tip(self):
        from cubos.protocol_engine.commands.pipette import pick_up_tip

        ctx, rack, pipette = _tip_rack_context()
        pick_up_tip(ctx, position="tips.A1")

        pipette.pick_up_tip.assert_called_once_with(50.0)
        pipette.set_attached_tip_extension.assert_called_once_with(DEFAULT_TIP_LENGTH_MM)
        assert rack.is_tip_present("A1") is False

    def test_raises_when_no_pipette(self):
        from cubos.protocol_engine.commands.pipette import pick_up_tip

        ctx, _rack, _pipette = _tip_rack_context(has_pipette=False)
        with pytest.raises(ProtocolExecutionError, match="[Nn]o pipette"):
            pick_up_tip(ctx, position="tips.A1")

    def test_raises_when_target_is_not_a_tip_rack(self):
        from cubos.protocol_engine.commands.pipette import pick_up_tip

        ctx = _mock_context()
        with pytest.raises(ProtocolExecutionError):
            pick_up_tip(ctx, position="tiprack_1.A1")

    def test_raises_when_slot_already_consumed(self):
        from cubos.protocol_engine.commands.pipette import pick_up_tip

        ctx, rack, _pipette = _tip_rack_context()
        rack.mark_tip_used("A1")
        with pytest.raises(ProtocolExecutionError, match="not available"):
            pick_up_tip(ctx, position="tips.A1")

    def test_untracked_next_available_selection_picks_first_present_tip(self):
        from cubos.protocol_engine.commands.pipette import pick_up_tip

        ctx, rack, pipette = _two_tip_rack_context()
        rack.mark_tip_used("A1")

        pick_up_tip(ctx, position="tips")

        pipette.pick_up_tip.assert_called_once_with(50.0)
        pipette.set_attached_tip_extension.assert_called_once_with(
            DEFAULT_TIP_LENGTH_MM,
        )
        assert rack.is_tip_present("A2") is False

    def test_untracked_next_available_raises_when_rack_is_empty(self):
        from cubos.protocol_engine.commands.pipette import pick_up_tip

        ctx, rack, _pipette = _tip_rack_context()
        rack.mark_tip_used("A1")
        with pytest.raises(ProtocolExecutionError, match="no available tips"):
            pick_up_tip(ctx, position="tips")

    def test_tracked_pick_up_tip_journals_before_motion_and_commits_after_success(
        self,
    ):
        from cubos.protocol_engine.commands.pipette import pick_up_tip

        ctx, rack, pipette = _tip_rack_context()
        ctx.campaign_id = 7
        ctx.fluid_state_id = 11
        events = []
        store = MagicMock()
        store.begin_pick_up_tip.side_effect = (
            lambda *args, **kwargs: events.append("begin") or (True, "A1", 59.3)
        )
        store.complete_pick_up_tip.side_effect = (
            lambda operation_key: events.append("complete")
        )
        ctx.data_store = store
        ctx.gantry.move_to_labware.side_effect = (
            lambda *args, **kwargs: events.append("move")
        )
        pipette.pick_up_tip.side_effect = lambda *args: events.append("pick_up_tip")

        pick_up_tip(ctx, position="tips.A1")

        assert events == ["begin", "move", "pick_up_tip", "complete"]
        store.begin_pick_up_tip.assert_called_once()
        args = store.begin_pick_up_tip.call_args.args
        assert args[0] == 11
        assert args[1].endswith(":pick_up_tip")
        assert args[2:] == ("tips", "A1", rack.tip_length)
        assert store.begin_pick_up_tip.call_args.kwargs == {"campaign_id": 7}
        pipette.set_attached_tip_extension.assert_called_once_with(rack.tip_length)
        assert rack.is_tip_present("A1") is False

    def test_tracked_pick_up_tip_skips_already_applied_replay_and_restores_extension(
        self,
    ):
        from cubos.protocol_engine.commands.pipette import pick_up_tip

        ctx, rack, pipette = _tip_rack_context()
        ctx.campaign_id = 7
        ctx.fluid_state_id = 11
        ctx.data_store = MagicMock()
        ctx.data_store.begin_pick_up_tip.return_value = (False, "A1", 59.3)

        assert pick_up_tip(ctx, position="tips.A1") is None

        ctx.gantry.move_to_labware.assert_not_called()
        pipette.pick_up_tip.assert_not_called()
        ctx.data_store.complete_pick_up_tip.assert_not_called()
        pipette.set_attached_tip_extension.assert_called_once_with(59.3)
        # The replay must not consume a second physical tip.
        assert rack.is_tip_present("A1") is True

    def test_tracked_pick_up_tip_failure_marks_reconciliation_required(self):
        from cubos.protocol_engine.commands.pipette import pick_up_tip

        ctx, _rack, pipette = _tip_rack_context()
        ctx.campaign_id = 7
        ctx.fluid_state_id = 11
        ctx.data_store = MagicMock()
        ctx.data_store.begin_pick_up_tip.return_value = (True, "A1", 59.3)
        pipette.pick_up_tip.side_effect = RuntimeError("pickup outcome unknown")

        with pytest.raises(RuntimeError, match="pickup outcome unknown"):
            pick_up_tip(ctx, position="tips.A1")

        ctx.data_store.mark_tip_reconciliation_required.assert_called_once()
        ctx.data_store.complete_pick_up_tip.assert_not_called()

    def test_tracked_pick_up_tip_commit_failure_is_not_silently_ignored(self):
        from cubos.protocol_engine.commands.pipette import pick_up_tip

        ctx, _rack, _pipette = _tip_rack_context()
        ctx.campaign_id = 7
        ctx.fluid_state_id = 11
        ctx.data_store = MagicMock()
        ctx.data_store.begin_pick_up_tip.return_value = (True, "A1", 59.3)
        ctx.data_store.complete_pick_up_tip.side_effect = RuntimeError(
            "database is locked"
        )

        with pytest.raises(ProtocolExecutionError, match="commit"):
            pick_up_tip(ctx, position="tips.A1")

        ctx.data_store.mark_tip_reconciliation_required.assert_called_once()

    def test_tracked_pick_up_tip_preflight_failure_stops_before_motion(self):
        from cubos.protocol_engine.commands.pipette import pick_up_tip

        ctx, _rack, pipette = _tip_rack_context()
        ctx.campaign_id = 7
        ctx.fluid_state_id = 11
        ctx.data_store = MagicMock()
        ctx.data_store.begin_pick_up_tip.side_effect = ValueError(
            "no available tip in rack"
        )

        with pytest.raises(ProtocolExecutionError, match="preflight failed"):
            pick_up_tip(ctx, position="tips.A1")

        ctx.gantry.move_to_labware.assert_not_called()
        pipette.pick_up_tip.assert_not_called()


# ─── drop_tip tests ──────────────────────────────────────────────────────────


class TestDropTipCommand:

    def test_resolves_position_via_deck(self):
        from cubos.protocol_engine.commands.pipette import drop_tip

        ctx = _mock_context()
        drop_tip(ctx, position="waste_1")
        ctx.deck.resolve_coordinate.assert_called_once_with("waste_1")

    def test_moves_then_drops(self):
        from cubos.protocol_engine.commands.pipette import drop_tip

        ctx = _mock_context()
        call_order = []
        ctx.gantry.move_to_labware.side_effect = lambda *a, **kw: call_order.append("move")
        _get_pipette(ctx).drop_tip.side_effect = lambda *a: call_order.append("drop_tip")

        drop_tip(ctx, position="waste_1")
        assert call_order == ["move", "drop_tip"]

    def test_passes_speed(self):
        from cubos.protocol_engine.commands.pipette import drop_tip

        ctx = _mock_context()
        drop_tip(ctx, position="waste_1", speed=10.0)
        _get_pipette(ctx).drop_tip.assert_called_once_with(10.0)

    def test_default_speed(self):
        from cubos.protocol_engine.commands.pipette import drop_tip

        ctx = _mock_context()
        drop_tip(ctx, position="waste_1")
        _get_pipette(ctx).drop_tip.assert_called_once_with(50.0)

    def test_clears_attached_tip_extension_after_drop(self):
        from cubos.protocol_engine.commands.pipette import drop_tip

        ctx = _mock_context()
        drop_tip(ctx, position="waste_1")

        _get_pipette(ctx).clear_attached_tip_extension.assert_called_once_with()

    def test_raises_when_no_pipette(self):
        from cubos.protocol_engine.commands.pipette import drop_tip

        ctx = _mock_context(has_pipette=False)
        with pytest.raises(ProtocolExecutionError, match="[Nn]o pipette"):
            drop_tip(ctx, position="waste_1")

    def test_tracked_drop_tip_journals_before_motion_and_commits_after_success(self):
        from cubos.protocol_engine.commands.pipette import drop_tip

        ctx = _mock_context()
        ctx.campaign_id = 7
        ctx.fluid_state_id = 11
        events = []
        store = MagicMock()
        store.begin_drop_tip.side_effect = (
            lambda *args, **kwargs: events.append("begin") or (True, "tips", "A1")
        )
        store.complete_drop_tip.side_effect = (
            lambda operation_key: events.append("complete")
        )
        ctx.data_store = store
        ctx.gantry.move_to_labware.side_effect = (
            lambda *args, **kwargs: events.append("move")
        )
        pipette = _get_pipette(ctx)
        pipette.drop_tip.side_effect = lambda *args: events.append("drop_tip")

        drop_tip(ctx, position="waste_1")

        assert events == ["begin", "move", "drop_tip", "complete"]
        store.begin_drop_tip.assert_called_once()
        args = store.begin_drop_tip.call_args.args
        assert args[0] == 11
        assert args[1].endswith(":drop_tip")
        assert store.begin_drop_tip.call_args.kwargs == {"campaign_id": 7}
        pipette.clear_attached_tip_extension.assert_called_once_with()

    def test_tracked_drop_tip_skips_already_applied_replay(self):
        from cubos.protocol_engine.commands.pipette import drop_tip

        ctx = _mock_context()
        ctx.campaign_id = 7
        ctx.fluid_state_id = 11
        ctx.data_store = MagicMock()
        ctx.data_store.begin_drop_tip.return_value = (False, "tips", "A1")

        assert drop_tip(ctx, position="waste_1") is None

        ctx.gantry.move_to_labware.assert_not_called()
        _get_pipette(ctx).drop_tip.assert_not_called()
        ctx.data_store.complete_drop_tip.assert_not_called()
        _get_pipette(ctx).clear_attached_tip_extension.assert_called_once_with()

    def test_tracked_drop_tip_failure_marks_reconciliation_required(self):
        from cubos.protocol_engine.commands.pipette import drop_tip

        ctx = _mock_context()
        ctx.campaign_id = 7
        ctx.fluid_state_id = 11
        ctx.data_store = MagicMock()
        ctx.data_store.begin_drop_tip.return_value = (True, "tips", "A1")
        _get_pipette(ctx).drop_tip.side_effect = RuntimeError("drop outcome unknown")

        with pytest.raises(RuntimeError, match="drop outcome unknown"):
            drop_tip(ctx, position="waste_1")

        ctx.data_store.mark_tip_reconciliation_required.assert_called_once()
        ctx.data_store.complete_drop_tip.assert_not_called()


# ─── transfer tests ──────────────────────────────────────────────────────────


def _mock_context_multi_resolve(has_pipette: bool = True) -> ProtocolContext:
    """Context where deck.resolve_coordinate returns different coords per position."""
    board = MagicMock()
    deck = MagicMock()

    coords = {
        "plate_1.A1": Coordinate3D(x=10.0, y=20.0, z=75.0),
        "plate_1.B1": Coordinate3D(x=10.0, y=28.0, z=75.0),
    }
    deck.resolve_coordinate.side_effect = (
        lambda pos: coords.get(pos, Coordinate3D(x=0.0, y=0.0, z=0.0))
)

    if has_pipette:
        pipette = MagicMock()
        # Real numeric heights so the dispatch finite-number guards pass;
        # test fixtures elsewhere set realistic values for engagement math.
        pipette.aspirate.return_value = MagicMock(success=True, volume_ul=100.0)
        pipette.dispense.return_value = MagicMock(success=True, volume_ul=100.0)
        board.instruments = {"pipette": pipette}
    else:
        board.instruments = {}

    return ProtocolContext(
        gantry=board,
        deck=deck,
        logger=logging.getLogger("test_pipette_commands"),
    )


class TestTransferCommand:

    def test_resolves_both_positions(self):
        from cubos.protocol_engine.commands.pipette import transfer

        ctx = _mock_context_multi_resolve()
        transfer(ctx, source="plate_1.A1", destination="plate_1.B1", volume_ul=100.0)

        ctx.deck.resolve_coordinate.assert_any_call("plate_1.A1")
        ctx.deck.resolve_coordinate.assert_any_call("plate_1.B1")
        assert ctx.deck.resolve_coordinate.call_count == 2

    def test_aspirates_from_source_then_dispenses_to_destination(self):
        from cubos.protocol_engine.commands.pipette import transfer

        ctx = _mock_context_multi_resolve()
        pip = ctx.gantry.instruments["pipette"]
        call_order = []
        ctx.gantry.move_to_labware.side_effect = lambda *a, **kw: call_order.append(("move", a[1]))
        pip.aspirate.side_effect = lambda *a: call_order.append("aspirate")
        pip.dispense.side_effect = lambda *a: call_order.append("dispense")

        transfer(ctx, source="plate_1.A1", destination="plate_1.B1", volume_ul=100.0)

        source_coord = Coordinate3D(x=10.0, y=20.0, z=75.0)
        dest_coord = Coordinate3D(x=10.0, y=28.0, z=75.0)
        assert call_order == [
            ("move", source_coord),
            "aspirate",
            ("move", dest_coord),
            "dispense",
        ]

    def test_passes_volume_and_speed(self):
        from cubos.protocol_engine.commands.pipette import transfer

        ctx = _mock_context_multi_resolve()
        pip = ctx.gantry.instruments["pipette"]

        transfer(ctx, source="plate_1.A1", destination="plate_1.B1", volume_ul=75.0, speed=25.0)

        pip.aspirate.assert_called_once_with(75.0, 25.0)
        pip.dispense.assert_called_once_with(75.0, 25.0)

    def test_default_speed(self):
        from cubos.protocol_engine.commands.pipette import transfer

        ctx = _mock_context_multi_resolve()
        pip = ctx.gantry.instruments["pipette"]

        transfer(ctx, source="plate_1.A1", destination="plate_1.B1", volume_ul=100.0)

        pip.aspirate.assert_called_once_with(100.0, 50.0)
        pip.dispense.assert_called_once_with(100.0, 50.0)

    def test_transfer_always_blows_out_with_speed_after_dispense(self):
        from cubos.protocol_engine.commands.pipette import transfer

        ctx = _mock_context_multi_resolve()
        pip = ctx.gantry.instruments["pipette"]
        call_order = []
        pip.aspirate.side_effect = lambda *a: call_order.append("aspirate")
        pip.dispense.side_effect = lambda *a: call_order.append("dispense")
        pip.blowout.side_effect = lambda *a: call_order.append("blowout")

        transfer(
            ctx, source="plate_1.A1", destination="plate_1.B1", volume_ul=100.0,
            speed=40.0,
        )

        pip.blowout.assert_called_once_with(40.0)
        assert call_order == ["aspirate", "dispense", "blowout"]

    def test_tracked_transfer_journals_before_liquid_and_commits_after_dispense(self):
        from cubos.protocol_engine.commands.pipette import transfer

        ctx = _mock_context_multi_resolve()
        ctx.campaign_id = 7
        ctx.fluid_state_id = 11
        events = []

        source_target = SimpleNamespace(labware_key="source", location_id=None)
        destination_target = SimpleNamespace(
            labware_key="holder.plate", location_id="A1",
        )
        ctx.deck.resolve_labware_target.side_effect = [
            source_target, destination_target,
        ]

        store = MagicMock()
        store.begin_fluid_transfer.side_effect = (
            lambda *args, **kwargs: events.append("begin") or True
        )
        store.complete_fluid_transfer.side_effect = (
            lambda operation_key: events.append("complete")
        )
        ctx.data_store = store
        ctx.gantry.move_to_labware.side_effect = (
            lambda *args, **kwargs: events.append("move")
        )
        pipette = ctx.gantry.instruments["pipette"]
        pipette.aspirate.side_effect = lambda *args: events.append("aspirate")
        pipette.dispense.side_effect = lambda *args: events.append("dispense")

        transfer(
            ctx,
            source="source",
            destination="holder.plate.A1",
            volume_ul=25.0,
        )

        assert events == [
            "begin", "move", "aspirate", "move", "dispense", "complete",
        ]
        args = store.begin_fluid_transfer.call_args.args
        assert args[0] == 11
        assert args[2:] == (
            "source", None, "holder.plate", "A1", 25.0,
        )
        store.record_transfer.assert_not_called()

    def test_tracked_transfer_skips_already_applied_operation(self):
        from cubos.protocol_engine.commands.pipette import transfer

        ctx = _mock_context_multi_resolve()
        ctx.campaign_id = 7
        ctx.fluid_state_id = 11
        ctx.deck.resolve_labware_target.side_effect = [
            SimpleNamespace(labware_key="source", location_id=None),
            SimpleNamespace(labware_key="plate", location_id="A1"),
        ]
        ctx.data_store = MagicMock()
        ctx.data_store.begin_fluid_transfer.return_value = False

        transfer(ctx, source="source", destination="plate.A1", volume_ul=25.0)

        pipette = ctx.gantry.instruments["pipette"]
        pipette.aspirate.assert_not_called()
        pipette.dispense.assert_not_called()
        ctx.data_store.complete_fluid_transfer.assert_not_called()

    def test_tracked_transfer_preflight_failure_stops_before_aspirate(self):
        from cubos.protocol_engine.commands.pipette import transfer

        ctx = _mock_context_multi_resolve()
        ctx.campaign_id = 7
        ctx.fluid_state_id = 11
        ctx.deck.resolve_labware_target.side_effect = [
            SimpleNamespace(labware_key="source", location_id=None),
            SimpleNamespace(labware_key="plate", location_id="A1"),
        ]
        ctx.data_store = MagicMock()
        ctx.data_store.begin_fluid_transfer.side_effect = ValueError(
            "source only has 5 uL"
        )

        with pytest.raises(ProtocolExecutionError, match="preflight failed"):
            transfer(
                ctx, source="source", destination="plate.A1", volume_ul=25.0,
            )

        ctx.gantry.instruments["pipette"].aspirate.assert_not_called()
        ctx.gantry.move_to_labware.assert_not_called()

    @pytest.mark.parametrize("missing", ["data_store", "campaign_id"])
    def test_incomplete_tracked_context_fails_before_motion(self, missing):
        from cubos.protocol_engine.commands.pipette import transfer

        ctx = _mock_context_multi_resolve()
        ctx.fluid_state_id = 11
        ctx.data_store = None if missing == "data_store" else MagicMock()
        ctx.campaign_id = None if missing == "campaign_id" else 7

        with pytest.raises(ProtocolExecutionError, match="context is incomplete"):
            transfer(ctx, source="source", destination="plate.A1", volume_ul=25.0)

        ctx.gantry.move_to_labware.assert_not_called()
        _get_pipette(ctx).aspirate.assert_not_called()

    def test_tracked_transfer_failure_marks_reconciliation_required(self):
        from cubos.protocol_engine.commands.pipette import transfer

        ctx = _mock_context_multi_resolve()
        ctx.campaign_id = 7
        ctx.fluid_state_id = 11
        ctx.deck.resolve_labware_target.side_effect = [
            SimpleNamespace(labware_key="source", location_id=None),
            SimpleNamespace(labware_key="plate", location_id="A1"),
        ]
        ctx.data_store = MagicMock()
        ctx.data_store.begin_fluid_transfer.return_value = True
        pipette = ctx.gantry.instruments["pipette"]
        pipette.dispense.side_effect = RuntimeError("dispense uncertain")

        with pytest.raises(RuntimeError, match="dispense uncertain"):
            transfer(
                ctx, source="source", destination="plate.A1", volume_ul=25.0,
            )

        ctx.data_store.mark_fluid_reconciliation_required.assert_called_once()
        ctx.data_store.complete_fluid_transfer.assert_not_called()

    def test_tracked_transfer_commit_failure_is_not_silently_ignored(self):
        from cubos.protocol_engine.commands.pipette import transfer

        ctx = _mock_context_multi_resolve()
        ctx.campaign_id = 7
        ctx.fluid_state_id = 11
        ctx.deck.resolve_labware_target.side_effect = [
            SimpleNamespace(labware_key="source", location_id=None),
            SimpleNamespace(labware_key="plate", location_id="A1"),
        ]
        ctx.data_store = MagicMock()
        ctx.data_store.begin_fluid_transfer.return_value = True
        ctx.data_store.complete_fluid_transfer.side_effect = RuntimeError(
            "database is locked"
        )

        with pytest.raises(ProtocolExecutionError, match="commit failed"):
            transfer(
                ctx, source="source", destination="plate.A1", volume_ul=25.0,
            )

        ctx.data_store.mark_fluid_reconciliation_required.assert_called_once()

    def test_raises_when_no_pipette(self):
        from cubos.protocol_engine.commands.pipette import transfer

        ctx = _mock_context_multi_resolve(has_pipette=False)
        with pytest.raises(ProtocolExecutionError, match="[Nn]o pipette"):
            transfer(ctx, source="plate_1.A1", destination="plate_1.B1", volume_ul=100.0)

    def test_transfer_updates_datastore_source_and_destination_volumes(self):
        from cubos.protocol_engine.commands.pipette import transfer

        source = Vial(
            name="A1",
            model_name="standard",
            height=10.0,
            diameter=5.0,
            location=Coordinate3D(x=0.0, y=0.0, z=0.0),
            capacity_ul=1000.0,
            working_volume_ul=800.0,
        )
        plate = WellPlate(
            name="plate_1",
            model_name="test",
            rows=1,
            columns=1,
            wells={"A1": Coordinate3D(x=10.0, y=20.0, z=75.0)},
            capacity_ul=200.0,
            working_volume_ul=150.0,
        )
        board = MagicMock()
        pipette = MagicMock()
        board.instruments = {"pipette": pipette}
        store = DataStore(db_path=":memory:")
        campaign_id = store.create_campaign(description="transfer")
        store.register_labware(campaign_id, "vial_1", source)
        store.register_labware(campaign_id, "plate_1", plate)
        store._conn.execute(
            "UPDATE labware SET current_volume_ul = 100.0 "
            "WHERE labware_key = 'vial_1'"
        )
        store._conn.commit()
        ctx = ProtocolContext(
            gantry=board,
            deck=Deck({"vial_1": source, "plate_1": plate}),
            data_store=store,
            campaign_id=campaign_id,
        )

        transfer(ctx, source="vial_1", destination="plate_1.A1", volume_ul=25.0)

        rows = dict(store._conn.execute(
            "SELECT labware_key || COALESCE('.' || well_id, ''), current_volume_ul "
            "FROM labware"
        ).fetchall())
        assert rows["vial_1"] == pytest.approx(75.0)
        assert rows["plate_1.A1"] == pytest.approx(25.0)
        store.close()

    def test_transfer_persists_vial_grid_aliases_to_canonical_rows(self):
        from cubos.protocol_engine.commands.pipette import transfer

        source = Vial(
            name="source",
            location=Coordinate3D(x=0.0, y=0.0, z=0.0),
            capacity_ul=1000.0,
            working_volume_ul=800.0,
        )
        destination = Vial(
            name="destination",
            location=Coordinate3D(x=10.0, y=0.0, z=0.0),
            capacity_ul=1000.0,
            working_volume_ul=800.0,
        )
        reagents = VialGrid(
            name="reagents",
            rows=1,
            columns=2,
            vials={"A1": source, "A2": destination},
            aliases={"buffer": "A1", "product": "A2"},
        )
        board = MagicMock()
        board.instruments = {"pipette": MagicMock()}
        store = DataStore(db_path=":memory:")
        campaign_id = store.create_campaign(description="grid alias transfer")
        store.register_labware(campaign_id, "reagents", reagents)
        store._conn.execute(
            "UPDATE labware SET current_volume_ul = 100.0 "
            "WHERE labware_key = 'reagents' AND well_id = 'A1'"
        )
        store._conn.commit()
        ctx = ProtocolContext(
            gantry=board,
            deck=Deck({"reagents": reagents}),
            data_store=store,
            campaign_id=campaign_id,
        )

        transfer(
            ctx,
            source="reagents.buffer",
            destination="reagents.product",
            volume_ul=25.0,
        )

        rows = dict(store._conn.execute(
            "SELECT well_id, current_volume_ul FROM labware "
            "WHERE labware_key = 'reagents'"
        ).fetchall())
        assert rows["A1"] == pytest.approx(75.0)
        assert rows["A2"] == pytest.approx(25.0)
        store.close()

    def test_transfer_persists_legacy_nested_vials_to_canonical_grid_rows(self):
        from cubos.protocol_engine.commands.pipette import transfer

        source = Vial(
            name="vial_1",
            location=Coordinate3D(x=0.0, y=0.0, z=0.0),
            capacity_ul=1000.0,
            working_volume_ul=800.0,
        )
        destination = Vial(
            name="A2",
            location=Coordinate3D(x=10.0, y=0.0, z=0.0),
            capacity_ul=1000.0,
            working_volume_ul=800.0,
        )
        holder = VialHolder(
            name="vial_holder",
            location=Coordinate3D(x=0.0, y=0.0, z=0.0),
            contained_labware={"A1": source, "A2": destination},
        )
        canonical_grid = VialGrid(
            name="vial_holder__vials",
            model_name=holder.model_name,
            vials={"A1": source, "A2": destination},
        )
        deck = Deck(
            {"vial_holder": holder},
            volume_labware={"vial_holder__vials": canonical_grid},
            target_aliases={
                "vial_holder.A1": "vial_holder__vials.A1",
                "vial_holder.A2": "vial_holder__vials.A2",
            },
        )
        board = MagicMock()
        board.instruments = {"pipette": MagicMock()}
        store = DataStore(db_path=":memory:")
        campaign_id = store.create_campaign(description="legacy vial transfer")
        store.register_labware(campaign_id, "vial_holder__vials", canonical_grid)
        store._conn.execute(
            "UPDATE labware SET current_volume_ul = 100.0 "
            "WHERE labware_key = 'vial_holder__vials' AND well_id = 'A1'"
        )
        store._conn.commit()
        ctx = ProtocolContext(
            gantry=board,
            deck=deck,
            data_store=store,
            campaign_id=campaign_id,
        )

        transfer(
            ctx,
            source="vial_holder.A1",
            destination="vial_holder.A2",
            volume_ul=25.0,
        )

        rows = dict(store._conn.execute(
            "SELECT well_id, current_volume_ul FROM labware "
            "WHERE labware_key = 'vial_holder__vials'"
        ).fetchall())
        assert rows["A1"] == pytest.approx(75.0)
        assert rows["A2"] == pytest.approx(25.0)
        store.close()


# ─── serial_transfer tests ───────────────────────────────────────────────────


def _make_2x3_plate() -> WellPlate:
    """2 rows (A-B) x 3 columns (1-3) plate for serial_transfer tests."""
    return WellPlate(
        name="plate_1",
        model_name="test_model",
        length=127.71,
        width=85.43,
        height=14.10,
        rows=2,
        columns=3,
        wells={
            "A1": Coordinate3D(x=0.0, y=0.0, z=75.0),
            "A2": Coordinate3D(x=10.0, y=0.0, z=75.0),
            "A3": Coordinate3D(x=20.0, y=0.0, z=75.0),
            "B1": Coordinate3D(x=0.0, y=8.0, z=75.0),
            "B2": Coordinate3D(x=10.0, y=8.0, z=75.0),
            "B3": Coordinate3D(x=20.0, y=8.0, z=75.0),
        },
        capacity_ul=200.0,
        working_volume_ul=150.0,
    )


def _serial_transfer_context(
    plate: WellPlate | None = None,
    has_pipette: bool = True,
) -> ProtocolContext:
    plate = plate or _make_2x3_plate()

    board = MagicMock()
    deck = MagicMock()
    deck.__getitem__ = MagicMock(return_value=plate)
    deck.resolve_labware = MagicMock(return_value=plate)
    deck.resolve_coordinate.side_effect = (
        lambda pos: Coordinate3D(x=0.0, y=0.0, z=0.0)
    )

    if has_pipette:
        pipette = MagicMock()
        pipette.aspirate.return_value = MagicMock(success=True)
        pipette.dispense.return_value = MagicMock(success=True)
        board.instruments = {"pipette": pipette}
    else:
        board.instruments = {}

    return ProtocolContext(
        gantry=board,
        deck=deck,
        logger=logging.getLogger("test_serial_transfer"),
    )


def _serial_transfer_nested_context() -> ProtocolContext:
    plate = _make_2x3_plate()
    holder = WellPlateHolder(
        name="plate_holder",
        location=Coordinate3D(x=0.0, y=0.0, z=0.0),
        contained_labware={"plate": plate},
    )
    source = Vial(
        name="vial_1",
        model_name="standard_vial",
        height=66.75,
        diameter=28.0,
        location=Coordinate3D(x=90.0, y=90.0, z=30.0),
        capacity_ul=1500.0,
        working_volume_ul=1200.0,
    )

    board = MagicMock()
    pipette = MagicMock()
    pipette.aspirate.return_value = MagicMock(success=True)
    pipette.dispense.return_value = MagicMock(success=True)
    board.instruments = {"pipette": pipette}

    return ProtocolContext(
        gantry=board,
        deck=Deck({"plate_holder": holder, "vial_1": source}),
        logger=logging.getLogger("test_serial_transfer_nested"),
    )


class TestSerialTransferCommand:

    def test_row_axis_transfers_to_each_well_in_order(self):
        from cubos.protocol_engine.commands.pipette import serial_transfer

        ctx = _serial_transfer_context()
        serial_transfer(
            ctx, source="vial_1", plate="plate_1", axis="A",
            volumes=[10.0, 20.0, 30.0],
        )

        pip = ctx.gantry.instruments["pipette"]
        assert pip.aspirate.call_count == 3
        assert pip.dispense.call_count == 3

        # Verify resolve was called with the right destination strings
        resolve_calls = [c.args[0] for c in ctx.deck.resolve_coordinate.call_args_list]
        # Each transfer resolves source + destination, so 6 calls total
        # Destinations should be plate_1.A1, plate_1.A2, plate_1.A3
        assert "plate_1.A1" in resolve_calls
        assert "plate_1.A2" in resolve_calls
        assert "plate_1.A3" in resolve_calls

    def test_nested_plate_path_transfers_to_each_well_in_order(self):
        from cubos.protocol_engine.commands.pipette import serial_transfer

        ctx = _serial_transfer_nested_context()
        serial_transfer(
            ctx, source="vial_1", plate="plate_holder.plate", axis="A",
            volumes=[10.0, 20.0, 30.0],
        )

        pip = ctx.gantry.instruments["pipette"]
        assert pip.aspirate.call_count == 3
        assert pip.dispense.call_count == 3

        move_targets = [
            call_args.args[1]
            for call_args in ctx.gantry.move_to_labware.call_args_list
        ]
        assert move_targets[1::2] == [
            Coordinate3D(x=0.0, y=0.0, z=75.0),
            Coordinate3D(x=10.0, y=0.0, z=75.0),
            Coordinate3D(x=20.0, y=0.0, z=75.0),
        ]

    def test_column_axis_transfers_to_each_well_in_order(self):
        from cubos.protocol_engine.commands.pipette import serial_transfer

        ctx = _serial_transfer_context()
        serial_transfer(
            ctx, source="vial_1", plate="plate_1", axis="2",
            volumes=[10.0, 20.0],
        )

        pip = ctx.gantry.instruments["pipette"]
        assert pip.aspirate.call_count == 2
        assert pip.dispense.call_count == 2

        resolve_calls = [c.args[0] for c in ctx.deck.resolve_coordinate.call_args_list]
        assert "plate_1.A2" in resolve_calls
        assert "plate_1.B2" in resolve_calls

    def test_explicit_volumes_passed_correctly(self):
        from cubos.protocol_engine.commands.pipette import serial_transfer

        ctx = _serial_transfer_context()
        serial_transfer(
            ctx, source="vial_1", plate="plate_1", axis="A",
            volumes=[10.0, 50.0, 100.0],
        )

        pip = ctx.gantry.instruments["pipette"]
        aspirate_volumes = [c.args[0] for c in pip.aspirate.call_args_list]
        dispense_volumes = [c.args[0] for c in pip.dispense.call_args_list]
        assert aspirate_volumes == [10.0, 50.0, 100.0]
        assert dispense_volumes == [10.0, 50.0, 100.0]

    def test_volume_range_linearly_spaced(self):
        from cubos.protocol_engine.commands.pipette import serial_transfer

        ctx = _serial_transfer_context()
        serial_transfer(
            ctx, source="vial_1", plate="plate_1", axis="A",
            volume_range=[10.0, 30.0],
        )

        pip = ctx.gantry.instruments["pipette"]
        aspirate_volumes = [c.args[0] for c in pip.aspirate.call_args_list]
        # 3 wells in row A, linspace(10, 30, 3) = [10.0, 20.0, 30.0]
        assert aspirate_volumes == pytest.approx([10.0, 20.0, 30.0])

    def test_volume_range_single_well_column(self):
        """volume_range with a 1-well axis uses the start value."""
        from cubos.protocol_engine.commands.pipette import serial_transfer

        # Make a 1x3 plate so column "1" has only 1 well (A1)
        plate = WellPlate(
            name="plate_1", model_name="t", length=100.0,
            width=80.0, height=10.0, rows=1, columns=3,
            wells={
                "A1": Coordinate3D(x=0.0, y=0.0, z=75.0),
                "A2": Coordinate3D(x=10.0, y=0.0, z=75.0),
                "A3": Coordinate3D(x=20.0, y=0.0, z=75.0),
            },
            capacity_ul=200.0, working_volume_ul=150.0,
        )
        ctx = _serial_transfer_context(plate=plate)
        serial_transfer(
            ctx, source="vial_1", plate="plate_1", axis="1",
            volume_range=[10.0, 100.0],
        )

        pip = ctx.gantry.instruments["pipette"]
        assert pip.aspirate.call_count == 1
        aspirate_volumes = [c.args[0] for c in pip.aspirate.call_args_list]
        assert aspirate_volumes == [10.0]

    def test_custom_speed_passed_through(self):
        from cubos.protocol_engine.commands.pipette import serial_transfer

        ctx = _serial_transfer_context()
        serial_transfer(
            ctx, source="vial_1", plate="plate_1", axis="A",
            volumes=[10.0, 20.0, 30.0], speed=25.0,
        )

        pip = ctx.gantry.instruments["pipette"]
        for c in pip.aspirate.call_args_list:
            assert c.args[1] == 25.0
        for c in pip.dispense.call_args_list:
            assert c.args[1] == 25.0

    def test_volumes_length_mismatch_raises(self):
        from cubos.protocol_engine.commands.pipette import serial_transfer

        ctx = _serial_transfer_context()
        with pytest.raises(ProtocolExecutionError, match="length"):
            serial_transfer(
                ctx, source="vial_1", plate="plate_1", axis="A",
                volumes=[10.0, 20.0],  # row A has 3 wells
            )

    def test_neither_volumes_nor_range_raises(self):
        from cubos.protocol_engine.commands.pipette import serial_transfer

        ctx = _serial_transfer_context()
        with pytest.raises(ProtocolExecutionError, match="volumes.*volume_range"):
            serial_transfer(
                ctx, source="vial_1", plate="plate_1", axis="A",
            )

    def test_both_volumes_and_range_raises(self):
        from cubos.protocol_engine.commands.pipette import serial_transfer

        ctx = _serial_transfer_context()
        with pytest.raises(ProtocolExecutionError, match="volumes.*volume_range"):
            serial_transfer(
                ctx, source="vial_1", plate="plate_1", axis="A",
                volumes=[10.0, 20.0, 30.0], volume_range=[10.0, 30.0],
            )

    def test_invalid_axis_raises(self):
        from cubos.protocol_engine.commands.pipette import serial_transfer

        ctx = _serial_transfer_context()
        with pytest.raises(ProtocolExecutionError, match="axis"):
            serial_transfer(
                ctx, source="vial_1", plate="plate_1", axis="Z",
                volumes=[10.0],
            )

    def test_raises_when_no_pipette(self):
        from cubos.protocol_engine.commands.pipette import serial_transfer

        ctx = _serial_transfer_context(has_pipette=False)
        with pytest.raises(ProtocolExecutionError, match="[Nn]o pipette"):
            serial_transfer(
                ctx, source="vial_1", plate="plate_1", axis="A",
                volumes=[10.0, 20.0, 30.0],
            )

    def test_validates_plate_is_wellplate(self):
        from cubos.protocol_engine.commands.pipette import serial_transfer

        ctx = _serial_transfer_context()
        ctx.deck.resolve_labware = MagicMock(return_value=MagicMock(spec=[]))

        with pytest.raises(ProtocolExecutionError, match="WellPlate"):
            serial_transfer(
                ctx, source="vial_1", plate="plate_1", axis="A",
                volumes=[10.0],
            )
