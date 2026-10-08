"""Uncalibrated 56 mm-travel example for the fork's open-ended rack."""
import copy
import yaml
from .defaults import GANTRY, DECK, STOCKS, dump, protocol_for


def protocol(volumes=(100, 100, 100), well='A1', tip_start=0):
    original = yaml.safe_load(protocol_for(volumes, well, tip_start))['protocol']
    steps = []
    for step in original:
        if 'pick_up_tip' in step:
            target = step['pick_up_tip']['position']
            row, column = target.split('.')[1][0], int(target.split('.')[1][1:])
            step = {'pick_up_tip': {'position': f'tips.{row}{13-column}'}}
            steps.append(step)
            # Leave the rack's Y footprint in the clear inward corridor
            # before normal X-first travel to the source vials.
            steps.append({'move': {'instrument': 'pipette', 'position': [140, 130, 30], 'travel_z': 30}})
            continue
        if 'drop_tip' in step:
            step = {'drop_tip': {'position': 'waste'}}
        steps.append(step)
    return dump({'protocol': steps})


def bundle():
    gantry, deck = copy.deepcopy(GANTRY), copy.deepcopy(DECK)
    gantry['cnc'].update(factory_z_travel_mm=56, safe_z=56)
    gantry['working_volume']['z_max'] = 56
    gantry['instruments']['pipette'].update(pipette_model='simulation_1000ul', depth=-70)
    gantry['instruments']['camera']['depth'] = 0
    rack = deck['labware']['tips']
    rack.update(name='1000 µL / 70 mm side-exit rack', model_name='ColorMatching_TipHolder',
                location={'x': 150, 'y': 20, 'z': 0}, length=120, width=84, height=63,
                pickup_z=70, tip_length=70, x_offset=10, y_offset=10, side_exit={'lift_mm': 30, 'exit_x': 140},
                calibration={'a1': {'x': 263, 'y': 26, 'z': 70}, 'a2': {'x': 253, 'y': 26, 'z': 70}})
    deck['labware']['waste'] = {'type': 'vial', 'name': 'Virtual tip waste',
        'location': {'x': 280, 'y': 145, 'z': 45}, 'height': 45, 'diameter': 20,
        'capacity_ul': 5000, 'working_volume_ul': 4000}
    return {'gantry_yaml': dump(gantry), 'deck_yaml': dump(deck), 'protocol_yaml': protocol(),
            'stocks': STOCKS, 'assumptions': [
                'User-specified 56 mm usable Z travel; 70 mm attached tip extension is provisional.',
                'CAD from adediredaniel/Cubware main 8072cbe: rack mesh is 120 × 84 × 63 mm after orientation.',
                'Pickup at nozzle Z70: carriage Z0; lift to carriage Z30; exit -X toward the vials at X140 without changing Y or Z.',
                'Tips are consumed from the open -X edge inward. A separate Y move outside the rack precedes travel to stock.',
                'Deck anchors, nozzle depth, waste and camera offsets are simulation estimates; measure before physical use.',
                'Native CubOS execution with synthetic liquids; no serial traffic, firmware or collision certification.',
            ]}
