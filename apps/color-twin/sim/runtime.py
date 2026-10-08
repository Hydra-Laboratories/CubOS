"""Execute native CubOS YAML against in-memory instruments; no serial creation.

This is a kinematic/experiment simulator, not a GRBL firmware emulator.
"""
from __future__ import annotations
import copy
import math
import yaml
from pathlib import Path
from tempfile import TemporaryDirectory

from cubos.deck.loader import load_deck_from_yaml_safe
from cubos.gantry.loader import load_gantry_from_yaml_safe
from cubos.gantry.instrument_mount import InstrumentedGantry
from cubos.gantry.origin import validate_working_volume_origin
from cubos.instruments.pipette.interface import PipetteInstrument
from cubos.instruments.pipette.models import PipetteConfig, PipetteFamily, PipetteStatus, AspirateResult, MixResult
from cubos.instruments.camera.interface import CameraInstrument
from cubos.protocol_engine.loader import load_protocol_from_yaml_safe
from cubos.protocol_engine.runtime import ProtocolContext
from cubos.validation.bounds import validate_protocol_motion_bounds
from cubos.validation.protocol_semantics import validate_protocol_semantics
from .defaults import STOCKS


def rgb(hex_color):
    return [int(hex_color[i:i+2], 16) / 255 for i in (1, 3, 5)]


def mixture(parts):
    total = sum(parts)
    if total <= 0:
        return '#ffffff'
    # Illustrative Beer-Lambert absorbance, not calibrated dye optics.
    channels = [round(255 * math.exp(sum(parts[j] / total * math.log(max(.02, rgb(STOCKS[j]['hex'])[i])) for j in range(len(STOCKS))))) for i in range(3)]
    return '#' + ''.join(f'{v:02x}' for v in channels)


def distance(a, b):
    return math.sqrt(sum((x-y)**2 for x,y in zip(rgb(a), rgb(b)))) * 100


class World:
    def __init__(self, event_sink=None, step_sink=None, stock_palette=None):
        self.event_sink = event_sink
        self.step_sink = step_sink
        self.events = []
        self.time = 0.0
        self.step = -1
        self.command = 'ready'
        self.context = None
        self.stock_palette = list(stock_palette or STOCKS)
        self.channels = len(STOCKS)
        self.fluids = {}
        self.consumed_tips = set()
        self.routing = {}

    def emit(self, kind, duration=0.3, **payload):
        event = {'kind': kind, 't': self.time, 'duration': duration, 'step': self.step, 'command': self.command, **payload}
        self.events.append(event)
        self.time += duration
        if self.event_sink is not None:
            self.event_sink(event, self)

    def target(self):
        position = self.context.gantry.last_commanded_pose['instrument_position']
        matches = []
        for key, labware in self.context.deck.labware.items():
            if not getattr(labware, 'rows', 0):
                point = self.context.deck.resolve_coordinate(key)
                matches.append((math.hypot(point.x-position[0], point.y-position[1]), key))
            for row in range(getattr(labware, 'rows', 0)):
                for col in range(getattr(labware, 'columns', 0)):
                    name = f'{key}.{chr(65+row)}{col+1}'
                    point = self.context.deck.resolve_coordinate(name)
                    matches.append((math.hypot(point.x-position[0], point.y-position[1]), name))
        if not matches or min(matches)[0] > .1:
            raise ValueError('Instrument is not aligned with a supported deck target.')
        return min(matches)[1]

    def step_started(self, **kw):
        self.step, self.command = kw['index'], kw['command']
        self.emit('step', duration=0, label=self.command.replace('_', ' '))

    def step_completed(self, **kw):
        if self.step_sink is not None: self.step_sink('completed', kw)
    def step_failed(self, **kw):
        if self.step_sink is not None: self.step_sink('failed', kw)
    def step_skipped(self, **kw):
        if self.step_sink is not None: self.step_sink('skipped', kw)

    def fluid_snapshot(self):
        return {k: {'volume': sum(v), 'color': mixture(v), 'parts': v[:]} for k,v in self.fluids.items()}


class SimController:
    def __init__(self, world, bounds):
        self.world, self.bounds = world, bounds
        self.position = [bounds.x_min, bounds.y_min, bounds.z_max]

    def move_to(self, x, y, z, travel_z=None):
        # Match CubOS Mill's coordinated XY command and separate Z moves.
        sx, sy, sz = self.position
        if travel_z is None:
            targets = []
            if x != sx or y != sy:
                targets.append([x,y,sz])
            if z != sz:
                targets.append([x,y,z])
        else:
            targets = []
            if sz != travel_z:
                targets.append([sx,sy,travel_z])
            if x != sx or y != sy:
                targets.append([x,y,travel_z])
            if z != travel_z:
                targets.append([x,y,z])
        # Validate every waypoint before recording a partial move.
        for target in targets:
            if not all(math.isfinite(v) for v in target) or not self.bounds.contains(*target):
                raise ValueError(f'Simulated gantry target {target} exceeds configured travel.')
        for target in targets:
            length = math.dist(self.position, target)
            if length < 1e-8: continue
            words = ' '.join(
                f'{axis}{value:.3f}'
                for axis, before, value in zip('XYZ', self.position, target)
                if value != before
            )
            self.world.emit('move', duration=length/35, start=self.position[:], end=target[:],
                            gcode=f'G90 G1 {words} F2100')
            self.position = target[:]

    def get_coordinates(self): return dict(zip('xyz', self.position))
    def get_status(self): return 'Idle'
    def connect(self): pass
    def disconnect(self): pass
    def is_healthy(self): return True


class SimPipette(PipetteInstrument):
    def __init__(self, world, max_volume=120, **mount):
        super().__init__(offline=True, **mount)
        self.world, self.tip, self.extension = world, False, 0.0
        self.loaded = [0.0]*len(STOCKS)
        self.config = PipetteConfig(name=f'simulation_{max_volume}ul', family=PipetteFamily.PICUS2, channels=1, max_volume=max_volume, min_volume=5, volume_increment_ul=.1)

    def connect(self): pass
    def disconnect(self): pass
    def health_check(self): return True
    def home(self): self.loaded = [0.0]*len(STOCKS)
    def prime(self, speed=50): pass
    @property
    def attached_tip_extension(self): return self.extension
    @property
    def effective_depth(self): return self.depth + self.extension
    def set_attached_tip_extension(self, extension_mm):
        self.extension = float(extension_mm)
        self.world.emit('tip', attached=True, extension=self.extension, target=self.world.target())
    def clear_attached_tip_extension(self):
        self.extension = 0
        self.world.emit('tip', attached=False, extension=0, target=self.world.target())
    def get_status(self): return PipetteStatus(True, 0, self.config.max_volume, self.tip, True)
    def pick_up_tip(self, speed=50):
        target = self.world.target()
        if self.tip or target in self.world.consumed_tips:
            raise ValueError('Tip already attached or selected slot has been consumed.')
        self.world.consumed_tips.add(target)
        self.tip = True
    def drop_tip(self, speed=50):
        if sum(self.loaded) > 1e-7: raise ValueError('Cannot discard a tip containing liquid.')
        self.tip = False
    def _volume(self, volume):
        if not math.isfinite(volume) or not 5 <= volume <= self.config.max_volume or abs(volume*10-round(volume*10)) > 1e-6:
            raise ValueError(f'Simulated pipette requires 5–{self.config.max_volume:g} µL strokes in 0.1 µL increments.')
        if not self.tip: raise ValueError('Attach a tip before liquid handling.')
    def aspirate(self, volume_ul, speed=50):
        self._volume(volume_ul)
        target = self.world.target()
        parts = self.world.fluids.get(target, [0.0]*len(STOCKS))
        if sum(parts) + 1e-7 < volume_ul: raise ValueError(f'{target} has insufficient liquid.')
        if sum(self.loaded)+volume_ul > self.config.max_volume+1e-7: raise ValueError('Tip capacity exceeded.')
        taken = [v*volume_ul/sum(parts) for v in parts]
        self.world.fluids[target] = [v-t for v,t in zip(parts,taken)]
        self.loaded = [v+t for v,t in zip(self.loaded,taken)]
        self.world.emit('aspirate', duration=1.2, target=target, volume=volume_ul, color=mixture(self.loaded))
        return AspirateResult(True, volume_ul, 0, sum(self.loaded))
    def dispense(self, volume_ul, speed=50):
        self._volume(volume_ul)
        if sum(self.loaded)+1e-7 < volume_ul: raise ValueError('Dispense exceeds loaded volume.')
        target = self.world.target()
        parts = self.world.fluids.get(target, [0.0]*len(STOCKS))
        entry = self.world.deck_metadata[target.split('.')[0]]
        capacity = min(entry['capacity_ul'], entry['working_volume_ul'])
        if sum(parts)+volume_ul > capacity+1e-7: raise ValueError(f'{target} working capacity exceeded.')
        released = [v*volume_ul/sum(self.loaded) for v in self.loaded]
        self.loaded = [max(0,v-t) for v,t in zip(self.loaded,released)]
        parts = self.world.fluids[target] = [v+t for v,t in zip(parts,released)]
        self.world.emit('dispense', duration=1.2, target=target, volume=volume_ul, total=sum(parts), color=mixture(parts))
        return AspirateResult(True, volume_ul, 0, sum(self.loaded))
    def blowout(self, speed=50):
        if sum(self.loaded)>1e-7: self.dispense(sum(self.loaded),speed)
    def mix(self, volume_ul, cycles=3, speed=50, *, gantry=None, position=None):
        self._volume(volume_ul)
        target = self.world.target()
        if sum(self.world.fluids.get(target, [])) < volume_ul: raise ValueError('Insufficient well volume to mix.')
        self.world.emit('mix', duration=cycles*1.2, target=target, cycles=cycles)
        return MixResult(True,volume_ul,cycles)


class SimCamera(CameraInstrument):
    def __init__(self, world, **mount):
        super().__init__(offline=True, **mount)
        self.world = world
    def connect(self): pass
    def disconnect(self): pass
    def health_check(self): return True
    def capture(self, save_path=None):
        target = self.world.target()
        parts = self.world.fluids.get(target, [0]*len(STOCKS))
        color = mixture(parts)
        volume = sum(parts)
        reference = f'simulation://capture/{target}'
        if save_path is not None:
            # Preserve the native command's collision-safe requested name
            # without pretending the synthetic observation is a TIFF file.
            reference = f'{reference}/{Path(save_path).name}'
        self.world.emit(
            'capture', duration=1, target=target, color=color, volume=volume,
            reference=reference,
        )
        return reference


def _event_route(events, routing):
    """Serialize the actual movement events as route segments for the UI.

    When a newer core exposes MotionPlan, ``execute`` replaces this derived
    view with its ``to_dict`` result.  The fallback remains the exact segment
    stream emitted by the native protocol runtime, never an illustrated path.
    """
    segments = []
    for event in events:
        if event.get('kind') != 'move' or not event.get('start') or not event.get('end'):
            continue
        delta = [b - a for a, b in zip(event['start'], event['end'])]
        axes = ''.join(axis for axis, value in zip('XYZ', delta) if abs(value) > 1e-7)
        segments.append({
            'index': len(segments), 'step': event.get('step', -1),
            'phase': 'travel', 'start': event['start'], 'end': event['end'],
            'axes': axes or 'none', 'duration': event.get('duration', 0),
            'command': event.get('command', ''), 'reason': 'native runtime segment',
        })
    return {
        'schema_version': 'labware-routing/v1',
        'source': 'native-runtime-events',
        'planner_enabled': bool(routing.get('planner_enabled', False)),
        'strategy': 'legacy',
        'axis_order': [],
        'reason': 'legacy native movement',
        'segments': segments,
    }


def _shared_plan_dict(context):
    """Find a serialized MotionPlan exposed by a newer core runtime.

    The simulator deliberately does not calculate a second route. Core
    preflight stores immutable plans per protocol step on the context; those
    exact serialized plans are the sole source for planned path display.
    """
    candidates = []
    prepared_steps = getattr(context, 'planned_motion_steps', {})
    for step_index in sorted(prepared_steps):
        prepared = prepared_steps[step_index]
        for plan in prepared.plans:
            value = plan.to_dict()
            value['_step'] = step_index
            candidates.append(value)
    if not candidates:
        candidates = context.serialized_motion_plans()
    plans = []
    for candidate in candidates:
        value = candidate if isinstance(candidate, dict) else candidate.to_dict()
        if isinstance(value, dict) and isinstance(value.get('segments'), list):
            plans.append(copy.deepcopy(value))
    if not plans:
        return None
    if len(plans) == 1:
        return plans[0]
    combined = copy.deepcopy(plans[0])
    combined['segments'] = [
        {**segment, **({'_step': plan['_step']} if '_step' in plan else {})}
        for plan in plans for segment in plan.get('segments', [])
    ]
    combined['state_changes'] = [change for plan in plans for change in plan.get('state_changes', [])]
    combined['strategies'] = [plan.get('strategy') for plan in plans]
    combined['strategy'] = 'sequence'
    combined['reason'] = '; '.join(dict.fromkeys(str(plan.get('reason', '')).strip() for plan in plans if plan.get('reason')))
    return combined


def _route_with_shared_plan(events, routing, context, *, check_parity=True):
    executed = _event_route(events, routing)
    planned = _shared_plan_dict(context)
    if planned is None:
        if getattr(context.deck, 'planning_enabled', False):
            raise RuntimeError('Motion planning is enabled for this demo, but the core MotionPlan was not produced.')
        return executed
    segments = planned.get('segments', [])
    normalized = []
    for index, segment in enumerate(segments):
        item = dict(segment)
        item.setdefault('index', index)
        if '_step' in item:
            item['step'] = item.pop('_step')
        for key in ('start', 'end'):
            point = item.get(key)
            if isinstance(point, dict):
                item[key] = [point.get('x'), point.get('y'), point.get('z')]
        item.setdefault('axes', item.get('axis', ''))
        normalized.append(item)
    planned['segments'] = normalized
    planned['source'] = 'core-motion-plan'
    planned['execution_segments'] = executed['segments']
    # Compare only the exact carriage endpoints, keeping extra core phase
    # labels and state changes available for display.
    planned_endpoints = [(tuple(item.get('start', ())), tuple(item.get('end', ()))) for item in normalized]
    executed_endpoints = [(tuple(item.get('start', ())), tuple(item.get('end', ()))) for item in executed['segments']]
    planned['parity'] = {'segments_equal': planned_endpoints == executed_endpoints, 'checked': check_parity}
    if check_parity and getattr(context.deck, 'planning_enabled', False) and not planned['parity']['segments_equal']:
        raise RuntimeError('Core MotionPlan and simulator execution segments differ; refusing to present a routed run.')
    scene = getattr(getattr(context, 'routing_session', None), 'base_scene', None)
    if scene is not None:
        planned['fixtures'] = [fixture.to_dict() for fixture in scene.fixtures]
    planned.setdefault('strategy', executed['strategy'])
    planned.setdefault('reason', '')
    planned.setdefault('axis_order', [segment.get('axes', '') for segment in normalized])
    return planned


def execute(gantry_yaml, deck_yaml, protocol_yaml, *, event_sink=None,
            ready_sink=None, step_sink=None, initial_position=None, validate_only=False,
            routing=None):
    with TemporaryDirectory(prefix='cubos-twin-') as directory:
        paths = []
        for name, content in zip(('gantry','deck','protocol'),(gantry_yaml,deck_yaml,protocol_yaml)):
            if len(content)>200000: raise ValueError('YAML exceeds 200 KB limit.')
            path = Path(directory)/f'{name}.yaml'
            path.write_text(content)
            paths.append(path)
        raw_protocol = yaml.safe_load(protocol_yaml)
        flat_steps = raw_protocol.get('protocol') if isinstance(raw_protocol, dict) else None
        allowed = {'pick_up_tip','drop_tip','transfer','mix','measure','move','capture'}
        if not isinstance(flat_steps, list) or not 1 <= len(flat_steps) <= 400:
            raise ValueError('Provide 1–400 flat protocol steps.')
        for raw_step in flat_steps:
            if not isinstance(raw_step, dict) or len(raw_step) != 1 or next(iter(raw_step)) not in allowed:
                raise ValueError('Unsupported simulation command; use flat pick_up_tip, drop_tip, transfer, mix, measure, move, or capture steps.')
            args = next(iter(raw_step.values()))
            if isinstance(args, dict):
                for field in ('volume_ul','cycles'):
                    if field in args and (not isinstance(args[field], (int,float)) or not math.isfinite(args[field]) or not 0 < args[field] <= (4000 if field == 'volume_ul' else 100)):
                        raise ValueError(f'{field} exceeds the supported simulation range.')
        routing_metadata = routing or (yaml.safe_load(deck_yaml) or {}).get('motion_planning', {})
        config = load_gantry_from_yaml_safe(paths[0])
        if set(config.instruments) != {'pipette','camera'}:
            raise ValueError('This scene requires exactly the pipette and camera instruments.')
        if config.instruments['pipette'].get('type') != 'pipette' or config.instruments['camera'].get('type') != 'camera':
            raise ValueError('Expected pipette and camera instrument types.')
        if config.instruments['pipette'].get('pipette_model') not in {'picus2_1ch_120', 'simulation_1000ul'}:
            raise ValueError('This simulator requires a supported simulation pipette profile.')
        if config.instruments['pipette'].get('liquid_classes'):
            raise ValueError('Liquid-class corrections are not yet modeled by this simulator.')
        deck_metadata = yaml.safe_load(deck_yaml)['labware']
        expected = {'plate': ('well_plate',8,12), 'stocks': ('vial_grid',2,None), 'tips': ('tip_rack',8,12)}
        if set(deck_metadata) not in (set(expected), set(expected) | {'waste'}):
            raise ValueError('The photo scene requires plate, stocks, and tips labware keys.')
        for key,(kind,rows,columns) in expected.items():
            entry = deck_metadata[key]
            if entry.get('type') != kind or entry.get('rows') != rows or (columns is not None and entry.get('columns') != columns) or (key == 'stocks' and entry.get('columns') not in (3, 6)):
                shape = f'{rows} × {columns}' if columns is not None else '2 × 3 or 2 × 6'
                raise ValueError(f'{key} must remain a {shape} {kind} for this scene.')
            if key != 'tips':
                for field in ('capacity_ul','working_volume_ul'):
                    value = entry.get(field)
                    if not isinstance(value,(int,float)) or not math.isfinite(value) or value <= 0:
                        raise ValueError(f'{key}.{field} must be finite and positive.')
        if config.origin_policy.value != 'deck_origin' or config.y_axis_motion.value != 'bed':
            raise ValueError('This photo model currently supports deck_origin with bed Y motion only.')
        validate_working_volume_origin(config)
        deck = load_deck_from_yaml_safe(paths[1], factory_z_travel_mm=config.factory_z_travel_mm)
        protocol = load_protocol_from_yaml_safe(paths[2])
        allowed = {'pick_up_tip','drop_tip','transfer','mix','measure','move','capture'}
        if len(protocol.steps)>400: raise ValueError('At most 400 steps per simulation.')
        for step in protocol.steps:
            if step.command_name not in allowed:
                raise ValueError(f'Unsupported simulation command: {step.command_name}. Supported: {sorted(allowed)}')
            if step.command_name == 'measure' and (step.args.get('instrument') != 'camera' or step.args.get('method') != 'capture'):
                raise ValueError('Only synthetic camera capture measurements are supported.')
        transfer_sources = []
        for raw_step in flat_steps:
            args = next(iter(raw_step.values())) if isinstance(raw_step, dict) else {}
            if isinstance(args, dict) and isinstance(args.get('source'), str) and args['source'] not in transfer_sources:
                transfer_sources.append(args['source'])
        stock_palette = [dict(STOCKS[i % len(STOCKS)], target=target) for i, target in enumerate(transfer_sources)] or list(STOCKS)
        world = World(event_sink, step_sink, stock_palette=stock_palette)
        world.deck_metadata = deck_metadata
        initial_volume = min(4000, deck_metadata['stocks']['working_volume_ul'])
        world.fluids = {stock['target']: [initial_volume if i == j % len(STOCKS) else 0.0 for i in range(len(STOCKS))] for j,stock in enumerate(stock_palette)}
        world.routing = routing_metadata
        controller = SimController(world, config.working_volume)
        if initial_position is not None:
            if not config.working_volume.contains(*initial_position):
                raise ValueError('Current simulated position is outside the selected gantry bounds.')
            controller.position = list(initial_position)
        start_position = controller.position[:]
        instruments = {}
        for name, cls in (('pipette',SimPipette),('camera',SimCamera)):
            raw = config.instruments[name]
            mount = {k:raw.get(k,0) for k in ('offset_x','offset_y','depth')}
            instruments[name] = cls(world, name=name, **mount, **({'max_volume': 1000 if raw.get('pipette_model') == 'simulation_1000ul' else 120} if name == 'pipette' else {}))
        gantry = InstrumentedGantry(controller, instruments, safe_z=config.resolved_safe_z,
                                    motion_envelopes=getattr(config, 'motion_envelopes', None))
        violations = validate_protocol_motion_bounds(config,protocol,deck,gantry)
        violations += validate_protocol_semantics(protocol,gantry,deck,config)
        if violations: raise ValueError('; '.join(str(v) for v in violations))
        context = ProtocolContext(
            gantry=gantry, deck=deck, gantry_config=config,
            positions=protocol.positions, step_observer=world,
            image_output_dir=Path(directory) / 'images',
        )
        world.context = context
        points = {}
        for key,item in deck.labware.items():
            points[key] = []
            for row in range(getattr(item,'rows',0)):
                for col in range(getattr(item,'columns',0)):
                    name = f'{key}.{chr(65+row)}{col+1}'
                    p = deck.resolve_coordinate(name)
                    points[key].append({'id':name,'x':p.x,'y':p.y,'z':p.z})
        result = {'events': [], 'duration': 0, 'steps': len(protocol.steps), 'points': points,
                  'fluids': world.fluid_snapshot(), 'deck_metadata': deck_metadata,
                  'mounts': copy.deepcopy(config.instruments), 'initial_position': start_position,
                  'routing': routing_metadata,
                  'plan': [{'index': s.index, 'command': s.command_name,
                            'summary': s.command_name.replace('_', ' '), 'args': s.args}
                           for s in protocol.steps]}
        if validate_only:
            if getattr(deck, 'planning_enabled', False):
                from cubos.protocol_engine.routing import prepare_planning_context
                prepare_planning_context(protocol, context)
                result['route'] = _route_with_shared_plan([], routing_metadata, context, check_parity=False)
            else:
                result['route'] = {'schema_version': 'labware-routing/v1', 'source': 'not-executed',
                                   'planner_enabled': False, 'segments': []}
            return result
        if ready_sink is not None:
            ready_sink(result)
        protocol.execute(context)
        route = _route_with_shared_plan(world.events, routing_metadata, context)
        return {**result, 'events': world.events, 'duration': world.time,
                'fluids': world.fluid_snapshot(), 'route': route,
                'plan': [{**item, 'route_segments': [s for s in route['segments'] if s.get('step') == item['index']]} for item in result['plan']]}
