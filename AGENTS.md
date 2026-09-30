# CubOS Agent Guide

CubOS controls real lab hardware: a GRBL CNC gantry plus mounted instruments.
Prefer repo source over model memory; keep retrieval focused.

## Fast Start

1. Read this file and `CLAUDE.md`.
2. Use `docs/agent-index.md` to find the source/docs for the area you are touching.
3. Read only the files needed for the task unless you are changing shared interfaces, coordinate semantics, hardware motion, YAML schemas, or protocol setup.
4. Before opening a PR, follow `CONTRIBUTING.md`: tests for the behavior you changed, the hardware-validation report, and the abstraction checklist. The PR template mirrors that checklist; fill in every section.

## Hardware Safety

Any code/config change can affect real motion, instruments, samples, or controllers.

Always tell the user:
- Hardware touched or potentially affected.
- Offline validation performed.
- Required physical validation still pending.

Hardware-facing handoff order:
1. Run focused offline/unit validation for the edited behavior.
2. Stop and give the user the exact hardware test procedure before cleanup or broad test sweeps.
3. After the user confirms or asks to continue, clean up temporary files/checkpoints and run broader relevant tests.

Prefer dry-runs and validation scripts before commands that move gantries, start protocols, or actuate instruments.

## Coordinate Convention

CubOS deck frame:
- Origin: front-left-bottom (FLB)
- `+X`: operator-right
- `+Y`: back/away
- `+Z`: up; `-Z`: down

Do not pre-flip signs in high-level code. GRBL `$3` axis direction and `$23` homing direction must make controller WPos match this frame. `$H` homes to back-right-top (BRT). High-level gantry code applies no hidden Z sign flip; working-volume bounds are deck-frame values.

Retrieval rule: do not infer sign flips from older code or model memory. Confirm convention in source and tests.

### Heights: absolute vs. labware-relative

Two kinds of Z fields coexist:
- **Absolute deck-frame Z** (`gantry.cnc.safe_z`, working-volume bounds, `move`'s `travel_z`, named positions, literal `[x, y, z]` targets). `safe_z` is the travel ceiling: every resolved approach/action Z must be <= `safe_z`. Defaults to `working_volume.z_max` when omitted.
- **Labware-relative offsets** (`measurement_height`, `interwell_scan_height`, `indentation_limit_height` on `scan`/`measure`). Positive = above the well's calibrated surface Z; negative = below. Resolved at command time as `well.z + relative_offset`, where `well.z` is the calibration anchor's z. The labware's `height` is the physical outer dimension, not a Z reference.

These offsets live on the protocol command, never on instruments. `scan` requires both `measurement_height` and `interwell_scan_height`; `measure` requires `measurement_height`. Pipette commands engage at the labware reference Z (`measurement_height = 0` implicitly). ASMI `indentation_limit_height` is a signed labware-relative offset (negative = below the well surface); must be at or below `measurement_height`.

## Active Retrieval

`docs/agent-index.md` is the detailed retrieval map for gantry, deck, protocol, instrument, calibration, setup, exception-handling, and testing work. Keep long source lists there, not here.

For setup validation:

```bash
python -m cubos.tools.validate_setup <gantry.yaml> <deck.yaml> <protocol.yaml>
```

### Instruments
- `packages/core/src/cubos/instruments/<type>/interface.py`, `vendors/<vendor>.py`, `models.py`, `exceptions.py`.
- `packages/core/src/cubos/instruments/registry.yaml`, `packages/core/src/cubos/instruments/registry.py`, `packages/core/src/cubos/instruments/yaml_schema.py`.
- Gantry YAML uses `type` + `vendor`; registry lookup is by that pair. External driver packages register via `cubos.instrument_registries` entry points or `CUBOS_INSTRUMENT_REGISTRY_PATHS` overlays.
- `packages/core/src/cubos/protocol_engine/measurements.py`, `packages/core/src/cubos/data/data_store.py` — persisted measurements.
- Tests: `packages/core/tests/instruments/`, `packages/core/tests/protocol_engine/`, `packages/core/tests/data/`.

## Where New Things Go

Placement rules for new code — decide the layer before writing:

- **New vendor driver for an existing instrument type**: `packages/core/src/cubos/instruments/<type>/vendors/<vendor>.py` plus an entry in `packages/core/src/cubos/instruments/registry.yaml`. Vendor SDK imports stay lazy (tied to connect or the method that needs them); the SDK dependency goes in a `packages/core/pyproject.toml` optional extra, never core dependencies. Support `offline=True` when a meaningful dry-run exists.
- **New instrument type**: `packages/core/src/cubos/instruments/<type>/` with `interface.py`, `models.py`, `exceptions.py`, `vendors/`. Generic behavior lives in the interface/models/exceptions; device-specific quirks live only in the vendor module.
- **New measurement method or result**: method on the type interface, result models in that type's `models.py`, persistence through `packages/core/src/cubos/protocol_engine/measurements.py` and `packages/core/src/cubos/data/data_store.py`. Do not add a parallel storage path.
- **Heights and limits** (measurement heights, indentation limits, and similar): labware-relative offsets on the protocol command schema (`packages/core/src/cubos/protocol_engine/yaml_schema.py` + `commands/`), never on instrument config or gantry YAML. See "Heights: absolute vs. labware-relative" above.
- **Vendor-specific behavior** must never appear in protocol engine code, generic interfaces, or shared models. If a shared layer seems to need a vendor special case, the abstraction is wrong — re-check against `CONTRIBUTING.md` before opening the PR.
- **External or proprietary drivers**: register via `cubos.instrument_registries` entry points or `CUBOS_INSTRUMENT_REGISTRY_PATHS` overlays; do not merge private code into CubOS.

## Calibration Scripts

- `packages/core/src/cubos/tools/calibrate_gantry.py` — only supported user-facing calibration entrypoint. Loads input gantry YAML, dispatches single- or multi-instrument flow by instrument count. Without `--output-gantry`, prompts before overwriting input; with it, writes the explicit path without extra prompt.
- `packages/core/src/cubos/tools/calibration/single_instrument_calibration.py` — internal one-instrument flow.
- `packages/core/src/cubos/tools/calibration/multi_instrument_calibration.py` — internal multi-instrument flow.
- Detailed operator steps and offset math live in `docs/calibration.md`.

Calibration soft-limit rule: `working_volume` is the usable deck/WPos range
after homing pull-off. GRBL `$130/$131/$132` and YAML
`grbl_settings.max_travel_*` are controller spans and must include the `$27`
homing pull-off reserve. Calibration sets `$10=0` for WPos status reporting,
writes the per-machine `grbl_settings.homing_pull_off` (`$27`) before homing
when configured, and saves/programs max travel as usable span plus pull-off.
Do not treat the pull-off reserve as usable WPos.

## Setup Scripts

- `packages/core/src/cubos/tools/validate_setup.py` — offline gantry+deck+protocol bounds/semantics validation. PASS/FAIL.
- `packages/core/src/cubos/tools/run_protocol.py` — load, validate, connect hardware, run protocol end-to-end. Delegates the hardware lifecycle to `protocol_engine.setup.run_on_hardware`, which connects the gantry (clearing expected GRBL alarm, restoring state) and instruments before the first step and disconnects both in `finally`.
- Python-authored protocols use `ProtocolBuilder.with_setup(...)` (in `protocol_engine.builder`); `protocol.validate()` runs the offline checks and `protocol.run()` runs the same `run_on_hardware` lifecycle. The low-level `protocol.execute(context)` runs steps against a context the caller already prepared.
- `packages/core/src/cubos/tools/hello_world.py` — interactive deck-origin jog test. Homes without rewriting WCS, then jogs in the deck frame. Arrow keys (X/Y ±1mm), Z (down 1mm), X (up 1mm), Q (quit).
- `packages/core/src/cubos/tools/keyboard_input.py` — single-keypress reader (Unix `tty`/`termios`).
`scan.plate` may target either a top-level `WellPlate` key or a nested holder path such as `plate_holder.plate`, as long as that path resolves to a `WellPlate`.

## Development Modes

**TDD Mode** is the default: planning sessions, new features, and cross-repo interface changes. Write tests alongside implementation and update docs as you go. New or changed core Python lines must be exercised by tests before the PR is final — CI enforces this with a diff-coverage gate.

**Hardware Iteration Mode** covers two situations where TDD is deferred:

- **Debugging**: fast diagnosis of an existing bug — skip tests until the fix is confirmed, then add a regression test.
- **Iterative hardware testing**: small changes (calibration, GRBL params, YAML tweaks, motion adjustments) where physical validation must come before tests are meaningful.

In both cases: make the minimal targeted change, mark deferred work `# TODO(iter): test` or `# TODO(iter): doc`, and sweep those tags at close-out. Temporary instrumentation is OK if tagged and removed before finalizing. Deferral is within a working session, not past the PR: sweep `# TODO(iter)` tags before the PR is ready for review.

## Progress Notes

See `progress/README.md`. Default: no progress file. Create only for hardware-facing motion changes, refactors >5 files, or explicit handoffs. Delete on PR merge.

## Verification Gates

Smallest meaningful gate first, then broaden:

```bash
python -m pytest packages/core/tests/deck/test_deck_loader.py packages/core/tests/deck/test_holder_labware.py -q
python -m pytest packages/core/tests/protocol_engine -q
python -m pytest packages/core/tests services/api/tests sdk/python/tests -q
python -m cubos.tools.validate_setup packages/core/configs/gantry/cub_xl_asmi.yaml packages/core/configs/deck/asmi_deck.yaml packages/core/configs/protocol/asmi/indentation.yaml
```

Report exact commands and observed results in the PR body.

CI runs the core suite plus a diff-coverage gate: core Python lines added or
changed by the PR must be at least 90% covered. Check locally with:

```bash
python -m pytest packages/core/tests --cov=packages/core/src/cubos --cov-report=xml -q
diff-cover coverage.xml --compare-branch=origin/main --fail-under=90
```

## When to Update This File

Update `AGENTS.md` only when agent retrieval, hardware-safety workflow, or source-of-truth pointers change. Update `README.md` / docs only when public CLI/workflow, YAML schema/config, coordinate/motion/calibration semantics, protocol behavior, or cross-repo interfaces change. Adding, removing, or renaming a public config/catalog choice (such as an instrument model or labware definition) is a public config change even when the schema is unchanged; update docs and examples that enumerate those choices.
