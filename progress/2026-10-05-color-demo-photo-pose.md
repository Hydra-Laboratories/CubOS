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
