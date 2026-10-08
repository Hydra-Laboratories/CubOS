from unittest.mock import MagicMock

import pytest

from cubos.gantry.gantry import Gantry
from cubos.gantry.gantry_driver import driver
from cubos.gantry.gantry_driver.exceptions import MillConnectionError
from cubos.gantry.session import GantrySession, GantrySessionError


@pytest.fixture(autouse=True)
def native_loggers(monkeypatch):
    monkeypatch.setattr(driver, 'set_up_mill_logger', lambda path: MagicMock())
    monkeypatch.setattr(driver, 'set_up_command_logger', lambda path: MagicMock())


class CriticalController:
    def __init__(self, banner=True):
        self.is_open = True
        self.writes = []
        self.banner = banner
        self.reset = False

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(data)
        self.reset = data == b'\x18'

    def readline(self):
        if self.reset and self.banner:
            self.banner = False
            return b"Grbl 1.1h ['$' for help]\r\n"
        return b''

    def close(self):
        self.is_open = False


def test_explicit_reset_reaches_silent_critical_controller_and_closes(monkeypatch):
    controller = CriticalController()
    constructor = MagicMock(return_value=controller)
    monkeypatch.setattr(driver.serial, 'Serial', constructor)
    mill = driver.Mill()
    mill.homed = True
    assert mill.recover_critical_alarm('/dev/controller').startswith('Grbl ')
    constructor.assert_called_once_with(port='/dev/controller', baudrate=115200, timeout=0.1, write_timeout=0.5, exclusive=True)
    assert controller.writes == [b'\x18']
    assert not controller.is_open
    assert mill.ser_mill is None and not mill.active_connection and not mill.homed
    assert mill._wco is None


def test_recovery_timeout_closes_port(monkeypatch):
    controller = CriticalController(banner=False)
    monkeypatch.setattr(driver.serial, 'Serial', lambda **kwargs: controller)
    with pytest.raises(MillConnectionError, match='unverified'):
        driver.Mill().recover_critical_alarm('/dev/controller', timeout=0.001)
    assert controller.writes == [b'\x18']
    assert not controller.is_open


@pytest.mark.parametrize('owned', [False, True])
def test_driver_rejects_missing_port_or_owner_before_open(monkeypatch, owned):
    constructor = MagicMock()
    monkeypatch.setattr(driver.serial, 'Serial', constructor)
    mill = driver.Mill()
    mill.active_connection = owned
    with pytest.raises(MillConnectionError):
        mill.recover_critical_alarm('/dev/controller' if owned else '')
    constructor.assert_not_called()


def test_normal_connect_never_resets_silent_controller(monkeypatch):
    controller = CriticalController()
    mill = driver.Mill()
    monkeypatch.setattr(mill, '_locate_over_serial', lambda port: (controller, port))
    monkeypatch.setattr(mill, '_read_serial', lambda: '')
    monkeypatch.setattr(driver.time, 'sleep', lambda seconds: None)
    with pytest.raises(MillConnectionError, match='No initial GRBL status'):
        mill.connect('/dev/controller')
    assert controller.writes == [b'?', b'?', b'?']


def test_session_recovery_delegates_and_stays_disconnected(monkeypatch):
    staged = MagicMock()
    staged.recover_critical_alarm.return_value = "Grbl 1.1h"
    factory = MagicMock(return_value=staged)
    session = GantrySession(gantry_factory=factory)
    monkeypatch.setattr(session, '_load_config_from_yaml', lambda path: {'serial_port': '/dev/controller'})
    snapshot = session.recover_critical_alarm('gantry.yaml')
    staged.recover_critical_alarm.assert_called_once_with('/dev/controller')
    staged.connect.assert_not_called()
    assert not session.connected and not snapshot.connected
    assert 'Grbl 1.1h' in snapshot.status
    assert 'G92' in snapshot.calibration_warning and 'WCO' in snapshot.calibration_warning


@pytest.mark.parametrize('owned', [False, True])
def test_session_recovery_rejects_busy_or_owned_session(owned):
    session = GantrySession()
    if owned:
        session._gantry = MagicMock()
    else:
        session.operation_lock.acquire()
    try:
        with pytest.raises(GantrySessionError):
            session.recover_critical_alarm('unused.yaml')
    finally:
        if not owned:
            session.operation_lock.release()


def test_public_gantry_recovery_delegates_and_propagates():
    gantry = Gantry.__new__(Gantry)
    gantry._offline = False
    gantry._mill = MagicMock()
    gantry._mill.recover_critical_alarm.return_value = 'Grbl 1.1h'
    assert gantry.recover_critical_alarm('/dev/controller') == 'Grbl 1.1h'
    failure = MillConnectionError('timeout')
    gantry._mill.recover_critical_alarm.side_effect = failure
    with pytest.raises(MillConnectionError) as caught:
        gantry.recover_critical_alarm('/dev/controller')
    assert caught.value is failure


@pytest.mark.parametrize('failure_at', ['write', 'reset_input_buffer'])
def test_serial_failure_closes_port(monkeypatch, failure_at):
    controller = CriticalController()
    failure = OSError('serial failure')
    setattr(controller, failure_at, MagicMock(side_effect=failure))
    monkeypatch.setattr(driver.serial, 'Serial', lambda **kwargs: controller)
    mill = driver.Mill()
    with pytest.raises(OSError) as caught:
        mill.recover_critical_alarm('/dev/controller')
    assert caught.value is failure
    assert not controller.is_open and mill.ser_mill is None


def test_invalid_banner_does_not_verify_recovery(monkeypatch):
    controller = CriticalController()
    controller.readline = lambda: b'Grbl impostor banner\n'
    monkeypatch.setattr(driver.serial, 'Serial', lambda **kwargs: controller)
    with pytest.raises(MillConnectionError, match='unverified'):
        driver.Mill().recover_critical_alarm('/dev/controller', timeout=0.001)
    assert not controller.is_open


def test_recovery_uses_selected_yaml_port(tmp_path):
    from pathlib import Path
    import yaml
    source = Path(__file__).parents[1] / 'fixtures/configs/gantry/mock_panda.yaml'
    config = yaml.safe_load(source.read_text())
    config['serial_port'] = '/dev/serial/by-id/1a86-controller'
    selected = tmp_path / 'gantry.yaml'
    selected.write_text(yaml.safe_dump(config))
    staged = MagicMock()
    session = GantrySession(gantry_factory=lambda **kwargs: staged)
    session.recover_critical_alarm(selected)
    staged.recover_critical_alarm.assert_called_once_with('/dev/serial/by-id/1a86-controller')
