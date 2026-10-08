# Picus routing checkpoint — 2026-09-14

This checkpoint preserves the current station YAMLs and the work still waiting
to be deployed. These are machine-specific snapshots, not universal presets.

## What is saved

- `live-configs/`: a fresh copy of `/home/cub/cubos-data/configs` from the Pi.
  It includes the active working deck, routing-review setup, original seed
  files, protocols 01–04, and setup notes. Keep the originals as provenance;
  older notes inside that folder describe earlier deployment stages.
- `proposed/03_routing_side_exit_with_photos.yaml`: protocol 03 with three
  camera moves and captures, immediately after its three 100 µL transfers.
  It preserves the user's latest **−20 mm source heights**. The subsequent
  mixing step is retained.
- `proposed/validation.txt`: successful offline validation of that proposed
  protocol against the current saved working deck and gantry.
- `observed-device-status.json`: read-only controller and connection snapshot.
- `routing-design-and-validation.md` and `deployment-progress.md`: the design,
  test evidence, deployment history, fixes, and remaining limitations.

## Code versus deployed state

| Item | State at this checkpoint |
|---|---|
| Pi code | `52c7fc740c7bb43f79d8d2c66879a2f28f71fe8f` |
| Latest code included in this branch | `da575c5a8cc493be945f1ff33f37d9b6e6415bc4` plus this checkpoint |
| Routing and solid-model simulator | Deployed |
| Camera USB recovery | Completed; CubOS captured a diagnostic image at index 0 |
| Routed camera-command support | Committed and pushed, **not deployed on the Pi** |
| Three-photo protocol 03 | **Proposed only**, not installed on the Pi |
| Active-learning tab | Deployed; offline/mock workflows supported |
| Physical color-matching campaign | Not ready: routing/durable-state integration, numeric image scoring, and physical validation remain |

The attempted camera-step deployment stopped at its source-file guard before
stopping services or changing Pi code. It detected the user's newer −20 mm
source-height edit. That edit has now been preserved in the proposed YAML.

## Current setup

- Live Operator: `http://10.193.33.24:8742/`
- Separate simulation: `http://10.193.33.24:8760/`
- Gantry review file: `live-configs/gantry/picus1000_routing_review.yaml`
- Latest working deck: `live-configs/deck/cub_deck.yaml`
- Carriage Z range: 10.5–66.5 mm, representing 56 mm travel.
- Saved movement feed: 2000 mm/min. Axis calibration: 800 steps/mm.
- Picus USB identity: `47684951`; use the saved by-id path rather than assuming
  `/dev/ttyACM0` remains stable.
- Camera: Arducam IMX323, OpenCV index 0. Its earlier connection error was a
  stalled USB interface; a camera-only USB reset restored capture.
- At snapshot time, the gantry was connected and Idle at `(230,129,66.5)`;
  no protocol was active.
- A calibration warning remains: controller homing pull-off `$27=0.5 mm`
  versus the saved expectation of 2 mm. Do not change steps/mm to change speed.

## Photo behavior and validation

Each added pair aligns the camera over `plate.A1` at the carriage travel ceiling
and then takes a non-moving capture. Labels are `A1_after_dispense_1`,
`A1_after_dispense_2`, and `A1_after_dispense_3`. Physical images use the existing
timestamped image paths under `~/.cubos/images/campaign_<id>/`, unless overridden
by `CUBOS_IMAGES_DIR`.

The new code preflights camera capture and withdraws the previously engaged
pipette through its own access corridor before moving the camera. It fixes
planned camera-move validation without changing legacy move semantics.

Checks completed for this addition: 2,658 core tests passed, 17 skipped,
14 subtests passed; changed-core coverage was 94%. Simulator tests verified
three A1 captures at 100, 200, and 300 µL and exact plan/execution parity.
The proposed YAML with the latest −20 mm depths passes offline validation:
16 steps, 35 motion targets, 34 immutable plans. No physical photo protocol was
run by the agent. A validator PASS does not establish physical calibration,
focus, field of view, or collision clearance.

From the repository root, with its environment active:

```sh
python -m cubos.tools.validate_setup \
  examples/station-checkpoints/2026-09-14/live-configs/gantry/picus1000_routing_review.yaml \
  examples/station-checkpoints/2026-09-14/live-configs/deck/cub_deck.yaml \
  examples/station-checkpoints/2026-09-14/proposed/03_routing_side_exit_with_photos.yaml
```

Before resuming deployment, compare the live files again, preserve any newer
operator edits, verify the station is idle, and back up its current revision
and configs. Deploy the camera-support code before installing the proposed
protocol. Do not automatically home, resume, or execute a physical protocol.
