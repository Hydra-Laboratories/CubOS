"""Photo-derived illustrative scene; all mounting coordinates await calibration."""
import math
import yaml

GANTRY = {
    'serial_port': 'SIMULATION_ONLY', 'gantry_type': 'cub',
    'cnc': {'factory_z_travel_mm': 100, 'y_axis_motion': 'bed', 'safe_z': 65},
    'working_volume': {'x_min': 0, 'x_max': 290, 'y_min': 0, 'y_max': 180, 'z_min': 0, 'z_max': 100},
    'instruments': {
        'pipette': {'type': 'pipette', 'vendor': 'sartorius', 'pipette_model': 'picus2_1ch_120', 'offset_x': 0, 'offset_y': 0, 'depth': 0},
        'camera': {'type': 'camera', 'vendor': 'mount_only', 'offset_x': -25, 'offset_y': -28, 'depth': -18},
    },
}
DECK = {'labware': {
    'plate': {'type': 'well_plate', 'name': '96-well color plate', 'rows': 8, 'columns': 12,
              'length': 127.76, 'width': 85.48, 'height': 15, 'capacity_ul': 300, 'working_volume_ul': 300,
              'calibration': {'a1': {'x': 30, 'y': 90, 'z': 15}, 'a2': {'x': 39, 'y': 90, 'z': 15}},
              'x_offset': 9, 'y_offset': 9, 'row_direction': 'negative'},
    'stocks': {'type': 'vial_grid', 'name': 'Six-vial holder', 'rows': 2, 'columns': 3,
               'calibration': {'a1': {'x': 35, 'y': 150, 'z': 45}, 'a2': {'x': 58, 'y': 150, 'z': 45}},
               'x_offset': 23, 'y_offset': 23, 'row_direction': 'negative', 'vial_height': 45,
               'vial_diameter': 18, 'capacity_ul': 5000, 'working_volume_ul': 4000},
    'tips': {'type': 'tip_rack', 'name': 'Picus tip rack', 'model_name': 'photo_estimate',
             'location': {'x': 170, 'y': 20, 'z': 0}, 'rows': 8, 'columns': 12,
             'calibration': {'a1': {'x': 178, 'y': 90, 'z': 45}, 'a2': {'x': 187, 'y': 90, 'z': 45}},
             'x_offset': 9, 'y_offset': 9, 'pickup_z': 45,
             'tip_length': 25, 'length': 128, 'width': 86, 'height': 45},
}}
STOCKS = [
    {'name': 'Red', 'target': 'stocks.A1', 'hex': '#df244b'},
    {'name': 'Yellow', 'target': 'stocks.A2', 'hex': '#f1ca25'},
    {'name': 'Blue', 'target': 'stocks.A3', 'hex': '#246ddd'},
]

def dump(value):
    return '# SIMULATION ONLY — photo-derived estimates, not calibrated hardware configs.\n' + yaml.safe_dump(value, sort_keys=False)

def protocol_for(volumes, well='A1', tip_start=0):
    if len(volumes) != 3 or any(not math.isfinite(v) or not 50 <= v <= 200 for v in volumes) or not math.isclose(sum(volumes), 300, abs_tol=1e-6):
        raise ValueError('Each recipe requires three colors at 50–200 µL each, totaling 300 µL.')
    steps = []
    for index, (stock, volume) in enumerate(zip(STOCKS, volumes)):
        if volume <= 0:
            continue
        tip = tip_start + index
        if tip >= 96:
            raise ValueError('Tip rack exhausted; reset the simulated campaign.')
        tip_id = f'{chr(65 + tip // 12)}{tip % 12 + 1}'
        steps += [{'pick_up_tip': {'position': f'tips.{tip_id}'}},
                  {'transfer': {'source': stock['target'], 'destination': f'plate.{well}', 'volume_ul': volume, 'source_height': -8}},
                  {'drop_tip': {'position': f'tips.{tip_id}'}}]
    if steps:
        steps.insert(len(steps)-1, {'mix': {'position': f'plate.{well}', 'volume_ul': min(60, sum(volumes)), 'cycles': 3, 'height': -3}})
    steps += [{'measure': {'instrument': 'camera', 'position': f'plate.{well}', 'measurement_height': 40, 'method': 'capture'}}]
    return dump({'protocol': steps})

def bundle():
    return {'gantry_yaml': dump(GANTRY), 'deck_yaml': dump(DECK),
            'protocol_yaml': protocol_for([100, 100, 100]), 'stocks': STOCKS,
            'assumptions': [
                'Photo-derived mounting and deck dimensions; not calibrated.',
                'Illustrative Z travel is 100 mm to preview the raised setup. Factory PROVer V2 Z travel is 40 mm; actual reach must be measured.',
                'Camera lens, field of view, and offsets are estimates.',
                'Synthetic absorbance mixing and RGB distance; no measured chemistry or camera calibration.',
                'Tip return uses the same rack slot for this virtual demonstration; a real waste location and tip strategy are required.',
                'Motion timing uses constant feed; no GRBL firmware, acceleration, collision, or emergency-stop fidelity yet.',
            ]}
