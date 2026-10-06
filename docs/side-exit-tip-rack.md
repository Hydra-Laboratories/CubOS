# 1000 µL side-exit tip rack

A long tip cannot clear this open-ended rack by retracting vertically within the available 56 mm Z travel. CubOS supports an optional rack pickup path: approach above the slot, engage, lift at least 30 mm, then withdraw along X at unchanged Y and Z.

Add this to a calibrated `type: tip_rack` deck entry:

```yaml
tip_length: 70       # attached extension below the bare nozzle, mm
side_exit:
  lift_mm: 30        # physical lift from the engagement position; minimum 30 mm
  exit_x: 140        # absolute tool X beyond the rack opening, including clearance
```

These numbers describe the simulator example. Measure your own tip extension and exit coordinate before using a real machine. `exit_x` is in the gantry config's active frame, not an offset or a CAD coordinate. Either X direction is supported. The rack must be installed with its open channels along X; this field does not rotate the rack or certify its collision clearance.

`pick_up_tip: {position: tips.A12}` selects a slot. Remove tips from the open edge inward so the attached tip cannot hit another loaded tip in the same lane. Untracked runtime rack selection (`position: tips`) chooses an unblocked available tip; durable tracking requires an explicit slot; offline protocol validation continues to require explicit slots, as before. Ordinary racks without `side_exit` keep the normal pickup behavior.

The side-exit approach plane is `slot.z + lift_mm` for the bare nozzle. After engagement, the attached tip's end is lower by `tip_length`, so withdrawal targets are `slot.z + lift_mm - tip_length`. CubOS updates tool depth before withdrawal. This produces exactly the configured carriage lift, not lift plus tip length. Runtime checks all four carriage endpoints before moving; setup validation checks those same endpoints and known fixed structures. A failed withdrawal propagates the error and retains attached-tip/consumed-slot state. Durable tracking marks uncertain pickup operations for reconciliation.

After exiting, the next path must stay outside the rack until it is safe to travel toward the destination. The example first moves along Y at X140 into a clear corridor, then transfers liquid. The normal travel height alone cannot clear other loaded 70 mm tips in this 56 mm envelope. No automatic rack-return path is provided; the example discards into a separate virtual waste receptacle.

## A1 and rack geometry

The simulator rotates the rack 180° in place so the open end faces inward (-X), toward the vials. Slot identities rotate with the physical rack. A1 is a slot center, separate from the lower corner of the rack's outer box.

```yaml
location: {x: 150, y: 20, z: 0}
length: 120
width: 84
height: 63
rows: 8
columns: 12
calibration:
  a1: {x: 263, y: 26, z: 70}
  a2: {x: 253, y: 26, z: 70}
x_offset: 10
y_offset: 10
pickup_z: 70
```

All dimensions are millimeters. A2 defines the adjacent column direction: columns run in -X, and the default row direction then runs in +Y. Thus A12 is (153,26,70) and H12 is (153,96,70). The exposed tip nearest the opening is A12, so the demo consumes A12, A11, A10 before progressing to the next row. A1 and A12 are labeled in the 3D scene.

`location` plus `length`/`width`/`height` describe the placed outer box; the 8×12 calibrated grid describes addressable slot centers. `pickup_z` is the bare-nozzle engagement plane, not the rack's outer height or the attached tip's end. The STL supplies the visual walls and slots, but CubOS does not infer calibration or motion from the mesh at runtime. The nominal 10 mm grid and outer dimensions came from CAD inspection; deck placement and engagement Z remain simulation estimates.

## Watch the offline example

From the CubOS root:

```sh
npm run build --prefix apps/color-twin
PYTHONPATH=packages/core/src:apps/color-twin python -m uvicorn sim.server:app --host 127.0.0.1 --port 8753
```

Open [the side-exit simulator](http://127.0.0.1:8753/?profile=side-exit). The initial 14-step native CubOS protocol picks three fresh tips, transfers red/yellow/blue (100 µL each) into A1, mixes, discards tips and captures a synthetic camera observation. Choose **Rack close-up**, then **Review first pickup at 1×**. **A · Lifted 30 mm** and **B · Exited −X** pause at the relevant endpoints. Press Play to watch the entire protocol, or Run BO campaign to fill additional wells.

The exact inputs are in `apps/color-twin/examples/side-exit/`. The simulator renders the original `ColorMatching_TipHolder.stl` from [the requested Cubware fork](https://github.com/adediredaniel/Cubware/tree/8072cbe5161d8888661d9e80451d763da57efeef/cub/instrument_mounts/CAD_ColorMatchingDemo). Its upright dimensions are 120 × 84 × 63 mm; the example uses CAD-derived 10 mm slot pitch. Calibration, engagement Z and mounted extension remain unverified. The simulation-only 1000 µL adapter does not add a production pipette driver.

## Hardware validation status

No real gantry, pipette or camera was connected or actuated. This is a geometric/protocol simulation, not a GRBL firmware run or a collision-certified digital twin. Offline coverage includes ordered lift/X motion, signed offsets, bounds rejection, both exit directions, blocked lanes, failure state, fluid conservation, a 1000 µL stroke and multi-trial replay.

An operator must measure the usable Z range, bare nozzle depth, attached tip extension, slot positions, engagement height and exit corridor. With the deck clear of liquids, supervise one pickup at the exposed edge: verify seating, at least 30 mm of lift with no X/Y travel, then X-only withdrawal until the tip is outside the rack. Verify the Y corridor before a small water transfer to a calibrated destination and tip discard. Repeat with the next inward slot. Record the physical outcomes before merging hardware-facing changes.
