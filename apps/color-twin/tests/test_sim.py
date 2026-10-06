"""Offline behavior tests for genuine YAML execution and policy feedback."""
import copy
import math
import pytest
import yaml
from fastapi.testclient import TestClient
from sim.defaults import bundle, protocol_for, GANTRY, DECK, dump
from sim.runtime import execute, SimController, World, mixture
from sim.policy import suggest, campaign
from sim.server import app, Policy
from cubos.gantry.gantry_config import WorkingVolume

@pytest.fixture
def inputs(): return bundle()

def simulate(inputs, protocol=None, deck=None, gantry=None):
    return execute(gantry or inputs['gantry_yaml'],deck or inputs['deck_yaml'],protocol or inputs['protocol_yaml'])

def test_native_yaml_execution_conserves_stock_and_well_volume(inputs,monkeypatch):
    # Any accidental serial construction is a hard failure, even in loaders.
    import serial
    monkeypatch.setattr(serial,'Serial',lambda *a,**k: pytest.fail('Hardware access attempted'))
    result=simulate(inputs)
    assert result['fluids']['plate.A1']['parts']==[100]*3
    assert sum(x['volume'] for x in result['fluids'].values())==12000
    assert len([e for e in result['events'] if e['kind']=='capture'])==1
    assert any(e['kind']=='mix' for e in result['events'])
    assert len(result['points']['tips'])==96
    assert result['points']['tips'][-1]['y']==27
    assert result['events'][-1]['kind']=='capture'

def test_real_mount_offsets_and_tip_extension_reach_correct_tool_targets(inputs):
    r=simulate(inputs)
    tip_events=[e for e in r['events'] if e['kind']=='tip']
    assert tip_events[0]['extension']==25
    # First stock is at (35,150,45), aspiration offset -8, tip extension 25.
    assert any(e.get('end')==[35,150,62] for e in r['events'])
    # Camera aims plate A1 (30,90,15+40); offset=(-25,-28), depth=-18.
    assert [e for e in r['events'] if e['kind']=='move'][-1]['end']==[55,118,37]

def test_travel_waypoints_validate_before_partial_motion():
    w=World();c=SimController(w,WorkingVolume(0,290,0,180,0,100))
    c.move_to(30,50,10,travel_z=80)
    assert [e['end'] for e in w.events]==[[0,0,80],[30,0,80],[30,50,80],[30,50,10]]
    n=len(w.events)
    with pytest.raises(ValueError,match='exceeds'): c.move_to(40,60,5,travel_z=110)
    assert len(w.events)==n
    with pytest.raises(ValueError): c.move_to(math.nan,20,10)

@pytest.mark.parametrize('protocol',[
    {'protocol':[{'home':{}}]},
    {'protocol':[{'wait':{'seconds':9999}}]},
    {'protocol':[{'loop':{'count':1000000}}]},
    {'protocol':[]},
    {'protocol':[{'measure':{'instrument':'camera','position':'plate.A1','method':'unknown','measurement_height':40}}]},
])
def test_unsupported_protocols_reject(inputs,protocol):
    with pytest.raises(Exception): simulate(inputs,yaml.safe_dump(protocol))

def test_no_tip_and_double_pickup_reject(inputs):
    for steps in [
        [{'transfer':{'source':'stocks.A1','destination':'plate.A1','volume_ul':20}}],
        [{'pick_up_tip':{'position':'tips.A1'}},{'pick_up_tip':{'position':'tips.A2'}}],
    ]:
        with pytest.raises(Exception): simulate(inputs,dump({'protocol':steps}))

def test_tip_reuse_is_rejected_across_trials(inputs):
    first=yaml.safe_load(protocol_for([100]*3))['protocol']
    with pytest.raises(Exception): simulate(inputs,dump({'protocol':first+first}))

def test_edited_plate_capacity_is_enforced(inputs):
    deck=copy.deepcopy(DECK);deck['labware']['plate']['working_volume_ul']=50
    with pytest.raises(Exception): simulate(inputs,deck=dump(deck))

def test_stock_depletion_enforced(inputs):
    deck=copy.deepcopy(DECK);deck['labware']['stocks']['working_volume_ul']=20
    with pytest.raises(Exception): simulate(inputs,deck=dump(deck))

def test_factory_z_envelope_cannot_run_illustrative_setup(inputs):
    gantry=copy.deepcopy(GANTRY);gantry['working_volume']['z_max']=40;gantry['cnc']['safe_z']=35
    with pytest.raises(Exception): simulate(inputs,gantry=dump(gantry))

def test_invalid_yaml_and_unknown_geometry_reject(inputs):
    with pytest.raises(Exception): simulate(inputs,protocol='protocol: [broken:')
    deck=copy.deepcopy(DECK);deck['labware']['plate']['rows']=4
    with pytest.raises(ValueError,match='8 × 12'):simulate(inputs,deck=dump(deck))

def test_bo_reproducible_and_observations_drive_new_suggestions(inputs):
    policy=Policy(trials=8,target=mixture([60,80,160]))
    a=campaign(inputs['gantry_yaml'],inputs['deck_yaml'],policy)
    b=campaign(inputs['gantry_yaml'],inputs['deck_yaml'],policy)
    assert a['history']==b['history']
    assert min(h['loss'] for h in a['history'][4:])<min(h['loss'] for h in a['history'][:4])
    assert all(sum(h['volumes'])==300 and len(h['volumes'])==3 and all(50<=v<=200 and v%5==0 for v in h['volumes']) for h in a['history'])
    replay=simulate(inputs,protocol=a['protocol_yaml'])
    assert replay['fluids']==a['fluids']
    changed=copy.deepcopy(a['history'][:4]);changed[0]['loss']=0
    assert suggest(changed)[0]!=suggest(a['history'][:4])[0]

@pytest.mark.parametrize('acquisition',['ei','lcb','random'])
def test_policy_variants(inputs,acquisition):
    r=campaign(inputs['gantry_yaml'],inputs['deck_yaml'],Policy(trials=6,initial=2,acquisition=acquisition))
    assert len({tuple(h['volumes']) for h in r['history']})==6

def test_api_and_bad_policy(inputs):
    client=TestClient(app)
    payload={k:inputs[k] for k in ('gantry_yaml','deck_yaml','protocol_yaml')}
    assert client.get('/api/twin/defaults').status_code==200
    assert client.post('/api/twin/simulate',json=payload).status_code==200
    assert client.post('/api/twin/campaign',json={**payload,'policy':{'trials':4}}).status_code==200
    assert client.post('/api/twin/campaign',json={**payload,'policy':{'total':25,'trials':8}}).status_code==422
    assert client.post('/api/twin/simulate',json={**payload,'protocol_yaml':'bad'}).status_code==422
    assert client.post('/api/v1/gantry/connect',json={}).status_code in (404,405)


def test_three_color_defaults_and_fixed_total(inputs):
    assert [stock['name'] for stock in inputs['stocks']] == ['Red', 'Yellow', 'Blue']
    assert Policy().total == 300
    for invalid in (120, 299, 301):
        with pytest.raises(ValueError): Policy(total=invalid)
    assert yaml.safe_load(inputs['deck_yaml'])['labware']['plate']['working_volume_ul'] == 300

@pytest.mark.parametrize('volumes', [[200, 50, 50], [50, 200, 50], [50, 50, 200], [125, 125, 50]])
def test_large_color_transfers_split_at_pipette_capacity(inputs, volumes):
    result = simulate(inputs, protocol=protocol_for(volumes))
    assert result['fluids']['plate.A1']['volume'] == pytest.approx(300)
    assert result['fluids']['plate.A1']['parts'] == pytest.approx(volumes)
    strokes = [e['volume'] for e in result['events'] if e['kind'] == 'aspirate']
    assert all(5 <= v <= 120 for v in strokes)
    assert sum(strokes) == pytest.approx(300)
    assert len(strokes) > 3
    assert len([e for e in result['events'] if e['kind'] == 'tip' and e['attached']]) == 3

@pytest.mark.parametrize('volumes', [[49, 151, 100], [201, 50, 49], [50, 50, 50], [100, 100, 50, 50], [0, 150, 150]])
def test_recipe_generator_rejects_outside_experiment_constraints(volumes):
    with pytest.raises(ValueError): protocol_for(volumes)


def test_full_plate_budget_stops_before_consumable_exhaustion(inputs):
    result = campaign(inputs['gantry_yaml'], inputs['deck_yaml'], Policy(trials=96))
    assert result['requested_trials'] == 96
    assert 0 < len(result['history']) <= 32
    assert result['stop_reason']
    assert all(v['volume'] >= 0 for v in result['fluids'].values())
    replay = simulate(inputs, protocol=result['protocol_yaml'])
    assert replay['fluids'] == result['fluids']
