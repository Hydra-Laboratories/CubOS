# CubOS color simulation

A local, simulation-only viewer that executes native CubOS fluid-handling protocols against virtual instruments. The side-exit example uses a 1000 µL pipette profile, 70 mm attached tips and 56 mm usable Z travel.

## Run

From the CubOS repository root, using this checkout's virtual environment:

```sh
python -m pip install -e 'packages/core[dev]' fastapi uvicorn httpx
npm ci --prefix apps/color-twin
npm run build --prefix apps/color-twin
PYTHONPATH=packages/core/src:apps/color-twin python -m uvicorn sim.server:app --host 127.0.0.1 --port 8753
```

Open <http://127.0.0.1:8753/?profile=side-exit>. Choose **Rack close-up**, then **Review first pickup at 1×**. The **A / B** buttons pause after the 30 mm lift and X-only withdrawal. Play the entire protocol to transfer three colors into A1, mix, discard the tips and capture a synthetic color observation. **Run BO campaign** generates recipes and fills additional wells.

Inputs are in `examples/side-exit/`. Download edited YAML or export the complete run through the UI. The earlier illustrative 120 µL profile is available without the query parameter.

## What the example verifies

The simulator uses CubOS loaders, protocol handlers, deck resolution, mounted-tool offsets, bounds and semantic validators. Gantry movements follow CubOS's separate axis moves. Virtual instruments track tip consumption, liquid capacity, stock depletion and mixture volumes. The scene displays the original rack STL; its provenance and orientation are documented in `src/assets/README.md`.

No serial connection or hardware API is used. This is an uncalibrated geometric/protocol preview: engagement Z, mounted extension, camera offsets and deck anchors require physical measurement. The constant-feed playback does not emulate GRBL firmware, acceleration, alarms or emergency stops. Colors use a synthetic absorbance model, not measured chemistry. The demo's explicit Y corridor and waste position must be calibrated for a real setup.

See [side-exit rack support](../../docs/side-exit-tip-rack.md) for the YAML fields, coordinate math and operator test procedure. Physical validation is pending.

## Tests

```sh
PYTHONPATH=packages/core/src:apps/color-twin python -m pytest apps/color-twin/tests -q
npm run build --prefix apps/color-twin
```
