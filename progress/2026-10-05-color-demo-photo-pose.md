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

## Final deployed readiness

- Pi deployment completed at commit `91c7cdcf`, including the backend photo-pose/event contract and frontend recording fallback. Deployment backups were preserved by the deployment workflow.
- In Operator on localhost, the Oct. 5 preset was loaded, state 10 was bound, and the final UI-generated protocol `ade_color_matching_7caacd83.yaml` was built, validated, and saved. Read-only tail inspection confirmed the photo move uses `[222.959, 162.305, 181.601]`, which resolves to carriage pose `[244.589, 144.0, 94.601]`, followed by a two-second settle and two-second capture hold for A4.
- Brave at `http://localhost:8742/?view=demo` displayed the MX Brio whole frame at 1920x1080. A short local Record next attempt / Stop test saved `Downloads/cubos-unassociated-campaign-2026-10-05T21-39-09.352Z.webm` (3,585,600 bytes) and its 490-byte sync JSON. Saved-recording recovery showed one recording.
- Screenshot evidence: `outputs/color-demo-2026-10-05/live-stream-ready.png` and `outputs/color-demo-2026-10-05/photo-campaign-ready.jpg`.
- No physical protocol was started. `camera-only-a1-alignment-check.yaml` remains an offline-validated optional operator physical test.

## Six-trial burnt-orange setup and autonomy audit

- Native color setup created preset `demo_six_trial_burnt_orange_2026_10_05.yaml` and immutable protocol `ade_color_matching_98710e2c.yaml` for selected RGB `#A84300` (168, 67, 0), wells B1-B6, batch size 1, three exact R/Y/B-dominant initial recipes, and three EI opportunities. Stop conditions are max 6 and target value 2.0; current campaign semantics stop at objective <= 2. Source height -40, mix 60 µL x3 at -7, and the calibrated photo pose are preserved.
- API validation passed. Offline validation passed 28 motion targets, semantics, and 32 collision-aware plans. Run status remained inactive; state 10 B1-B6 remained empty with zero pending operations and zero reconciliation items.
- `cubos.service` is active and enabled, runs as `cub`, and restarts on failure after three seconds. Campaign work runs in a server-owned daemon thread, so closing the browser or unplugging the Mac does not cancel the Pi campaign. A service/Pi restart does not resume automatically: startup marks nonterminal campaigns interrupted with `server_restart` for operator inspection.
- Tailscale and Codex executables were absent. Raspberry Pi Connect is installed but not running; its user service is inactive/dead and user lingering is disabled. No remote-access software was installed, enabled, signed in, or exposed. Mac-local camera recording cannot continue after the Mac is unplugged.
