"""Side-exit motion must preserve physical lift across tool-depth changes."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from pydantic import ValidationError
from cubos.deck.deck import Deck
from cubos.deck.labware.tip_rack import TipRack, SideExit
from cubos.gantry.gantry_config import GantryConfig, WorkingVolume
from cubos.gantry.instrument_mount import InstrumentedGantry
from cubos.gantry.offline import OfflineGantry
from cubos.gantry.errors import MillConnectionError
from cubos.protocol_engine.runtime import ProtocolContext
from cubos.protocol_engine.commands.pipette import pick_up_tip
from cubos.protocol_engine.errors import ProtocolExecutionError
from cubos.validation.bounds import collect_protocol_motion_targets, validate_protocol_motion_bounds
from cubos.validation.protocol_semantics import validate_protocol_semantics

class Pipette:
    name = 'pipette'
    offset_x, offset_y, depth = 7., -3., -70.
    attached_tip_extension = 0.
    pick_up_tip = Mock()
    @property
    def effective_depth(self): return self.depth + self.attached_tip_extension
    def set_attached_tip_extension(self, value): self.attached_tip_extension = value


def setup(*, exit_x=280, lift=30):
    rack = TipRack(name='tips', rows=1, columns=2, pickup_z=70, tip_length=70,
        tips={'A1': {'x': 158, 'y': 90, 'z': 70}, 'A2': {'x': 167, 'y': 90, 'z': 70}},
        side_exit={'lift_mm': lift, 'exit_x': exit_x})
    pipette = Pipette()
    pipette.pick_up_tip = Mock()
    controller = OfflineGantry()
    controller.move_to = Mock(wraps=controller.move_to)
    config = GantryConfig(serial_port='SIMULATION_ONLY', gantry_type='cub', factory_z_travel_mm=56,
        safe_z=56, working_volume=WorkingVolume(0,290,0,180,0,56))
    gantry = InstrumentedGantry(controller, {'pipette': pipette}, safe_z=56)
    return ProtocolContext(gantry=gantry, deck=Deck({'tips': rack}), gantry_config=config), rack, pipette


def protocol(position='tips.A2'):
    return SimpleNamespace(steps=[SimpleNamespace(command_name='pick_up_tip', args={'position':position}, index=0)])


def test_physical_lift_is_30_then_x_only_and_tip_is_consumed():
    ctx, rack, pipette = setup()
    pick_up_tip(ctx, 'tips.A2')
    calls = ctx.gantry.controller.move_to.call_args_list
    assert [c.args for c in calls] == [(160,93,30),(160,93,0),(160,93,30),(273,93,30)]
    assert calls[0].kwargs['travel_z'] == 30
    assert all(c.kwargs['travel_z'] is None for c in calls[1:3])
    assert calls[-1].kwargs['travel_z'] == 30
    assert pipette.attached_tip_extension == 70
    assert not rack.is_tip_present('A2')
    assert rack.next_available_tip() == 'A1'


def test_bounds_and_semantics_match_runtime_and_do_not_use_bare_safe_z():
    ctx, rack, _ = setup()
    assert not validate_protocol_motion_bounds(ctx.gantry_config, protocol(), ctx.deck, ctx.gantry)
    assert not validate_protocol_semantics(protocol(), ctx.gantry, ctx.deck, ctx.gantry_config)
    assert [t.position_id for t in collect_protocol_motion_targets(ctx.gantry_config,protocol(),ctx.deck)] == ['A2.approach','A2.engage','A2.lift','A2.exit']
    assert rack.is_tip_present('A2')  # validation has no deck side effects

@pytest.mark.parametrize('changes', [{'exit_x':310}, {'lift':57}])
def test_invalid_path_rejected_before_any_motion(changes):
    ctx, _, pipette = setup(**changes)
    assert validate_protocol_motion_bounds(ctx.gantry_config,protocol(),ctx.deck,ctx.gantry)
    assert validate_protocol_semantics(protocol(),ctx.gantry,ctx.deck,ctx.gantry_config)
    with pytest.raises(ProtocolExecutionError, match='bounds'): pick_up_tip(ctx,'tips.A2')
    ctx.gantry.controller.move_to.assert_not_called()
    pipette.pick_up_tip.assert_not_called()


def test_loaded_tip_in_exit_lane_rejected_and_auto_selection_uses_open_edge():
    ctx, _, _ = setup()
    assert validate_protocol_semantics(protocol('tips.A1'),ctx.gantry,ctx.deck,ctx.gantry_config)
    with pytest.raises(ProtocolExecutionError,match='blocked'): pick_up_tip(ctx,'tips.A1')
    ctx.gantry.controller.move_to.assert_not_called()
    pick_up_tip(ctx,'tips')
    assert ctx.deck['tips'].is_tip_present('A1')
    assert not ctx.deck['tips'].is_tip_present('A2')

@pytest.mark.parametrize('fail_at', [2,3])
@pytest.mark.parametrize('error_type', [RuntimeError, MillConnectionError])
def test_withdrawal_failure_retains_attached_consumed_state(fail_at, error_type):
    ctx, rack, pipette = setup()
    error = error_type('controller unavailable')
    ctx.gantry.controller.move_to.side_effect = [None]*fail_at + [error]
    with pytest.raises(error_type,match='unavailable') as caught: pick_up_tip(ctx,'tips.A2')
    assert caught.value is error
    assert pipette.attached_tip_extension == 70
    assert not rack.is_tip_present('A2')
    assert ctx.gantry.controller.move_to.call_count == fail_at+1

@pytest.mark.parametrize('data', [{'lift_mm':29,'exit_x':280},{'lift_mm':30,'exit_x':float('nan')},{'lift_mm':float('inf'),'exit_x':280},{'lift_mm':30,'exit_x':280,'axis':'y'}])
def test_invalid_side_exit_schema(data):
    with pytest.raises(ValidationError): SideExit(**data)


def test_exit_inside_rack_rejected():
    with pytest.raises(ValidationError,match='beyond'): setup(exit_x=160)


def test_negative_x_exit_has_same_vertical_lift():
    ctx, rack, _ = setup(exit_x=140)
    pick_up_tip(ctx,'tips.A1')
    assert ctx.gantry.controller.move_to.call_args.args == (133,93,30)


def test_missing_config_and_already_attached_fail_before_motion():
    ctx, _, pipette = setup()
    ctx.gantry_config = None
    with pytest.raises(ProtocolExecutionError,match='gantry_config'): pick_up_tip(ctx,'tips.A2')
    pipette.attached_tip_extension = 70
    with pytest.raises(ProtocolExecutionError,match='bare'): pick_up_tip(ctx,'tips.A2')
    ctx.gantry.controller.move_to.assert_not_called()


def test_durable_pickup_commits_after_exit_and_marks_failed_exit_uncertain():
    for fail in (False, True):
        ctx, rack, pipette = setup()
        ctx.fluid_state_id, ctx.campaign_id = 1, 2
        ctx.active_step_index, ctx.active_step_command = 0, 'pick_up_tip'
        ctx.data_store = Mock()
        ctx.data_store.begin_pick_up_tip.return_value = (True, 'A2', 70)
        if fail:
            ctx.gantry.controller.move_to.side_effect = [None,None,None,MillConnectionError('exit failed')]
            with pytest.raises(MillConnectionError): pick_up_tip(ctx,'tips.A2')
            ctx.data_store.complete_pick_up_tip.assert_not_called()
            ctx.data_store.mark_tip_reconciliation_required.assert_called_once()
        else:
            def commit(_):
                assert ctx.gantry.controller.get_coordinates() == {'x':273,'y':93,'z':30}
            ctx.data_store.complete_pick_up_tip.side_effect = commit
            pick_up_tip(ctx,'tips.A2')
            ctx.data_store.complete_pick_up_tip.assert_called_once()
            ctx.data_store.mark_tip_reconciliation_required.assert_not_called()
        assert not rack.is_tip_present('A2')
        assert pipette.attached_tip_extension == 70


def test_tracked_side_exit_requires_explicit_slot_before_journaling():
    ctx, _, _ = setup()
    ctx.fluid_state_id, ctx.campaign_id = 1, 2
    ctx.data_store = Mock()
    with pytest.raises(ProtocolExecutionError,match='explicit tip slot'): pick_up_tip(ctx,'tips')
    ctx.data_store.begin_pick_up_tip.assert_not_called()
    ctx.gantry.controller.move_to.assert_not_called()


def test_unresolved_targets_are_reported_without_side_exit_attribute_errors():
    ctx, _, _ = setup()
    assert validate_protocol_semantics(protocol('missing.A1'),ctx.gantry,ctx.deck,ctx.gantry_config)
    assert collect_protocol_motion_targets(ctx.gantry_config,protocol('missing.A1'),ctx.deck)==[]
