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
    def __init__(self, event_sink=None, step_sink=None):
        self.event_sink = event_sink
        self.step_sink = step_sink
        self.events = []
        self.time = 0.0
        self.step = -1
        self.command = 'ready'
        self.context = None
        self.fluids = {s['target']: [4000.0 if i == j else 0.0 for i in range(len(STOCKS))] for j,s in enumerate(STOCKS)}
        self.consumed_tips = set()

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
        # Match CubOS Mill: direct X/Y/Z, or travel-Z then X/Y/final-Z.
        sx, sy, sz = self.position
        targets = ([[x,sy,sz], [x,y,sz], [x,y,z]] if travel_z is None
                   else [[sx,sy,travel_z], [x,sy,travel_z], [x,y,travel_z], [x,y,z]])
        # Validate every waypoint before recording a partial move.
        for target in targets:
            if not all(math.isfinite(v) for v in target) or not self.bounds.contains(*target):
                raise ValueError(f'Simulated gantry target {target} exceeds configured travel.')
        for target in targets:
            length = math.dist(self.position, target)
            if length < 1e-8: continue
            self.world.emit('move', duration=length/35, start=self.position[:], end=target[:],
                            gcode='G90 G1 ' + ' '.join(f'{a}{v:.3f}' for a,v in zip('XYZ',target)) + ' F2100')
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
    def capture(self):
        target = self.world.target()
        parts = self.world.fluids.get(target, [0]*len(STOCKS))
        self.world.emit('capture', duration=1, target=target, color=mixture(parts), volume=sum(parts))
        return f'simulation://capture/{target}'


def execute(gantry_yaml, deck_yaml, protocol_yaml, *, event_sink=None,
            ready_sink=None, step_sink=None, initial_position=None, validate_only=False):
    with TemporaryDirectory(prefix='cubos-twin-') as directory:
        paths = []
        for name, content in zip(('gantry','deck','protocol'),(gantry_yaml,deck_yaml,protocol_yaml)):
            if len(content)>200000: raise ValueError('YAML exceeds 200 KB limit.')
            path = Path(directory)/f'{name}.yaml'
            path.write_text(content)
            paths.append(path)
        raw_protocol = yaml.safe_load(protocol_yaml)
        flat_steps = raw_protocol.get('protocol') if isinstance(raw_protocol, dict) else None
        allowed = {'pick_up_tip','drop_tip','transfer','mix','measure','move'}
        if not isinstance(flat_steps, list) or not 1 <= len(flat_steps) <= 400:
            raise ValueError('Provide 1–400 flat protocol steps.')
        for raw_step in flat_steps:
            if not isinstance(raw_step, dict) or len(raw_step) != 1 or next(iter(raw_step)) not in allowed:
                raise ValueError('Unsupported simulation command; use flat pick_up_tip, drop_tip, transfer, mix, measure, or move steps.')
            args = next(iter(raw_step.values()))
            if isinstance(args, dict):
                for field in ('volume_ul','cycles'):
                    if field in args and (not isinstance(args[field], (int,float)) or not math.isfinite(args[field]) or not 0 < args[field] <= (4000 if field == 'volume_ul' else 100)):
                        raise ValueError(f'{field} exceeds the supported simulation range.')
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
        expected = {'plate': ('well_plate',8,12), 'stocks': ('vial_grid',2,3), 'tips': ('tip_rack',8,12)}
        if set(deck_metadata) not in (set(expected), set(expected) | {'waste'}):
            raise ValueError('The photo scene requires plate, stocks, and tips labware keys.')
        for key,(kind,rows,columns) in expected.items():
            entry = deck_metadata[key]
            if (entry.get('type'),entry.get('rows'),entry.get('columns')) != (kind,rows,columns):
                raise ValueError(f'{key} must remain a {rows} × {columns} {kind} for this scene.')
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
        allowed = {'pick_up_tip','drop_tip','transfer','mix','measure','move'}
        if len(protocol.steps)>400: raise ValueError('At most 400 steps per simulation.')
        for step in protocol.steps:
            if step.command_name not in allowed:
                raise ValueError(f'Unsupported simulation command: {step.command_name}. Supported: {sorted(allowed)}')
            if step.command_name == 'measure' and (step.args.get('instrument') != 'camera' or step.args.get('method') != 'capture'):
                raise ValueError('Only synthetic camera capture measurements are supported.')
        world = World(event_sink, step_sink)
        world.deck_metadata = deck_metadata
        initial_volume = min(4000, deck_metadata['stocks']['working_volume_ul'])
        world.fluids = {stock['target']: [initial_volume if i == j else 0.0 for i in range(len(STOCKS))] for j,stock in enumerate(STOCKS)}
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
        gantry = InstrumentedGantry(controller, instruments, safe_z=config.resolved_safe_z)
        violations = validate_protocol_motion_bounds(config,protocol,deck,gantry)
        violations += validate_protocol_semantics(protocol,gantry,deck,config)
        if violations: raise ValueError('; '.join(str(v) for v in violations))
        context = ProtocolContext(gantry=gantry,deck=deck,gantry_config=config,positions=protocol.positions,step_observer=world)
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
                  'plan': [{'index': s.index, 'command': s.command_name,
                            'summary': s.command_name.replace('_', ' '), 'args': s.args}
                           for s in protocol.steps]}
        if validate_only:
            return result
        if ready_sink is not None:
            ready_sink(result)
        protocol.execute(context)
        return {**result, 'events': world.events, 'duration': world.time,
                'fluids': world.fluid_snapshot()}
