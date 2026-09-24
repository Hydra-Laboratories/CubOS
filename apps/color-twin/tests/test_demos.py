"""Three native routing demonstration inputs and simulator evidence."""
from pathlib import Path
import hashlib

import yaml

from sim.demos import SAVED_PICUS120, SAVED_USER_DECK, _ordinary_motion_metadata, _saved_gantry, _with_motion, demos, get_demo
from sim.runtime import SimCamera, execute
from sim.server import compare_ordinary
from cubos.deck.loader import load_deck_from_yaml_safe


def test_saved_user_deck_is_preserved_byte_for_byte():
    assert hashlib.sha256(SAVED_USER_DECK.read_bytes()).hexdigest() == 'db518dbb124aa0c3c95ece7692a66fa80370c48af536557add067f29977e4cc4'


def test_saved_user_gantry_fixture_matches_generated_profile():
    fixture = Path(__file__).parents[1] / 'examples' / 'saved-user' / 'gantry.yaml'
    assert yaml.safe_load(fixture.read_text()) == yaml.safe_load(_saved_gantry())


def test_catalog_has_exactly_four_named_protocol_demos():
    catalog = demos()
    assert [item['id'] for item in catalog] == [
        'ordinary-transfer', 'saved-side-exit', 'saved-detour-to-waste', 'saved-picus120'
    ]
    assert all(item['routing']['schema_version'] == 'labware-routing/v1' for item in catalog)


def test_ordinary_native_run_has_vertical_route_and_liquid_outcome():
    bundle = get_demo('ordinary-transfer').bundle()
    result = execute(bundle['gantry_yaml'], bundle['deck_yaml'], bundle['protocol_yaml'], routing=bundle['routing'])
    assert result['route']['strategy'] == 'legacy'
    assert result['route']['segments']
    assert result['fluids']['plate.A1']['parts'] == [100, 100, 100]


def test_ordinary_planner_comparison_uses_same_native_protocol_and_matches():
    result = compare_ordinary()
    assert result.get('planner_error') is None
    assert result['same_liquid_outcome'] is True
    assert result['legacy_inputs']['protocol_yaml'] == result['planned_inputs']['protocol_yaml']
    assert result['legacy_inputs']['initial_position'] == result['planned_inputs']['initial_position']
    assert result['legacy_run']['fluids'] == result['planned_run']['fluids']
    assert result['planned_route']['source'] == 'core-motion-plan'
    assert result['planned_route']['parity'] == {'segments_equal': True, 'checked': True}
    assert result['planned_run']['route']['segments'] == result['planned_route']['segments']
    assert any(segment['axis'] == 'xy' for segment in result['planned_route']['segments'])
    assert any(segment['axes'] == 'XY' for segment in result['planned_route']['execution_segments'])


def test_ordinary_registered_boxes_contain_calibrated_labware(tmp_path):
    demo = get_demo('ordinary-transfer')
    raw = _with_motion(yaml.safe_load(demo.deck_yaml), _ordinary_motion_metadata())
    path = tmp_path / 'ordinary.yaml'
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    deck = load_deck_from_yaml_safe(path)

    def bounds(key):
        box = deck.labware[key].motion['resolved_box']
        return box['min'], box['max']

    def inside(point, minimum, maximum, radius=0):
        return (minimum['x'] + radius <= point.x <= maximum['x'] - radius
                and minimum['y'] + radius <= point.y <= maximum['y'] - radius)

    minimum, maximum = bounds('plate')
    for row in range(8):
        for col in range(12):
            assert inside(deck.resolve_coordinate(f'plate.{chr(65 + row)}{col + 1}'), minimum, maximum)
    minimum, maximum = bounds('stocks')
    for row in range(2):
        for col in range(3):
            assert inside(deck.resolve_coordinate(f'stocks.{chr(65 + row)}{col + 1}'), minimum, maximum, radius=9)
    minimum, maximum = bounds('tips')
    for row in range(8):
        for col in range(12):
            assert inside(deck.resolve_coordinate(f'tips.{chr(65 + row)}{col + 1}'), minimum, maximum)


def test_saved_side_exit_uses_a1_without_protocol_waypoints():
    bundle = get_demo('saved-side-exit').bundle()
    raw = yaml.safe_load(bundle['protocol_yaml'])['protocol']
    assert [step['pick_up_tip']['position'] for step in raw if 'pick_up_tip' in step] == ['tips.A1', 'tips.A2', 'tips.A3']
    assert not any('move' in step for step in raw)
    result = execute(bundle['gantry_yaml'], bundle['deck_yaml'], bundle['protocol_yaml'], routing=bundle['routing'])
    assert result['route']['source'] == 'core-motion-plan'
    assert result['route']['parity'] == {'segments_equal': True, 'checked': True}
    assert sum(yaml.safe_load(bundle['deck_yaml'])['labware']['tips']['tip_present'].values()) == 96
    moves = [event for event in result['events'] if event['kind'] == 'move' and event['step'] == 0]
    assert any(event['end'][2] - event['start'][2] == 30 for event in moves)
    exits = [segment for segment in result['route']['segments'] if segment.get('step') == 0 and segment.get('phase') == 'exit']
    assert exits and exits[-1]['end'][0] < 139.456
    assert result['fluids']['plate.A1']['parts'] == [100, 100, 100]
    assert result['deck_metadata']['stocks']['columns'] == 6


def test_saved_detour_has_shared_route_evidence_and_two_by_six_stocks():
    bundle = get_demo('saved-detour-to-waste').bundle()
    result = execute(bundle['gantry_yaml'], bundle['deck_yaml'], bundle['protocol_yaml'], routing=bundle['routing'])
    assert result['deck_metadata']['stocks']['columns'] == 6
    assert result['route']['source'] == 'core-motion-plan'
    assert result['route']['parity'] == {'segments_equal': True, 'checked': True}
    transit = [segment['axis'] for segment in result['route']['segments'] if segment.get('step') == 2 and segment.get('phase') == 'engage_approach_transit']
    assert transit == ['y', 'x']
    assert sum(yaml.safe_load(bundle['deck_yaml'])['labware']['tips']['tip_present'].values()) == 96
    assert result['route']['segments']
    assert result['fluids']['plate.A1']['parts'] == [100, 0, 0]


def test_saved_picus120_matches_example_files_and_completes_batch_one():
    bundle = get_demo('saved-picus120').bundle()
    assert bundle['gantry_yaml'] == (SAVED_PICUS120 / 'gantry.yaml').read_text()
    assert bundle['deck_yaml'] == (SAVED_PICUS120 / 'deck.yaml').read_text()
    result = execute(
        bundle['gantry_yaml'], bundle['deck_yaml'], bundle['protocol_yaml'],
        routing=bundle['routing'], initial_position=bundle['initial_position'],
    )
    assert result['events']
    assert result['steps'] == 48
    for well in ('A5', 'A6', 'A7', 'A8', 'A9', 'A10'):
        assert result['fluids'][f'plate.{well}']['volume'] == 300


def test_planning_preview_matches_execution_plan_without_events():
    bundle = get_demo('saved-side-exit').bundle()
    preview = execute(bundle['gantry_yaml'], bundle['deck_yaml'], bundle['protocol_yaml'], routing=bundle['routing'], initial_position=bundle['initial_position'], validate_only=True)
    run = execute(bundle['gantry_yaml'], bundle['deck_yaml'], bundle['protocol_yaml'], routing=bundle['routing'], initial_position=bundle['initial_position'])
    assert preview['events'] == []
    assert [(s['start'], s['end']) for s in preview['route']['segments']] == [(s['start'], s['end']) for s in run['route']['segments']]


def _native_capture_bundle():
    bundle = get_demo('saved-side-exit').bundle()
    raw = yaml.safe_load(bundle['protocol_yaml'])
    steps = []
    capture_index = 0
    for step in raw['protocol']:
        steps.append(step)
        if 'transfer' not in step:
            continue
        capture_index += 1
        steps.extend([
            {'move': {'instrument': 'camera', 'position': 'plate.A1'}},
            {'capture': {
                'instrument': 'camera',
                'position': 'plate.A1',
                'label': f'plate-A1-after-{capture_index * 100}ul',
            }},
        ])
    bundle['protocol_yaml'] = yaml.safe_dump(raw | {'protocol': steps}, sort_keys=False)
    return bundle


def test_routed_native_captures_observe_each_dispense(monkeypatch):
    bundle = _native_capture_bundle()
    calls = []
    native_capture = SimCamera.capture

    def recording_capture(self, save_path=None):
        assert save_path is not None
        path = Path(save_path)
        assert path.parent.parent.name == 'images'
        assert not path.exists()
        saved = native_capture(self, save_path=save_path)
        assert not path.exists()
        calls.append(path)
        return saved

    monkeypatch.setattr(SimCamera, 'capture', recording_capture)
    result = execute(
        bundle['gantry_yaml'], bundle['deck_yaml'], bundle['protocol_yaml'],
        routing=bundle['routing'], initial_position=bundle['initial_position'],
    )

    captures = [event for event in result['events'] if event['kind'] == 'capture']
    assert [event['target'] for event in captures] == ['plate.A1'] * 3
    assert [event['volume'] for event in captures] == [100, 200, 300]
    assert [path.name.split('_', 1)[0] for path in calls] == [
        'plate-A1-after-100ul', 'plate-A1-after-200ul', 'plate-A1-after-300ul',
    ]
    assert [event['reference'].rsplit('/', 1)[-1] for event in captures] == [
        path.name for path in calls
    ]
    assert result['route']['source'] == 'core-motion-plan'
    assert result['route']['parity'] == {'segments_equal': True, 'checked': True}


def test_routed_native_capture_preview_does_not_capture_or_write(monkeypatch):
    import cubos.protocol_engine.commands.camera as camera_commands

    bundle = _native_capture_bundle()

    def unexpected_call(*args, **kwargs):
        raise AssertionError('preview attempted to capture or allocate an image path')

    monkeypatch.setattr(SimCamera, 'capture', unexpected_call)
    monkeypatch.setattr(camera_commands, 'build_image_path', unexpected_call)
    preview = execute(
        bundle['gantry_yaml'], bundle['deck_yaml'], bundle['protocol_yaml'],
        routing=bundle['routing'], initial_position=bundle['initial_position'],
        validate_only=True,
    )

    assert preview['events'] == []
    assert preview['route']['source'] == 'core-motion-plan'
    assert preview['route']['parity'] == {'segments_equal': False, 'checked': False}
