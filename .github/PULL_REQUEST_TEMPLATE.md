<!-- Keep each PR focused on one behavior, bug fix, driver, workflow, or docs
change. See CONTRIBUTING.md for full expectations. -->

## Summary
-

## Tests
- [ ] Added or updated test coverage for the behavior changed
- [ ] Ran focused tests:
- [ ] Ran offline setup validation (`python -m cubos.tools.validate_setup`), if relevant:
- [ ] CI is expected to pass (full suite + diff-coverage gate)

## Hardware validation
<!-- Required for changes to gantry motion, calibration, setup scripts,
protocol execution, labware coordinate resolution, instrument drivers, vendor
integrations, or commands that actuate instruments. Be concrete — do not
summarize as "tested on hardware". -->
- [ ] This PR does not affect hardware
- [ ] Hardware affected:
- [ ] Hardware tested:
- [ ] Exact commands/protocols run:
- [ ] Actions observed:
- [ ] Hardware behavior not tested:

## Abstractions
- [ ] Instrument/vendor boundaries are preserved (no vendor-specific behavior
      in protocol code, generic interfaces, or shared models)
- [ ] Height/limit parameters live on protocol command schemas, not on
      instrument or gantry config
- [ ] Optional vendor dependencies remain optional (lazy SDK imports, extras
      in `packages/core/pyproject.toml`)
- [ ] Public docs/config examples reflect every added, removed, or renamed
      config/catalog choice (including changes that do not alter the schema)
