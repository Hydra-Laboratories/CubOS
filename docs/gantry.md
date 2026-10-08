# Gantry

The gantry is the CNC motion platform that moves instruments over the deck.
CubOS communicates with GRBL-based controllers over serial.

## Coordinate Convention

The high-level gantry boundary uses the CubOS deck frame. Gantry YAML's
`origin_policy` picks which corner is zero (see [Origin
Policy](#origin-policy)); axis directions are fixed regardless of policy:

- origin `(0, 0, 0)` is the front-left-bottom reachable work volume under the
  default `deck_origin` policy — the homed back-right-top corner under
  `home_origin` instead
- `+X` moves right from the operator perspective
- `+Y` moves away from the operator, toward the back of the deck
- `+Z` moves up, away from the deck
- `-Z` moves down, toward the deck

The low-level controller may physically home at the opposite back-right-top
corner. That machine-frame detail stays at the controller/GRBL boundary. CubOS
does not apply a hidden Z sign flip in the high-level `Gantry` wrapper.

Protocol `home` runs GRBL `$H` and preserves the calibrated G54 WPos frame. It
does not apply `G92` or redefine work coordinates after homing.

## Origin Policy

Gantry YAML's top-level `origin_policy: deck_origin | home_origin` (default
`deck_origin`) selects the coordinate frame's zero point. This is a generic
policy any machine config may select — there are no machine-specific branches
for it.

- **`deck_origin`** (default) — the [Coordinate Convention](#coordinate-convention)
  above: work-position zero sits at the front-left-bottom deck corner.
  `working_volume` has `x_min: 0.0`, `y_min: 0.0`, and non-negative `z_min`;
  homing drives to the back-right-top corner, whose work position equals the
  `working_volume` maxima.
- **`home_origin`** — work-position zero sits at the homed back-right-top
  corner instead. The entire reachable workspace is expressed in negative
  coordinates: `working_volume` has `x_max: 0.0`, `y_max: 0.0`, and a
  non-positive `z_max`, with negative minima. After homing, WPos reads
  `(0, 0, 0)`.

Example `home_origin` working volume for a Cub XL-class machine:

```yaml
origin_policy: home_origin
working_volume:
  x_min: -400.0
  x_max: 0.0
  y_min: -300.0
  y_max: 0.0
  z_min: -100.0
  z_max: 0.0
cnc:
  safe_z: -5.0
```

`safe_z` is always an absolute ceiling constrained to `[working_volume.z_min,
working_volume.z_max]` — under `home_origin` it is typically a small negative
value (`-5.0` above); under `deck_origin` it defaults to
`working_volume.z_max` as described in [CNC Fields](#cnc-fields). Bounds
validation everywhere compares against the configured `working_volume`
min/max, never against literal zero, so a target outside the volume is
rejected before any motion under either policy.

Labware-relative height semantics (`measurement_height`,
`interwell_scan_height`, pipette `height`, …) are unchanged by policy, and
physical dimensions (`tip_length`, `calibration_block_height_mm`, …) are
always positive. Absolute Z targets derived from them — including tip-rack
`pickup_z`/`drop_z` — may be negative under `home_origin`.

## Config

Gantry YAML defines:

- `gantry_type`
- calibration reference height (`cnc.calibration_block_height_mm` — the key
  name is historical; it stores the Z reference height whether it came from
  the calibration block or another reference, see
  [Calibration Reference Options](calibration.md#calibration-reference-options))
- mounted instruments, offsets, reach depths, and driver settings

Representative example:

```yaml
serial_port: /dev/ttyUSB0
gantry_type: cub_xl
cnc:
  factory_z_travel_mm: 87.0
  y_axis_motion: head
  # Absolute deck-frame Z used for inter-labware travel and the entry
  # approach to the first well of a scan. Defaults to working_volume.z_max.
  safe_z: 85.0

working_volume:
  x_min: 0.0
  x_max: 399.0
  y_min: 0.0
  y_max: 280.0
  z_min: 0.0
  z_max: 87.0

grbl_settings:
  dir_invert_mask: 1
  status_report: 0
  homing_enable: true
  homing_dir_mask: 0
  homing_pull_off: 10.0
  max_travel_x: 409.0
  max_travel_y: 290.0
  max_travel_z: 97.0

instruments:
  asmi:
    type: asmi
    vendor: vernier
    offset_x: 0.0
    offset_y: 0.0
    depth: 0.0
```

Use this file when:

- switching to a different gantry
- changing travel limits
- recording expected controller settings
- changing mounted instruments, offsets, reach depths, or driver-specific connection settings

## CNC Fields

`gantry_type` is required at the gantry YAML root and identifies the physical
machine family. Supported values are `cub` and `cub_xl`.

`factory_z_travel_mm` is required and must be greater than zero. It is the
carriage stroke available *below the homed position* for the gantry
family/workcell, and is not rewritten by calibration. Measure it by homing and
then jogging Z down to the bottom limit; do not copy the "Z working range"
printed on the machine label, which measures home to the *stock* bed surface
and diverges from the stroke as soon as the deck is raised or lowered.
Calibration uses it only to decide whether a mounted
instrument can safely reach deck bottom; calibrated `working_volume.z_max`
comes from the final homed WPos readback. Deck labware deck-frame Z values come from
calibration anchors only — `calibration.a1.z` (plates / holders / tip racks)
or `location.z` (vials / holders). The labware `height` field is the
*physical outer dimension* (rim → underside) and is not a Z shorthand;
omitting an anchor `z` raises a load-time error.

`y_axis_motion` is optional and defaults to `head`. Use `head` when the gantry
head moves along Y, and `bed` when the machine bed moves along Y.

`safe_z` is optional and defaults to `working_volume.z_max`. It is the
absolute deck-frame Z used for inter-labware travel and the entry approach
for the first well of a scan. Validation requires every resolved approach
and action Z to satisfy `z <= safe_z` so the gantry can always retract
above the deck.

## Working Volume

Working volume bounds are inclusive and use the CubOS deck frame. The
requirements below describe the default `deck_origin` policy; under
`home_origin` the maxima are `0.0` instead, with negative minima — see
[Origin Policy](#origin-policy).

Protocol setup requires (`deck_origin`, the default):

- `x_min: 0.0`
- `y_min: 0.0`
- non-negative `z_min`

Use [Calibration](calibration.md) to measure the physical working
volume. The calibration script records the first homed Z, jogs to the
front-left block/reference point, sets X/Y with `G10 L20 P1 X0 Y0`, sets the
block touch to `cnc.calibration_block_height_mm`, then re-homes to measure
X/Y bounds and the real deck-frame `z_max`. It sets GRBL `$10=0` so runtime
status reports WPos and writes the configured `$27` homing pull-off when
present.

Example A: with `factory_z_travel_mm: 110.0`, a 35 mm block, and 50 mm of
home-to-block travel, the remaining factory travel below the block is 60 mm.
The tool can reach deck bottom, so calibration writes `z_min: 0.0`,
`z_max: 85.0`, and `max_travel_z: 85.0` plus `$27`.

Example B: with the same factory travel and block height but 100 mm of
home-to-block travel, only 10 mm remains below the block. The tool cannot
reach deck bottom, so calibration writes `z_min: 25.0`, uses the final homed
readback for `z_max` (135.0 in the nominal case), and writes
`max_travel_z: 110.0` plus `$27`.

Do not mix the two ranges:

- `working_volume` is usable deck/WPos space after homing pull-off.
- `grbl_settings.max_travel_x/y/z` mirrors GRBL `$130/$131/$132` and includes
  the `$27` pull-off reserve.

If homed WPos Z is `91` and `$27=10`, save `working_volume.z_max: 91` and
`grbl_settings.max_travel_z: 101`. The extra 10 mm is controller reserve, not
usable WPos.

Multi-instrument setups need per-instrument lower-reach limits and inactive-tool
collision checks instead of one global lower reach for every tool.

## Instrument Fields

Mounted instruments live under the gantry YAML `instruments` key.

- `offset_x` and `offset_y` describe XY offsets from the gantry/router
  reference point.
- `depth` is positive tool depth below the gantry reference point; in the +Z-up
  deck frame, gantry Z is computed as target/tool Z plus `depth`.

Instrument blocks carry only physical mounting state. Labware-relative
motion heights live on the protocol command — see "Protocol Height
Fields" below. Inter-labware and first-well-entry travel use the
gantry-level `safe_z`, not any instrument field.

## Protocol Height Fields

Labware-relative offsets above the calibrated well/labware surface Z
(positive = above; negative = below) are first-class arguments to the
protocol commands that consume them:

- `measurement_height` — required on `measure` and `scan`. It is the
  action plane offset.
- `interwell_scan_height` — required on `scan`. It is the between-wells
  XY-travel offset and must be at or above `measurement_height`.

Pipette commands default to the labware reference Z (`height = 0`), and
transfer commands may set separate source/destination heights. Once a tip is
picked up, validation adds the rack `tip_length` to pipette depth for gantry
bounds checks. Each tip rack must declare `tip_length`; CubOS has no safe
default for this collision-critical dimension.
- `park_position` is an explicit rest pose (absolute coords, not relative).
- ASMI `indentation_limit_height` is a *signed* labware-relative offset
  (mm above the well surface; negative = below). It must be at or below
  `measurement_height` (descent goes down).

Legacy names `entry_travel_z`, `entry_travel_height`,
`interwell_travel_height`, `safe_approach_height`, `indentation_limit`,
and ASMI `z_limit` are rejected before motion.

## Controller Bring-Up

Axis and homing normalization is controller administration, not routine
operator calibration. Use [Gantry Bring-Up](admin/gantry-bring-up.md) when a
machine is new or controller direction/WPos behavior is unknown.

That admin procedure covers:

- controller setting snapshots and rollback notes
- `$3` jog direction invert mask
- `$10` WPos status reporting
- `$23` homing direction invert mask
- WPos/MPos/WCO checks

## Supported Gantries

| Config | System | Current status |
|--------|--------|----------------|
| `packages/core/configs/gantry/cub_xl_asmi.yaml` | CubOS-XL + ASMI | Measured deck-origin ASMI config from 2026-04-24; still requires staged hardware checks before broad reuse |
| `packages/core/configs/gantry/cub_xl_sterling.yaml` | Sterling ASMI | Sterling ASMI setup; validate on hardware before real protocols |
| `packages/core/configs/gantry/cub_filmetrics.yaml` | Cub + Filmetrics | Converted deck-origin starting point; recalibrate and hardware-validate before real Filmetrics runs |
| `packages/core/configs/gantry/cub_xl_panda.yaml` | CubOS-XL + PANDA-style mounted instruments | Estimated layout/config surface; camera/capper use registered `mount_only` vendors until real PANDA-specific drivers are supplied |
