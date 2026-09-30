# Calibration

Use `packages/core/src/cubos/tools/calibrate_gantry.py` for gantry calibration. It reads one gantry
YAML file, detects the mounted instruments, selects the single- or
multi-instrument flow, and writes calibrated values back to YAML.

Calibration moves hardware and can update work coordinates and GRBL soft-limit
travel settings. Keep the E-stop reachable and clear the deck before starting.

## Before You Start

1. Turn on the gantry and controller.
2. Connect the gantry to the computer over USB/serial.
3. Make sure mounted instruments, cables, fixtures, and samples have clear
   travel paths.
4. Confirm the gantry YAML lists the instruments that are physically
   mounted (see [Set Up Gantry YAML](gantry-setup.md)).
5. Confirm the gantry YAML matches your machine — a CUB seed on a CUB XL
   (or vice versa) calibrates a wrong working volume (see
   [CUB vs CUB XL Seeds](gantry-setup.md#cub-vs-cub-xl-seeds)).
6. Choose a calibration reference and put it within reach — see
   [Calibration Reference Options](#calibration-reference-options).

![Calibration block aligned to a board placement marker](images/calibration-block-marker.webp){ width="420" }

## Calibration Reference Options

Calibration needs two things from a physical reference:

- an **XY origin point** — a repeatable spot at the front-left of your working
  area that becomes deck `(0, 0)`
- a **Z reference height** — the known height above the deck of the surface
  you touch, so calibration can infer where deck `Z = 0` is

Anything that provides both works. Either option produces the same
calibration — the same values get saved to the gantry YAML no matter which
you use.

### Option A — Calibration Block (recommended)

Use the Ursa calibration block — the printable model lives in
[Cubware `mounts/calibration`](https://github.com/Ursa-Laboratories/Cubware/tree/main/mounts/calibration) —
placed at the front-left origin mark. Its height is known and its flat top is
easy to touch consistently. For multi-instrument calibration, where every
instrument must touch the same physical point, always use the block (or
another rigid, flat-topped reference every instrument can reach).

### Option B — Deck Feature of Known Height

Without a block, use a rigid feature already on the deck. For example, with a
well plate: use the front-left-most (corner) well your protocols address as
the XY origin, and the height from the deck to the top of the plate as the Z
reference height.

![Annotated well plate calibration reference showing the XY origin and Z reference height](images/calibration-well-plate-reference.webp){ width="520" }

![Annotated close-up of a probe touching the selected well plate reference surface](images/calibration-well-plate-touch.webp){ width="520" }

Two things must hold for this to be reliable:

- **The XY point you pick becomes deck `(0, 0)`.** Deck YAML positions must be
  measured from that same point, and the object must sit repeatably (in a
  holder or against a fence). If the plate shifts, the calibration is wrong.
- **Measure the height, don't look it up.** The reference height directly sets
  deck `Z = 0`. Measure deck-to-plate-top with calipers; a labware datasheet
  height is wrong as soon as the plate sits in a holder.

!!! warning "Close other GRBL software first"
    Close Candle, Universal Gcode Sender (UGS), or any other GRBL sender
    before running calibration. A serial port can only be held open by one
    program at a time — if another program still has it open, calibration
    will fail to connect, or lose control of the gantry mid-run.

## Origin Policy

Gantry YAML's top-level `origin_policy` (`deck_origin` default, or
`home_origin`) decides which corner calibration assigns as zero — see
[Gantry: Origin Policy](gantry.md#origin-policy) for the YAML field and
working-volume invariants.

- **`deck_origin`** (default, described above) — calibration assigns zero at
  the front-left origin point you touch and writes `working_volume` minima of
  `0.0` (`x_min`, `y_min`, non-negative `z_min`); the Z reference height and
  homed readback set the maxima.
- **`home_origin`** — calibration instead assigns zero at the homed
  back-right-top corner. `working_volume` maxima come out `0.0` (`x_max`,
  `y_max`, `z_max`) with negative minima describing the reachable workspace.

Both flows still need a physical XY/Z reference point to size the volume;
only which corner receives value `0` changes. GRBL soft-limit travel settings
(`max_travel_x/y/z`) are span-based and unaffected by the policy.

## Run Calibration

To calibrate in place, run:

- **macOS / Linux / Windows (Git Bash):**
  ```bash
  python -m cubos.tools.calibrate_gantry packages/core/configs/gantry/cub_xl_asmi.yaml
  ```
- **Windows (PowerShell):**
  ```powershell
  $env:PYTHONPATH = "src"
  python -m cubos.tools.calibrate_gantry packages/core/configs/gantry/cub_xl_asmi.yaml
  ```

The script asks before overwriting the input file. To write a calibrated copy:

- **macOS / Linux / Windows (Git Bash):**
  ```bash
  python -m cubos.tools.calibrate_gantry \
    packages/core/configs/gantry/cub_xl_sterling_3_instrument.yaml \
    --output-gantry packages/core/configs/gantry/cub_xl_sterling_3_instrument_calibrated.yaml
  ```
- **Windows (PowerShell):**
  ```powershell
  $env:PYTHONPATH = "src"
  python -m cubos.tools.calibrate_gantry `
    packages/core/configs/gantry/cub_xl_sterling_3_instrument.yaml `
    --output-gantry packages/core/configs/gantry/cub_xl_sterling_3_instrument_calibrated.yaml
  ```

The preflight shows the input file, output file, detected instruments, and the
chosen flow before it connects to hardware.

## Soft Limits Are Off During Calibration

Calibration measures the machine's real travel, so the stored GRBL soft
limits (`$20`) cannot be trusted yet — CubOS disables them when calibration
starts and re-programs them from the measured spans when it finishes (or
restores them if you abort). **While calibrating, the only backstop is the
physical limit switches** — so CubOS also enables GRBL hard limits (`$21`)
for the calibration window, even on controllers that normally run with
them off. Without that, a jog past a travel end grinds against the frame
and silently skips steps, corrupting the calibration being taken. When
calibration finishes or is aborted, `$21` is restored to its prior value —
unless the gantry YAML's `grbl_settings` sets `hard_limits: true`, in
which case it stays enabled. Jog with small steps as you approach the
edges of travel.

If a jog does trip a hard-limit switch, CubOS soft-resets, unlocks GRBL,
and attempts a small pull-off opposite the failed jog direction. If
recovery fails, or the machine ends up stuck on a switch after a power
cycle, see
[Stuck on a Limit Switch](troubleshooting.md#stuck-on-a-limit-switch).

## Jog Controls

During calibration:

- arrow keys jog X/Y
- `X` jogs `+Z` up
- `Z` jogs `-Z` down
- number keys change step size
- Space cancels any active jog
- Enter confirms the current step
- `Q` aborts

!!! warning "Single presses only — do not hold jog keys down"
    Press a jog key once and wait for the move to finish before pressing
    again. Repeated keypresses are batched into one larger jog command, so
    holding a key can still overshoot past where you meant to stop, especially
    at 25 mm or larger step sizes. Press Space to cancel an active jog.

If a jog trips a hard limit, CubOS soft-resets, unlocks GRBL, and attempts a
small pull-off opposite the failed jog direction. If recovery fails, stop and
follow [Stuck on a Limit Switch](troubleshooting.md#stuck-on-a-limit-switch).

## Single-Instrument Calibration

Use this flow when the gantry YAML has one mounted instrument.

1. Start `packages/core/src/cubos/tools/calibrate_gantry.py`.
2. Confirm the single-instrument flow in the preflight.
3. Place your calibration reference at the front-left origin point — the
   calibration block, or a deck feature such as a plate's corner-most well
   (see [Calibration Reference Options](#calibration-reference-options)).
4. Jog the instrument tip or probe until it touches the top of the reference
   surface (block top or plate top).
5. Confirm the touch point when prompted, and enter the reference height
   above the deck if the script asks for it.
6. Let the script home and measure the usable work volume.
7. Review the summary and calibrated YAML path.

![Single instrument touching the calibration block](images/single-instrument-calibration-block.webp){ width="420" }

The script writes everything it measured back to the gantry YAML — no
manual edits needed.

## Multi-Instrument Calibration

Use this flow when the gantry YAML has more than one mounted instrument.
Every instrument must touch the same physical point, so use the calibration
block (or another rigid, flat-topped reference all instruments can reach)
rather than a deck feature.

If more than one CUB controller is connected, disconnect the controllers you are
not calibrating before starting. Calibration still uses auto-scan for the gantry
connection and has no `--port` flag.

![Board placement markers used during calibration](images/calibration-marks.webp){ width="520" }

1. Start `packages/core/src/cubos/tools/calibrate_gantry.py`.
2. Confirm the multi-instrument flow in the preflight.
3. Select the leftmost/reference instrument when prompted.
4. Enter the calibration block height when prompted.
5. Place the first calibration block on the leftmost board mark for the red
   reference instrument.

   ![Red reference instrument touching the block at the leftmost mark](images/leftmost-red-instrument-block.webp){ width="420" }

6. Jog the red reference instrument over the first block's X/Y mark. This step
   sets only X=0 and Y=0.
7. After the script homes and moves to the measured X/Y center, move the
   calibration block to the center board mark.

   ![Calibration block moved to the center board mark](images/center-calibration-block.webp){ width="520" }

8. Attach or verify all mounted instruments, then select the lowest mounted
   contact instrument when prompted.
9. Jog the lowest instrument to touch the shared center reference point. This
   touch sets Z from the block height and records that instrument's X/Y/Z block
   coordinate.
10. Jog each remaining contact instrument to the same center reference point as
   prompted.
11. For an RPi camera, center the camera over the same block mark, press Enter,
   then measure and enter the height from the calibration block top to the
   camera reference point. Lighting instruments have no step of their own —
   they are co-mounted with the camera and reuse its calibration.
12. For pipette setups, jog the pipette to the center reference point on the
   block when prompted.
13. Let the script compute each instrument's offsets.
14. Review the summary and calibrated YAML path.

## Interactive Jog Test

After calibration, run the interactive deck-frame jog check:

```bash
python -m cubos.tools.hello_world \
  --gantry packages/core/configs/gantry/cub_xl_asmi.yaml
```

Expected directions:

- `+X` moves right
- `+Y` moves back, away from the operator
- `+Z` moves up
- `-Z` moves down

Once calibration and the jog test are complete, continue to
[Set Up Deck and Labware](deck.md).
