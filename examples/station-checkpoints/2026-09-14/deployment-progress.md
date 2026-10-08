# CubOS Pi routing update

## Authorized outcome

Reconnect to the existing CubOS Pi, add the routing and solid-rendering changes, and serve Operator and the updated simulator for review. The user explicitly authorized the software update. No homing, jogging, pipette actuation, protocol execution or automatic motion recovery is part of this deployment.

## Observed starting state

- Pi reachable at `10.193.33.24`; use the already trusted `cubos.local` SSH host key when connecting by IP because mDNS is intermittent.
- `/home/cub/CubOS` is clean on `feat/operator-active-learning`, commit `f95fbed84b231839fa7cf12a610978c899efdce6`.
- Operator runs as system service `cubos` on port8742. Gantry disconnected, no active protocol, no campaigns.
- Calibrated configs are outside Git at `/home/cub/cubos-data/configs`; local backup is `CubOS-local-configs/pi-deploy-2026-09-14/before/configs`.
- Gantry USB serial device and cameras enumerate. Picus serial device currently does not enumerate.
- Calibrated carriage Z range is10.5–66.5mm (56mm travel); original config still has safe_z56.0. Preserve the original file; a separate routing-review configuration may use the calibrated ceiling66.5.

## Deployment design

1. Merge the tested routing branch `931f5a67` into an isolated deployment branch based on the exact Pi revision, retaining active learning and calibration fixes. Keep the feature branches separate.
2. Validate merged code and build both frontends locally. Prepare new review YAMLs from the live calibrated files, with explicit nominal geometry, leaving original configurations untouched.
3. Back up Pi configs, service configuration, code revision and installed dependency list. Transfer the reviewed merge and built assets; update only after a fresh idle check. Keep a rollback revision.
4. Restart Operator without auto-resume. Serve the simulator separately as simulation-only. Reconnect the gantry through the existing CubOS endpoint only if the audited connect path performs no movement or recovery commands.
5. Verify health, version, saved-calibration preservation, routing metadata and browser rendering. Clearly report missing Picus connectivity and the existing physical-validation limits.

## Known limits

Routing v1 rejects durable fluid-state execution/resume and unsupported commands. It must not be advertised as ready for an unattended physical active-learning campaign. New scene/tool geometry remains nominal until measured and validated on the hardware. The rendered CAD frame also has a documented handedness/registration caveat from the offline work. Installing the code does not establish physical collision safety.

## Status

Complete. Deployed and pushed `deploy/cubos-pi-routing-2026-09-14` at `52c7fc740c7bb43f79d8d2c66879a2f28f71fe8f`. Merge parents preserve the exact prior Pi build and the routing feature branch. Active-learning production code remains byte-identical to the prior Pi version; a flaky time-budget test was made deterministic without changing execution policy.

## Verification and handoff

- Merged offline checks: core 2,646 passed/17 skipped/14 subtests; API 335 passed/2 skipped excluding the five known sandbox `/dev/fd` updater tests; SDK18 passed; Operator342 passed and built; simulator44 Python tests,3 renderer tests and build passed.
- Pi editable packages refreshed without changing runtime dependencies. Both `cubos` (8742) and `cubos-routing-sim` (8760) are active. Simulator is an enabled system service running as `cub` with private devices/temp space; it has no access to the physical gantry or pipette devices.
- The normal updater now follows the pushed deployment branch via `/etc/systemd/system/cubos.service.d/routing-branch.conf`, and reports current/latest SHA52c7fc74 with no pending update.
- Original live `cub_deck.yaml` and `picus1000_arducam_usb_seed.yaml` match their backups by SHA-256. Original gantry SHA is `150cf905e278848e6f556f9da873d33fe3967c9ecd014159831ed740837273dd`; original deck remains `db518dbb124aa0c3c95ece7692a66fa80370c48af536557add067f29977e4cc4`.
- New review files were added alongside originals. The freshly plugged Picus identifies as `47683878`, now used in `picus1000_routing_review.yaml`; the old file remains unchanged.
- Pi offline validation of protocol03 passed with31 immutable plans. All three simulator runs were exported on the Pi to `/home/cub/cubos-data/backups/routing-2026-09-14/pi-simulation-evidence`.
- Reconnected the gantry only through CubOS. It reports Idle at `(258.205,144.645,66.5)` with a calibration warning: controller `$27=0.5`, configured expectation2.0. No homing, movement, recovery, instrument connection/actuation or physical protocol was performed. This discrepancy remains for operator calibration.
- Browser verified live Operator and Pi-hosted solid-rendering simulator. Selected Single protocol, the new gantry/deck/protocol03 review files, and Visualize in Operator; did not save over originals or run a protocol. Both tabs are left open.

URLs: `http://10.193.33.24:8742/` (live Operator), `http://10.193.33.24:8760/?profile=saved-side-exit` (simulation only).

Rollback material is under `/home/cub/cubos-data/backups/routing-2026-09-14`: prior revision, dependency list, service definition, original configs/settings, and Operator build. Before rollback, verify idle/disconnect, stop both services, restore code to `f95fbed84b231839fa7cf12a610978c899efdce6`, refresh editable packages, restore `operator-dist.tar.gz`, remove the deployment branch drop-in, reload systemd and restart only Operator. Use the original setup filenames after rollback; new review YAMLs require routing-aware code. Never resume motion automatically.

## Pipette-port follow-up

The user subsequently reported a missing serial47683878 device. A fresh USB inventory identified serial47684951 on `/dev/ttyACM0`; the by-id47683878 path was absent. Backed up the current review gantry under `/home/cub/cubos-data/backups/pipette-port-2026-09-14`, changed its pipette port to `/dev/serial/by-id/usb-Sartorius_Picus_2_47684951-if00`, and saved the same correction in Operator. Verified the symlink resolves to the present device. The user's saved 2000mm/min feed rate was preserved; no protocol or instrument actuation was retried. Separately observed a new settings mismatch: saved steps/mm2000 on all axes versus controller800; reported this as a calibration issue, not a speed control, without programming the controller.

On the user's subsequent “fix it” instruction, read live GRBL through CubOS and verified `$100/$101/$102=800`. Backed up the current YAML under `steps-calibration-2026-09-14`, restored only the three saved steps/mm values to800 through Operator, and reconnected the idle gantry. Verified saved feed rate remains2000mm/min, corrected pipette port remains47684951, and the only remaining settings warning is `$27` (expected2, actual0.5). No motion or GRBL calibration-setting write was performed.

## Camera recovery

The user reported `CameraConnectionError` at index0. Verified the Arducam IMX323 was present as `/dev/video0` (MJPG capture), permissions included the video group, and no other process held the video devices. Kernel logs showed repeated UVC USB protocol errors `-71`; CubOS's camera driver reproduced the open failure. After confirming no active protocol, reset only USB device `0c45:6366` with `usbreset`. The same CubOS `OpenCVCamera(camera_id=0)` then opened successfully, captured `/home/cub/cubos-data/camera-check.png`, and disconnected. No configuration change, gantry motion, or pipette actuation was required.

## Protocol 03 photos

User requested an A1 photo after each dispense. Add three `move(camera, plate.A1)` + `capture` pairs after the three 100 µL transfers, retaining the subsequent mixing step. Use existing capture image naming/persistence with labels `A1_after_dispense_1/2/3`. The routing engine currently rejects capture, so a GPT-5.6 subagent is adding bounded stationary-camera support and testing instrument handoff: withdraw the previously engaged pipette through its scoped access before aligning the camera. Camera alignment uses the configured travel ceiling rather than an invented focal height. Backups and proposed YAML live in `CubOS-local-configs/protocol03-camera-2026-09-14`. Validate offline, deploy to an idle station, and do not run the physical protocol automatically.
