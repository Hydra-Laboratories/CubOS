"""End-to-end campaign loop using native protocol handlers and offline instruments."""
import json
import shutil
import time
from pathlib import Path

from cubos_api.config import CubOSSettings
from cubos_api.models.campaigns import CampaignSpec
from cubos_api.services.campaign_manager import CampaignManager
from cubos_api.services.run_manager import RunManager


def test_native_mock_campaign_stops_from_observations_without_hardware(tmp_path, monkeypatch):
    import serial
    monkeypatch.setattr(serial, 'Serial', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('Hardware access')))
    source = Path(__file__).resolve().parents[3] / 'examples/active-learning'
    configs = tmp_path/'configs'
    for category in ('gantry','deck','protocol'):
        path=configs/category
        path.mkdir(parents=True)
        shutil.copy2(source/f'{category}.yaml',path/f'bo_demo_{category}.yaml')
    settings=CubOSSettings(config_dir=configs,run_dir=tmp_path/'runs',data_db_path=tmp_path/'data.db')
    spec=CampaignSpec.model_validate(json.loads((source/'campaign.json').read_text()))
    native=RunManager(settings)
    campaigns=CampaignManager(settings,native,poll_interval=.005)
    assert campaigns.validate(spec)['valid']
    record=campaigns.start(spec)
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        record=campaigns.get(record.campaign_id)
        if record.state in {'completed','failed','stopped'}:break
        time.sleep(.005)
    assert record.state=='completed',record.error
    assert record.stop_reason=='no_improvement'
    assert [t.objective for t in record.trials]==[150.0]*3
    assert len({tuple(t.parameters.items()) for t in record.trials})==3
    for trial in record.trials:
        run=native.get(trial.run_id)
        assert run.state=='succeeded' and run.mock_mode
        assert run.metadata['active_learning_campaign_id']==record.campaign_id
        assert 'result.json' in run.artifacts
    assert native.campaign_owner is None
