from unittest.mock import MagicMock
import pytest
from fastapi import HTTPException
from cubos.gantry.session import GantrySessionError
from cubos_api.routers import gantry


@pytest.mark.parametrize('busy', ['run', 'session', None])
def test_frame_query_conflicts_or_delegates(monkeypatch, busy):
    session = MagicMock()
    session.coordinate_frame.return_value = {'raw_status': '<Alarm|MPos:1,2,3>', 'work_coordinate_offset': None}
    monkeypatch.setattr(gantry, '_require_session', lambda: session)
    monkeypatch.setitem(gantry._run_state, 'active', busy == 'run')
    if busy == 'session':
        session.coordinate_frame.side_effect = GantrySessionError('busy')
    if busy:
        with pytest.raises(HTTPException) as caught:
            gantry.get_coordinate_frame()
        assert caught.value.status_code == 409
        if busy == 'run':
            session.coordinate_frame.assert_not_called()
    else:
        assert gantry.get_coordinate_frame() == session.coordinate_frame.return_value
