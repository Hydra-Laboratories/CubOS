from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock
import threading

import pytest
from fastapi import HTTPException
from cubos.gantry.session import GantryPositionSnapshot
from cubos_api.routers import gantry
from cubos_api.services import overnight_queue
from cubos_api.services.campaign_manager import CampaignManager
from cubos_api.services.run_manager import RunConflictError


@pytest.fixture
def recovery_setup(monkeypatch):
    session = MagicMock()
    session.connected = False
    session.calibration_active = False
    session.recover_critical_alarm.return_value = GantryPositionSnapshot(connected=False)
    monkeypatch.setattr(gantry, '_get_or_create_session', lambda: session)
    monkeypatch.setattr(gantry, '_selected_gantry_path', lambda name: (name, 'selected.yaml'))
    manager = SimpleNamespace(controller_recovery=lambda: nullcontext())
    monkeypatch.setattr(overnight_queue, 'get_overnight_queue_manager', lambda: manager)
    monkeypatch.setitem(gantry._run_state, 'active', False)
    return session, manager


def test_api_reset_only_delegates(recovery_setup):
    session, _ = recovery_setup
    assert not gantry.recover_critical_alarm(gantry.ConnectRequest(filename='cub.yaml')).connected
    session.recover_critical_alarm.assert_called_once_with('selected.yaml')
    session.connect.assert_not_called()
    session.unlock.assert_not_called()


@pytest.mark.parametrize('reason', ['run', 'session', 'campaign'])
def test_api_busy_guards_before_reset(recovery_setup, monkeypatch, reason):
    session, manager = recovery_setup
    if reason == 'run':
        monkeypatch.setitem(gantry._run_state, 'active', True)
    elif reason == 'session':
        session.connected = True
    else:
        @contextmanager
        def busy():
            raise RunConflictError('campaign owns station')
            yield
        manager.controller_recovery = busy
    with pytest.raises(HTTPException) as caught:
        gantry.recover_critical_alarm(gantry.ConnectRequest(filename='cub.yaml'))
    assert caught.value.status_code == 409
    session.recover_critical_alarm.assert_not_called()


def test_api_requires_filename(recovery_setup):
    session, _ = recovery_setup
    with pytest.raises(HTTPException) as caught:
        gantry.recover_critical_alarm(gantry.ConnectRequest())
    assert caught.value.status_code == 400
    session.recover_critical_alarm.assert_not_called()


@pytest.mark.parametrize('state', ['running', 'paused', 'awaiting_refill'])
def test_nonterminal_campaign_blocks_between_batches(state):
    manager = CampaignManager.__new__(CampaignManager)
    manager._lock = threading.RLock()
    manager._records = {'campaign': SimpleNamespace(state=state)}
    manager.runs = MagicMock()
    with pytest.raises(RunConflictError):
        with manager.controller_recovery():
            pytest.fail('recovery should not be entered')
    manager.runs.inventory_edit.assert_not_called()


@pytest.mark.parametrize('state, blocked', [('prepared', False), ('running', True), ('completed', False)])
def test_queue_guard_keeps_prepared_queue_and_blocks_running(tmp_path, state, blocked):
    manager = overnight_queue.OvernightQueueManager.__new__(overnight_queue.OvernightQueueManager)
    manager.base = tmp_path
    manager._lock = threading.RLock()
    manager.campaigns = SimpleNamespace(controller_recovery=lambda: nullcontext())
    (tmp_path / 'job').mkdir()
    (tmp_path / 'job' / 'queue.json').write_text('placeholder')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(overnight_queue.OvernightQueueRecord, 'model_validate_json', lambda content: SimpleNamespace(state=state))
        if blocked:
            with pytest.raises(RunConflictError):
                with manager.controller_recovery():
                    pytest.fail('running queue must block')
        else:
            with manager.controller_recovery():
                pass
    assert (tmp_path / 'job' / 'queue.json').read_text() == 'placeholder'


def test_session_busy_is_conflict(recovery_setup):
    from cubos.gantry.session import GantrySessionError
    session, _ = recovery_setup
    session.recover_critical_alarm.side_effect = GantrySessionError('operation busy')
    with pytest.raises(HTTPException) as caught:
        gantry.recover_critical_alarm(gantry.ConnectRequest(filename='cub.yaml'))
    assert caught.value.status_code == 409


def test_recovery_guard_excludes_campaign_reservation_until_exit():
    from cubos_api.services.run_manager import RunManager
    runs = RunManager.__new__(RunManager)
    runs._lock = threading.Lock()
    runs._active_run_id = None
    runs._campaign_owner = None
    campaigns = CampaignManager.__new__(CampaignManager)
    campaigns._lock = threading.RLock()
    campaigns._records = {}
    campaigns.runs = runs
    attempted = threading.Event()
    completed = threading.Event()

    def reserve():
        attempted.set()
        runs.reserve_campaign('new-campaign')
        completed.set()

    with campaigns.controller_recovery():
        worker = threading.Thread(target=reserve)
        worker.start()
        assert attempted.wait(1)
        assert not completed.wait(0.02)
    worker.join(1)
    assert completed.is_set() and runs.campaign_owner == 'new-campaign'
