# Agent Retrieval Index

Use this map after reading `AGENTS.md`. Read the smallest relevant set of files for the task.

## Gantry / motion / coordinates / homing

Read before changing motion, coordinates, bounds, homing, session ownership, or scan/protocol movement.
- `packages/core/src/cubos/gantry/session.py` - persistent connected gantry session, serial operation lock, cached position/status, calibration policy, interrupt handling, and CubOS-owned HTTP runtime delegation.
- `packages/core/src/cubos/gantry/gantry.py`, `gantry_config.py`, `origin.py` - frame, working volume, deck-origin calibration.
- `packages/core/src/cubos/gantry/machine_geometry.py` - built-in machine geometry per gantry family. Not user-authored YAML; consumed by setup validation.
- `packages/core/src/cubos/gantry/coordinate_translator.py`, `instrument_mount.py`, `instrument_loader.py`, `loader.py`, `yaml_schema.py`, `grbl_settings.py`, `calibration_utils.py`, `offline.py` - gantry boundary, mounted instrument offsets, GRBL calibration utilities, and labware movement.
- `packages/core/src/cubos/validation/bounds.py`, `packages/core/src/cubos/validation/protocol_semantics.py` - offline safety checks.
- Tests: `packages/core/tests/protocol_engine/test_deck_origin_configs.py`.

Setup and calibration flows should route through public `Gantry` APIs. Long-lived UI/API runtimes such as CubOS should route through `GantrySession`. Do not import `Mill` or `gantry_driver` internals from user-facing scripts.

Gantry YAML requires top-level `gantry_type` (`cub` or `cub_xl`).

Setup bounds validation checks the concrete motion targets implied by the loaded protocol, not every physical geometry anchor or unused labware point on the deck.

## Deck YAML / labware / calibration

- `packages/core/src/cubos/deck/yaml_schema.py` - strict Pydantic schema.
- `packages/core/src/cubos/deck/loader.py` - load-name expansion, calibration, derived wells, nested labware.
- Public API for UI/config consumers: `deck.resolve_load_names`,
  `deck.derive_wells_preview`, and
  `deck.yaml_schema.LABWARE_YAML_ENTRY_MODELS`.
- `packages/core/src/cubos/deck/labware/`, `packages/core/src/cubos/deck/labware/definitions/`.
- `packages/core/configs/deck/`.
- Tests: `packages/core/tests/deck/test_deck_loader.py`,
  `packages/core/tests/deck/test_holder_labware.py`, `packages/core/tests/deck/test_panda_deck_yaml.py`.

After schema/config changes: focused tests, then `packages/core/src/cubos/tools/validate_setup.py` for affected real triples.

## Protocol engine / setup validation

- `packages/core/src/cubos/protocol_engine/yaml_schema.py`, `loader.py`, `runtime.py`, `setup.py`,
  `setup_validator.py`.
- `packages/core/src/cubos/protocol_engine/commands/` - command behavior.
- `packages/core/src/cubos/tools/validate_setup.py` - end-to-end offline validation.
- `packages/core/configs/protocol/<instrument-or-workflow>/`.
- Tests: `packages/core/tests/protocol_engine/`.

## Instruments

- `packages/core/src/cubos/instruments/<type>/interface.py`, `vendors/<vendor>.py`, `models.py`, `exceptions.py`.
- `packages/core/src/cubos/instruments/registry.yaml`, `packages/core/src/cubos/instruments/registry.py`, `packages/core/src/cubos/instruments/yaml_schema.py`.
- Public API for UI/config consumers: `instruments.registry.config_fields`,
  `instruments.registry.list_measurement_methods`, and
  `protocol_engine.is_measurement_result`.
- Gantry YAML keeps `type` + `vendor`; `registry.py` resolves the concrete class, merges installed `cubos.instrument_registries` entry points, and then merges `CUBOS_INSTRUMENT_REGISTRY_PATHS` overlays.
- `packages/core/src/cubos/protocol_engine/measurements.py`, `packages/core/src/cubos/data/data_store.py` - persisted measurements.
- `packages/core/src/cubos/data/protocol_runs.py`, `packages/core/src/cubos/data/exports.py` - campaign creation for CubOS-owned protocol sessions and CubOS-compatible result summaries/CSV exports.
- `packages/core/src/cubos/data/download.py` - the Operator "Download Data" ZIP: raw recorded data as plain CSVs, no analysis.
- Tests: `packages/core/tests/instruments/`, `packages/core/tests/protocol_engine/`, `packages/core/tests/data/`.

## Calibration scripts

- `packages/core/src/cubos/tools/calibrate_gantry.py` - only supported user-facing calibration entrypoint. Loads input gantry YAML, dispatches single- or multi-instrument flow by instrument count. Without `--output-gantry`, prompts before overwriting input; with it, writes the explicit path without extra prompt.
- `packages/core/src/cubos/tools/calibration/single_instrument_calibration.py` - internal one-instrument flow.
- `packages/core/src/cubos/tools/calibration/multi_instrument_calibration.py` - internal multi-instrument flow.
- Detailed operator steps and offset math live in `docs/calibration.md`.

## Setup scripts

- `packages/core/src/cubos/tools/validate_setup.py` - offline gantry+deck+protocol bounds/semantics validation. PASS/FAIL.
- `packages/core/src/cubos/tools/run_protocol.py` - load, validate, connect hardware, run protocol end-to-end. Delegates the hardware lifecycle to `protocol_engine.setup.run_on_hardware`, which connects the gantry after clearing the expected GRBL alarm and restoring state, connects instruments before the first step, and disconnects both in `finally`. Python-authored protocols built with `ProtocolBuilder.with_setup(...)` reach the same path via `protocol.run()` (and `protocol.validate()` for offline checks).
- `packages/core/src/cubos/tools/hello_world.py` - interactive deck-origin jog test. Homes without rewriting WCS, then jogs in the deck frame. Arrow keys (X/Y +/-1mm), Z (down 1mm), X (up 1mm), Q (quit).
- `packages/core/src/cubos/tools/keyboard_input.py` - single-keypress reader using Unix `tty`/`termios`.

## Exception handling patterns

These rules apply throughout `packages/core/src/cubos/`:

- `except Exception` in user-facing pipelines: include the exception type name in the error message so developers can diagnose without a full traceback, for example `f"{type(exc).__name__}: {exc}"`. Add `logging.getLogger(__name__).debug(..., exc_info=True)` for config-load failures; use `logging.exception` for unexpected failures in validators.
- Unguarded validation calls: wrap calls to validators, bounds checkers, or semantics checkers that can raise so callers get a structured result or a clear re-raised message instead of raw operator tracebacks.
- `MillConnectionError`: never catch and discard; always re-raise. Tests must explicitly cover propagation at every place a hardware exception is caught.
- Broad substring matching: avoid bare tokens like `"failed"` in status-parsing functions. Name the exact sentinel strings and verify they do not match unrelated production status strings.
- Post-jog status probes: if a probe raises after a successful jog, separate its exception handler from the jog handler and emit a distinct message. Do not report the jog itself as failed when only the probe failed.

## Testing patterns

- `MillConnectionError` propagation: for every `except MillConnectionError: raise` in hardware code, add a test that injects a `MillConnectionError` at that call site and asserts it propagates unchanged out of the top-level function.
- Error-stage routing: for any function that returns a result object with a `stage` field, each distinct stage value must have at least one test that triggers it and asserts `result.stage == "<stage>"`.
- Negative cases for status-matching functions: parametrize healthy status strings such as `"<Idle|WPos:0,0,0>"` and `None` alongside positive cases to prevent regressions from broadened token matching.
- Validation-engine failures: use `monkeypatch` to make validator functions raise and verify the calling pipeline returns a structured error result rather than propagating the raw exception.
