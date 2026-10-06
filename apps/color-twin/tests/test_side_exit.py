import json
import pytest
import yaml
from sim.side_exit import bundle, protocol
from sim.runtime import execute
from sim.server import app, Policy
from sim.policy import campaign
from fastapi.testclient import TestClient


def run(b): return execute(b['gantry_yaml'],b['deck_yaml'],b['protocol_yaml'])


def test_side_exit_native_color_protocol_no_hardware(monkeypatch):
    import serial
    monkeypatch.setattr(serial,'Serial',lambda *a,**k:pytest.fail('Hardware access attempted'))
    r=run(bundle())
    assert r['steps']==14
    assert r['fluids']['plate.A1']['parts']==[100,100,100]
    assert sum(x['volume'] for x in r['fluids'].values())==12000
    assert [e['target'] for e in r['events'] if e['kind']=='tip' and e['attached']]==['tips.A12','tips.A11','tips.A10']
    for step in (0,4,8):
        moves=[e for e in r['events'] if e['kind']=='move' and e['step']==step]
        lift,exit=moves[-2:]
        assert lift['end'][2]-lift['start'][2]==30
        assert lift['end'][:2]==lift['start'][:2]
        assert exit['end'][0]==140
        assert exit['end'][0]<exit['start'][0]
        assert exit['end'][1:]==exit['start'][1:]
    assert all(0<=e['end'][2]<=56 for e in r['events'] if e['kind']=='move')
    assert all(sum(a!=b for a,b in zip(e['start'],e['end']))==1 for e in r['events'] if e['kind']=='move')
    assert all(e['target']=='waste' for e in r['events'] if e['kind']=='tip' and not e['attached'])
    assert r['events'][-1]['kind']=='capture'


def test_api_profile_and_bo_replay():
    b=TestClient(app).get('/api/twin/defaults?profile=side-exit').json()
    result=campaign(b['gantry_yaml'],b['deck_yaml'],Policy(trials=5))
    assert len(result['history'])==5  # cross a tip row boundary
    b['protocol_yaml']=result['protocol_yaml']
    assert run(b)['fluids']==result['fluids']


def test_thousand_ul_capacity_is_enforced():
    b=bundle();steps=yaml.safe_load(protocol())['protocol']
    steps[2]['transfer']['volume_ul']=1000
    d=yaml.safe_load(b['deck_yaml']);d['labware']['plate']['working_volume_ul']=1200;d['labware']['plate']['capacity_ul']=1200
    b['deck_yaml']=yaml.safe_dump(d);b['protocol_yaml']=yaml.safe_dump({'protocol':steps})
    r=run(b)
    assert [e['volume'] for e in r['events'] if e['kind']=='aspirate'][0]==1000
    assert r['fluids']['plate.A1']['volume']==1200


def test_blocked_tip_and_repeated_slot_rejected():
    for target in ('tips.A1','tips.A12'):
        b=bundle();steps=yaml.safe_load(b['protocol_yaml'])['protocol']
        if target=='tips.A1': steps[0]['pick_up_tip']['position']=target
        else: steps[4]['pick_up_tip']['position']=target
        b['protocol_yaml']=yaml.safe_dump({'protocol':steps})
        with pytest.raises(ValueError,match='blocked|consumed'):run(b)


def test_inward_rack_slot_geometry_and_corridor():
    r=run(bundle())
    points={p['id']:p for p in r['points']['tips']}
    assert points['tips.A1']=={'id':'tips.A1','x':263,'y':26,'z':70}
    assert points['tips.A2']=={'id':'tips.A2','x':253,'y':26,'z':70}
    assert points['tips.A12']=={'id':'tips.A12','x':153,'y':26,'z':70}
    assert points['tips.H12']=={'id':'tips.H12','x':153,'y':96,'z':70}
    rack=r['deck_metadata']['tips']
    assert rack['location']=={'x':150,'y':20,'z':0}
    assert (rack['length'],rack['width'],rack['height'])==(120,84,63)
    for step in (1,5,9):
        moves=[e for e in r['events'] if e['kind']=='move' and e['step']==step]
        assert all(e['start'][0]==e['end'][0]==140 for e in moves)
        assert moves[-1]['end']==[140,130,30]
