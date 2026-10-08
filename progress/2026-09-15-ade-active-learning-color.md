# ADE active-learning color matching

Status: application changes are deployed to the Raspberry Pi on branch `ADE` through `5e02d8290`. Target capture and one physical campaign trial have completed. Campaign `d6ba2e4d2fa9459cad6739feb3324f06` remains stopped. Its stateless tip inventory and legacy color ROI/profile are not safe inputs for automatic continuation; do not resume or repeat trial 1. Local review-branch fixes are under offline validation and are not deployed.

## Station access and safety

- Pi repository: `/home/cub/CubOS`
- Pi Python: `/home/cub/CubOS/.venv/bin/python`
- Live configs: `/home/cub/cubos-data/configs`
- Operator service: `cubos`, normally forwarded to `http://127.0.0.1:8742/`
- Simulator service: `cubos-routing-sim`, Pi port `8760`
- Connect with `ssh -o HostKeyAlias=cubos.local cub@10.193.33.24` using the existing key.
- Do not automatically home, jog, resume, or start a physical protocol. Use CubOS for hardware access and leave the final movement action to the operator.
- The controller `$27=0.5` versus selected gantry config `homing_pull_off: 2` mismatch remains unresolved. CubOS reports the calibration warning after connection.
- The operator service was restarted after the latest deployment. The most recent API check showed no active protocol and the gantry disconnected; click **Connect** before attempting to resume.
- Immediately before that restart, the gantry reported Idle at work position `(34.803, 116.535, 66.5)`.

## Implemented and deployed

- General active-learning/DOE campaign editor with numeric parameters, protocol bindings, sequences, ordered initial design points, mixture constraints, stop limits, and EI/LCB/random acquisition.
- Matérn 5/2 optimizer support.
- Center-ROI median RGB conversion to CIE Lab plus CIEDE2000/CIE76 results.
- `measure_color` protocol command and routed camera support.
- Integrated color-matching setup: select the target well, stocks, camera, candidate wells, and read the target from the same Active Learning view. The generated protocol and complete campaign draft remain visible/editable there.
- Target Lab values and their generated protocol location are visible in the UI.
- Browser JSON Lab arrays are accepted by the strict API model.
- Fluid-state tracking is optional for campaigns. Omitting a fluid-state ID avoids routing v1's unsupported durable-resume path.
- Campaign objective extraction now accepts both result representations used by CubOS: a top-level step-result list in mock mode and a hardware result wrapper containing that list under `results`.
- A failed campaign whose last native run succeeded can now be recovered with **Resume campaign**. Recovery scores the completed run, preserves that observation, and continues at the next trial/well instead of repeating the mixture.

Relevant ADE commits, oldest to newest:

- `52cb71a` — add color-matching active-learning campaigns
- `73b662c` — integrate target setup into campaigns
- `92a68c1` — accept browser target measurements
- `ae2b843` — show saved target color
- `f73a9e4` — read campaign objectives from run results
- `8a00954` — allow campaigns without fluid-state tracking
- `472ec25` — handle wrapped hardware campaign results
- `5e02d82` — resume campaigns after recoverable scoring errors

## Current experiment state

- Generated protocol: `/home/cub/cubos-data/configs/protocol/ade_color_matching_438892e7.yaml`
- Target well: `plate.A1`
- Captured target Lab: `(67.44549955394078, -4.620077403688915, 12.330453428107212)`
- Dye stocks: red `stocks.A1`, yellow `stocks.A2`, blue `stocks.A3`
- Candidate wells: `plate.A2` through `plate.A7`, then `plate.B1` through `plate.B12`
- Mixture bounds: 50–200 µL per dye, 5 µL grid, fixed 300 µL total
- Target: minimize ΔE00 to `<= 3`
- Objective path in the UI: `11.delta_e_00`
- Campaign ID to resume: `d6ba2e4d2fa9459cad6739feb3324f06`
- Trial 1 native run: `d6ba2e4d2fa9459cad6739feb3324f06-trial-1`
- Trial 1 completed physically in `plate.A2`: red 200 µL, yellow 50 µL, blue 50 µL.
- Trial 1 measured RGB `(123, 121, 114)`, Lab `(50.804827179452985, -0.59353825092201, 4.103951696294672)`, ΔE00 `16.524261796856912`, and ΔE76 `18.99474467760282`.
- Trial 1 image: `/home/cub/.cubos/images/campaign_12/color_candidate_20260915-130401.tiff`
- The native run succeeded. The campaign stopped only while extracting the objective because real hardware returned `{..., "results": [...]}` while mock mode returned `[...]`. No trial 2 movement occurred.

Do not resume campaign `d6ba2e4d2fa9459cad6739feb3324f06`: its saved ΔE00 `16.524261796856912` came from the legacy center ROI that included background rather than a verified well-local region, and its stateless run did not durably record A1/A2/A3 as consumed. Preserve the run, image, and physical trial as history. A later operator-directed campaign must use an accepted target processing profile, a freshly reconciled durable state that records the actual stock volumes and absent tips, and candidate wells/tips that exclude the already used resources. No software action should start, resume, home, jog, or move hardware automatically.

## Local durable-state and routing review

- Color campaign drafts now carry an explicit saved source protocol and batch size. The new six-sample default compiles 18 candidate wells into three native batch runs while the trial budget and initial design continue to count individual samples. Setup and history distinguish sample/batch counts, show template-inherited transfer and mix settings, and estimate 3 dye tips per batch plus one dedicated mix tip per sample. A fresh build takes generated bindings, candidate/tip sequences, and objective path from the compiler; it preserves only matching valid operator bounds and initial recipes, plus optimizer/stop choices. The source protocol remains unchanged. Preset YAML and local draft retain the source and batch setting. `# TODO(iter): test` remains deferred by the user; no hardware was run.
- Reusable campaign presets now save, list, and load strict YAML documents under `configs/campaign/`. They retain editable campaign and color-setup fields, clear the fluid-state ID, contain no target Lab/profile/run evidence, require fresh state and target confirmation on load, and never start or resume work. The UI suppresses any previously loaded protocol Lab, blocks capture/build/start until the preset's saved gantry and deck are selected, and preserves the loaded numeric parameters, objective, optimizer/design, stop limits, and mixture constraint when a fresh target regenerates the protocol. The pending fresh-target requirement, required files, edited color fields, preset selection, and current campaign draft survive browser reload; stale target evidence is cleared, Validate/Start remain disabled with the next action stated, and old selected campaigns are labeled as saved history instead of the active draft. For workspaces saved by the earlier UI, a restored rejected target is migrated into the same pending gate so reload cannot expose the invalid base-protocol validation path. `# TODO(iter): test` remains deferred for this quick iteration.
- Camera-only XY alignment now has a preview/save API contract and a dedicated panel at the top of the Gantry tab. The operator explicitly starts the shared live preview, manually jogs with the existing Gantry Control, selects a calibrated deck reference and camera, reviews the fresh frame plus current head/reference coordinates and before/after offsets, confirms centering, and explicitly saves. The server requires the named monitor to be running with a frame no more than three seconds old, computes offsets from the saved deck target and current Idle work position, then changes only the selected camera's `offset_x`/`offset_y` after optimistic file and position checks. It performs no motion, preserves round-trip YAML content, refreshes the connected runtime, and continues to expose the existing `$27` calibration warning. Saved offsets apply to new captures and new campaigns; existing campaigns retain their immutable gantry snapshots. `# TODO(iter): test` remains intentionally deferred for this quick hardware iteration at the user's request.
- Routed protocols can now read authoritative tip occupancy from a durable fluid state before constructing the immutable motion plans. Consumed tips are removed from collision scenes across native trials; available tips remain obstacles and pickup candidates.
- New real fluid-handling campaigns require a durable fluid-state ID. State creation accepts explicit physical tip-presence overrides, and color-campaign generation allocates tip triplets from the durable available order rather than restarting at A1.
- A stopped legacy campaign can be bound to a newly created, deck-compatible state only with a stored physical-reconciliation note. Binding preserves trials and never starts a run. Legacy color results without accepted quality and exact processing-profile provenance remain unverified and cannot become optimizer observations.
- Optional `image_height` is labware-relative. When supplied, routing preplans the checked camera descent and checked return to the configured planning ceiling; omission preserves the existing ceiling capture behavior.
- Target review can serve a browser-compatible rendering of the immutable saved frame and reanalyze that same trusted image after an operator-selected normalized well center. Target completion first verifies the camera-provided capture-time SHA-256 and freezes the exact TIFF in the run artifact store. Reanalysis does not acquire a camera or move the gantry, rechecks that digest, and persists serialized SHA-tagged analysis revisions separately from the original run result. Real campaign preparation rechecks the frozen bytes and requires the selected analysis revision to name the same capture digest before it accepts the target.
- OpenCV cameras now use one continuously drained, reference-counted reader per device. Protocol captures wait for a newer frame without consuming the live preview, and every saved frame records host receipt time, actual dimensions/format/settings, a configuration revision, and a SHA-256 of the exact written bytes. Live preview requires explicit per-view leases with heartbeats and expiry; stopping one view does not stop another or a protocol lease, and abandoned views release automatically. Optical-control writes are explicit, report unknown/unsupported support honestly, and are blocked during runs and campaign reservations.
- Offline checks currently pass: 96 focused campaign/state API tests, 195 focused core routing/camera/color/pipette/setup tests, 363 broader core state/planning tests, and 383 API tests with 2 skipped when the sandbox-incompatible deployment-script module is excluded. Camera-backend validation additionally passed all 604 core instrument tests, 106 focused core camera/color/routing tests, 97 integrated monitor/run/campaign API tests, and 85 API monitor/security/lifespan tests. Five deployment-script tests remain blocked by the local `/dev/fd` sandbox. Physical camera validation remains pending; nothing in this local phase was deployed or run on hardware.

## Validation and backups

Earlier focused validation before hardware iteration:

- 53 core optimizer/color/camera-command tests passed.
- 46 focused API campaign/route tests passed.
- 7 campaign UI tests passed; TypeScript, ESLint, and the production build passed.
- The station candidate protocol passed setup validation with 33 targets and 32 immutable routing plans.
- The 18-trial campaign passed server preflight in offline mode.

For the final fluid-state and result-recovery fixes, tests were deferred at the user's explicit request. The latest operator production build completed successfully on the Pi. Add regression coverage for wrapped hardware results and failed-campaign recovery before preparing a PR.

Pi code backups made before deployment:

- `/home/cub/cubos-backups/2026-09-15-before-stateless-campaign/code.bundle`
- `/home/cub/cubos-backups/2026-09-15-before-objective-wrapper/code.bundle`
- `/home/cub/cubos-backups/2026-09-15-before-campaign-resume/code.bundle`

The local untracked `services/api/configs/deck/cub_deck.yaml` and `work/` directory predate the final changes and were intentionally left untouched.

## Campaign UI audit follow-up

Reorganized the operator flow into Setup, Run, and History, with a single next-action area, ordered setup steps, collapsed advanced settings, view-specific status, and a collapsible Station sidebar. Reworked saved-frame review to explain selection versus image-quality problems and retain rejected evidence. Inventory now has full rack enumeration, draft-only bulk tip controls, visible volume labels, and explicit confirmation before creating a record. API failures no longer appear as missing configurations or inventory.

Fixed stale target readiness after target/camera/height/ROI/stock/candidate edits, saved-frame point changes, and reloads. Existing recovery and immutable history remain available without automatic execution. Target-image keyboard events are isolated from gantry shortcuts.

Validation: production frontend build and lint pass; source review and manual local-browser checks cover preset load, synthetic capture/reanalysis, inventory selection, generated protocol, validation failure, live synthetic camera/status, and History separation. No physical motion or real campaign was started. The isolated geometry fixture still rejects a full transfer route; this is not physical validation. Pi deployment is pending because the network connection is unavailable.

TODO(iter): add deferred regression coverage for the guided views, readiness invalidation after edits/reload, rejected-image restoration, history recovery, API errors, keyboard isolation, partial tip definitions, and inventory confirmation. Automated tests remain deferred at the user's request.
