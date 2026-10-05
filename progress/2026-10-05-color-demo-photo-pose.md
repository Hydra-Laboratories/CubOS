# Color demo photo pose

## Scope

Add an optional saved color-campaign end-of-sample carriage pose. Protocol generation resolves it through the selected camera mount calibration, then uses CubOS planner-aware `move` and `photo_pause` commands after measurement. The step settles for two seconds, emits the recorder event, and holds the stable pose for two more seconds. No direct gantry access and no homing.

## Confirmed physical input

- Pose: X 244.589, Y 144, Z 94.601 in the deck frame.
- Z equals the selected `picus120_fast.yaml` safe height.
- Live station was reported idle; this work performs no deployment or physical motion.

## Status

- Completed locally: schema, protocol generation, batch preservation, completion-event contract, and focused offline tests.
- Presentation responses expose `server_now_epoch_ms` immediately before return so the recorder can compare the server-clock capture deadline without assuming host clocks are synchronized.
- Read-only Pi snapshot: `picus120_fast.yaml` still stores camera offset X 12.0, Y -46.0, depth -87.0. The reported unsaved alignment proposal was not treated as current configuration.
- Pending: deployment and supervised physical validation.

## Deployed configuration artifacts

- Existing preset and immutable protocol backed up under `/home/cub/cubos-data/backups/pre-photo-pose-20261005/`.
- Native color-setup API generated `ade_color_matching_a8a209b0.yaml` against state 10 and saved `photo_position: [244.589, 144.0, 94.601]` in `demo_single_well_2026_10_05.yaml`.
- Saved camera mount X -21.63, Y 18.305, depth -87.0 resolves the carriage pose to camera position `[222.959, 162.305, 181.601]`; offline validation reconstructed the exact carriage endpoint and passed 32 collision-aware plans.
- `camera-only-a1-alignment-check.yaml` contains only a native camera move and color measurement at `plate.A1`; offline validation passed one collision-aware plan. It was not executed.
- Run status remained inactive and state 10 retained full 4 mL stocks, empty A4-A11 candidates, zero pending operations, and zero reconciliation items.
