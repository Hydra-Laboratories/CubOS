# External API clients

CubOS owns instruments, motion, protocol execution and durable inventory. Applications such as Ursa Learning submit protocols and retrieve measurements over HTTP. They must not import the API server, open the station database, or read image paths on the CubOS host.

## Connect and inspect

Use the station's configured URL (normally `http://127.0.0.1:8742`). Native state-changing requests must send `Authorization: Bearer <device-token>` when an API token is configured. Existing Host and browser Origin checks still apply. A server-side application can proxy this boundary so its browser does not require cross-origin access or see the device token.

- `GET /api/v1/station/status` reports connection, calibration, active run and reservation owner.
- `GET /api/v1/{gantry,deck,protocol}/configs` lists saved configurations.
- `GET /api/v1/configs/{category}/{filename}/raw` returns `{ "content": "...YAML..." }`. Category is `gantry`, `deck` or `protocol`; identically named files in different categories remain distinct.
- `POST /api/v1/station/state/validate` accepts `{ "deck_yaml": "...", "fluid_state_id": 1 }`. It checks deck compatibility and unresolved fluid, tip and cap operations, then returns current `fluids`, `tips` and a `dead_volumes` mapping by canonical stock target. Fluid container rows also include `dead_volume_ul`.

Save immutable setup snapshots with your experiment. Retrieve current inventory at use time; never treat a copied inventory snapshot as authoritative.

## Reserve a station

`POST /api/v1/station/reservation` accepts `{ "owner": "my-experiment" }` and returns an opaque `reservation_token`. Keep the token private. `GET` on the same path reports ownership without exposing it. A reservation prevents unrelated protocol submissions and station mutations between trials and while an experiment is paused. The holder supplies `X-CubOS-Reservation` on required station writes; native run submission also accepts `reservation_token` in its body. Emergency hold, cancellation and camera monitor lifecycle remain available.

The reservation is persisted across CubOS restarts and has no automatic timeout. Interrupted native runs are marked failed and never replayed. After an application crash, inspect physical state before further work. Release with `DELETE /api/v1/station/reservation` and the `X-CubOS-Reservation` header after the active run finishes. The CubOS operator also exposes **Release reservation**, requiring confirmation, for recovery when the application lost its token. Its API is `POST /api/v1/station/reservation/operator-release` with the current `owner` and exact `confirmation` string `release <owner>`. It cannot release an active run. This uses the same authentication and Origin checks as other writes.

## Validate and execute

`POST /api/v1/runs/validate` accepts the normal `RunSubmission`: either a complete saved filename bundle, or inline `gantry_config`, `deck_config`, `protocol_yaml`. It returns `valid`, `errors` and `output` without submitting a run. Include `state: { "fluid_state_id": 1 }` to validate against existing durable inventory. Create and seed new state separately. Mock runs cannot select physical state.

An optional `tip_snapshot` is supported only for dry validation of a virtual future batch. For physical state, it must contain the current slots and physical pipette attachment; it may consume available tips but cannot restore consumed tips. This snapshot never changes inventory or overrides inventory during execution.

Submit the same bundle to `POST /api/v1/runs`. Supply an explicit `mock_mode`, caller-selected `run_id` and metadata identifying your experiment. Stateful submissions include `state: { "fluid_state_id": 1 }`. Poll `GET /api/v1/runs/{run_id}` for terminal state and numeric results. Use `/events` for progress, `/plan` for compiled steps, and `POST /cancel` for interruption. Stable run IDs let a client recover an ambiguous network response by reading the existing run; never blindly submit a second physical trial.

## Measurement evidence

Successful native measurement results containing `image_path` or `annotated_preview_path` are copied into immutable run artifacts. The configured image root is enforced, capture digests are checked when present, and source images remain unchanged. `RunRecord.metadata.evidence_artifacts` maps each result path and field to an opaque artifact name and SHA-256 digest. Retrieve bytes with `GET /api/v1/runs/{run_id}/artifacts/{name}`; `/artifacts` lists files. Use this mapping instead of opening remote filesystem paths.

A learning application can download a target capture, check its digest and save local analysis revisions without moving hardware. Keep its objective/provenance decisions and derived annotations in its own store; keep native measurements, inventory and execution in CubOS.

## Operator validation before physical use

No hardware was operated during this extraction. On an isolated offline station, verify reservation conflicts, validation and evidence downloads with mock runs. Before physical use, an operator should confirm the calibrated setup and consumables, reserve the station, validate and submit one reviewed protocol, inspect the returned measurements and image digests, and exercise cancellation and reservation release. Reconcile uncertain inventory before continuing. Finally confirm that a CubOS or application restart does not replay a run or remove the reservation.

Interrupted liquid-handling protocols must not be replayed from a consumed pickup target. Setup validation rejects that replay before execution, preserving the completed liquid journal and attached-tip state. Inspect and reconcile the physical state, then prepare a reviewed continuation protocol. Coordinated XY travel follows the configured work-position frame; engagement and retraction remain Z-only at the resolved target.
