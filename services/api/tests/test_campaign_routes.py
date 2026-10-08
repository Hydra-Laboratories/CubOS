import pytest
from tests.api_client import api_request
from cubos_api.app import create_app
from cubos_api.models.campaigns import Observation
from cubos_api.models.runs import RunSubmission
from cubos_api.services.run_manager import get_run_manager, RunConflictError


def test_station_reservation_blocks_external_submissions_and_setup_changes():
    manager = get_run_manager()
    manager.reserve_campaign('owner')
    try:
        with pytest.raises(RunConflictError, match='reserved'):
            manager.submit(RunSubmission(gantry_config='g',deck_config='d',protocol_yaml='p',mock_mode=True))
        app=create_app()
        assert api_request(app,'PUT','/api/v1/deck/new.yaml',json={'labware':{}}).status_code==409
        assert api_request(app,'POST','/api/v1/gantry/jog',json={'x':1}).status_code==409
        assert api_request(app,'POST','/api/v1/runs',json={}).status_code==409
        assert api_request(app,'GET','/api/v1/health').status_code==200
        assert api_request(app,'GET','/api/v1/campaigns').status_code==200
    finally:
        manager.release_campaign('owner')
    assert manager.campaign_owner is None


@pytest.mark.parametrize('value',[True, '1', float('nan'), float('inf')])
def test_manual_observation_rejects_invalid_types(value):
    with pytest.raises(ValueError): Observation(value=value)


def test_campaign_routes_report_missing_records():
    app=create_app()
    assert api_request(app,'GET','/api/v1/campaigns/not-found').status_code==404
    assert api_request(app,'POST','/api/v1/campaigns/not-found/stop',json={}).status_code==404
    assert api_request(app,'POST','/api/v1/campaigns/not-found/observation',json={'value':1}).status_code==404
