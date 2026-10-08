from unittest.mock import MagicMock

import pytest
from cubos.gantry.coordinates import Coordinates
from cubos.gantry.gantry import Gantry
from cubos.gantry.gantry_driver import driver
from cubos.gantry.gantry_driver.exceptions import MillConnectionError
from cubos.gantry.session import GantrySession, GantrySessionError


@pytest.fixture
def mill(monkeypatch):
    monkeypatch.setattr(driver, 'set_up_mill_logger', lambda path: MagicMock())
    monkeypatch.setattr(driver, 'set_up_command_logger', lambda path: MagicMock())
    monkeypatch.setattr(driver.time, 'sleep', lambda seconds: None)
    value = driver.Mill()
    value.ser_mill = MagicMock()
    value.ser_mill.is_open = True
    value._wco = Coordinates(100, 100, 100)
    return value


@pytest.mark.parametrize('raw, machine, work, offset', [
    ('<Idle|MPos:-1,-2,-3|WCO:10,20,30>', (-1, -2, -3), None, (10, 20, 30)),
    ('<Alarm|WPos:1,2,3>', None, (1, 2, 3), None),
    ('<Alarm|MPos:1,2,3>', (1, 2, 3), None, None),
    ('<Idle|MPos:-1,-2,-3|WPos:1,2,3|WCO:-2,-4,-6>', (-1, -2, -3), (1, 2, 3), (-2, -4, -6)),
    ('<Idle|WPos:nan,2,3|WCO:1,2|MPos:bad,2,3>', None, None, None),
])
def test_fresh_reported_coordinates_never_use_cached_offset(mill, monkeypatch, raw, machine, work, offset):
    monkeypatch.setattr(mill, '_read_serial', lambda: raw)
    report = mill.coordinate_frame()
    assert report['raw_status'] == raw
    assert report['fresh'] and report['source'] == 'fresh_grbl_status_query'
    assert isinstance(report['observed_at'], float)
    for key, expected in [('machine_position', machine), ('work_position', work), ('work_coordinate_offset', offset)]:
        assert report[key] == (dict(zip(('x', 'y', 'z'), expected)) if expected else None)
    mill.ser_mill.reset_input_buffer.assert_called_once_with()
    mill.ser_mill.write.assert_called_once_with(b'?')
    assert tuple(mill._wco) == (100, 100, 100)


def test_silent_query_does_not_claim_fresh_frame(mill, monkeypatch):
    monkeypatch.setattr(mill, '_read_serial', lambda: '')
    with pytest.raises(MillConnectionError, match='No fresh'):
        mill.coordinate_frame()


def test_session_busy_query_never_reads_serial():
    session = GantrySession()
    session._gantry = MagicMock()
    session.operation_lock.acquire()
    try:
        with pytest.raises(GantrySessionError, match='busy'):
            session.coordinate_frame()
        session._gantry.coordinate_frame.assert_not_called()
    finally:
        session.operation_lock.release()


def test_public_wrappers_delegate_alarm_report():
    gantry = Gantry.__new__(Gantry)
    gantry._offline = False
    gantry._mill = MagicMock()
    report = {'raw_status': '<Alarm|MPos:1,2,3>'}
    gantry._mill.coordinate_frame.return_value = report
    session = GantrySession()
    session._gantry = gantry
    assert session.coordinate_frame() is report
    gantry._mill.coordinate_frame.assert_called_once_with()
