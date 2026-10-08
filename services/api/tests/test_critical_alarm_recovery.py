from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock
import threading

import pytest
from fastapi import HTTPException
from cubos.gantry.session import GantryPositionSnapshot
from cubos_api.routers import gantry
from cubos_api.services.run_manager import RunConflictError
from cubos_api.services import run_manager


@pytest.fixture
def recovery_setup(monkeypatch):
    session = MagicMock()
    session.connected = False
    session.calibration_active = False
    session.recover_critical_alarm.return_value = GantryPositionSnapshot(connected=False)
    monkeypatch.setattr(gantry, '_get_or_create_session', lambda: session)
    monkeypatch.setattr(gantry, '_selected_gantry_path', lambda name: (name, 'selected.yaml'))
    manager = SimpleNamespace(inventory_edit=lambda: nullcontext())
    monkeypatch.setattr(run_manager, 'get_run_manager', lambda: manager)
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
        manager.inventory_edit = busy
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


def test_session_busy_is_conflict(recovery_setup):
    from cubos.gantry.session import GantrySessionError
    session, _ = recovery_setup
    session.recover_critical_alarm.side_effect = GantrySessionError('operation busy')
    with pytest.raises(HTTPException) as caught:
        gantry.recover_critical_alarm(gantry.ConnectRequest(filename='cub.yaml'))
    assert caught.value.status_code == 409


def test_recovery_guard_excludes_new_reservation_until_exit(tmp_path):
    from cubos_api.config import CubOSSettings
    runs = run_manager.RunManager(CubOSSettings(config_dir=tmp_path / "configs", run_dir=tmp_path / "runs"))
    attempted = threading.Event()
    completed = threading.Event()
    def reserve():
        attempted.set()
        runs.reserve_station("new-client")
        completed.set()
    with runs.inventory_edit():
        worker = threading.Thread(target=reserve)
        worker.start()
        assert attempted.wait(1)
        assert not completed.wait(0.02)
    worker.join(1)
    assert completed.is_set() and runs.reservation_owner == "new-client"
