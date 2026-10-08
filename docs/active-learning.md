# Active-learning campaigns

Choose **Workflow → Active learning campaign** to optimize numeric protocol arguments over repeated native CubOS runs. **Single protocol** retains the existing one-run workflow. Gantry, Deck, Visualize, State and Results continue to use the same operator workspace; each campaign trial also opens in the existing Run view.

## Configure a campaign

1. Select and save your calibrated gantry and working deck in the existing tabs. In campaign mode, select a saved **Protocol template**.
2. Add named parameters with a minimum, maximum, positive step size and bindings to numeric protocol arguments. One parameter can control several arguments. Steps are displayed starting at 1; saved JSON uses zero-based indexes. Nested dotted argument paths are supported by the API.
3. For repeated liquid handling, add target sequences for fresh tip slots and destination wells. Bind the same destination sequence to every step that uses that trial's well. Each sequence must cover the trial budget; pickup targets must be distinct across the entire campaign. Do not repeat a tip or silently reuse a filled well.
4. Optionally select a group of parameters and a fixed sum, for example red/yellow/blue volumes totaling 300 µL.
5. Choose expected improvement (EI), lower confidence bound (LCB), or the seeded random baseline. Set initial random trials, exploration and random seed. The GP uses a fixed RBF kernel; this is a bounded first implementation, not a substitute for experimental-design validation.
6. Choose a numeric result path and minimize/maximize direction, or manual observations. Paths are relative to `RunRecord.result`: for example `results.0.thickness_nm` for a hardware run result, or `0.thickness_nm` for the offline example's result list. Missing, nonnumeric or nonfinite values stop the campaign before another trial. A camera image is not a numeric score; use a measurement/analysis step that returns a number or enter the observation explicitly.
7. Choose trial budget, optional target value, patience with minimum improvement, and optional wall-time limit. Time limits are checked **between trials**; they do not cut a liquid-handling step short.
8. Select offline mock or real hardware explicitly. Real fluid-handling campaigns require an existing **fluid state ID**, created and seeded in State. The same durable state is reused across all trials. The mock mode cannot modify real fluid state.
9. **Validate**, then **Start campaign**. Editing a draft does not alter an already-running campaign's stored specification or setup snapshots.

## Observe and stop

The server submits one native run at a time, waits for it to finish, extracts its objective and feeds that observation into the next proposal. Proposal bounds and protocol semantics are checked before each trial. The trial table records parameter values, objective, native run ID and outcome. Use **Open run** to inspect progress and artifacts; use the regular Results and State views for native persisted hardware measurements and consumables.

- **Pause after trial** completes the active run and pauses before another proposal.
- **Stop after trial** completes the active run and ends the campaign.
- **Cancel run** requests native cancellation/feed hold for the active run and prevents subsequent trials. Inspect the native run/controller status before recovery; cancellation is not automatic motion resumption.
- A missing objective, invalid proposal or failed native run stops the loop. Search exhaustion, trial budget, target achievement and patience are recorded as stop reasons.

The station is reserved for the campaign, including between trials and while paused/awaiting observation. Setup mutations and unrelated run submissions are rejected; emergency hold/cancel remain available. Stop the campaign before manual movement or calibration. Switching views or closing the browser does not stop the server-owned campaign. A server restart marks unfinished campaigns **interrupted** and never automatically resumes hardware.

Campaign records and immutable gantry/deck/protocol snapshots live under `<run_dir>/campaigns/<campaign_id>/`. Child runs use the normal run store. The editor draft is stored locally in the browser; started campaign records remain on the server.

## Offline example

`examples/active-learning/` contains a mock-only gantry, deck, measurement protocol and campaign JSON. Copy the YAMLs into your isolated config directory as `bo_demo_gantry.yaml`, `bo_demo_deck.yaml`, and `bo_demo_protocol.yaml`. The mock Filmetrics adapter returns 150 nm on each run: this deliberately demonstrates feedback collection and automatic patience stopping, not physical optimization or improvement.

Use parameter `height`, bounds 0–10, step 1, bound to step 1 `measurement_height`; objective `0.thickness_nm`, minimize; initial trials 2; seed 7; max trials 8; patience 2. Keep **Offline mock** selected. It completes three native offline trials and stops with `no_improvement`.

API endpoints: `GET/POST /api/v1/campaigns`, `POST /validate`, `GET /{id}`, and `POST /{id}/pause`, `/resume`, `/stop`, `/cancel`, `/observation` (finite `value`). Submit/validate bodies wrap the configuration in `{"spec": ...}`. A native numeric objective and explicit target sequences are required for fully automatic experimental campaigns; manual observations intentionally wait for the operator.

## Hardware validation status

Development verification uses isolated mock runs and injected fake run managers. No real motion, pipetting or protocols were executed by the implementation task. Before a hardware campaign, an operator must validate one generated trial against calibrated labware, bind fresh consumables, verify the objective path from a completed run, then run a short supervised campaign and check stopping/cancellation and state conservation. Any camera scoring method needs its own defined image analysis and validation.
