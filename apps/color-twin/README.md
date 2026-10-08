# CubOS Color Twin

The Color Twin is a localhost, simulation-only viewer for native CubOS YAML
protocols. The routing review at the top of the page has exactly four named
cases:

1. **Ordinary vertical transfer** — the legacy and planner paths use vertical
   access in a roomy reference scene and must produce the same liquid result.
2. **Saved deck · A1 side exit** — the byte-preserved user deck is registered
   with its calibrated 2 × 6 stock holder. The native protocol picks up from
   exposed `tips.A1`; core access metadata performs the 30 mm lift and exits
   through the negative-X edge before fluid handling.
3. **Saved deck · Y-first waste detour** — the same saved deck registers the
   nominal 120 × 84 × 63 mm rack obstacle with a conservative 124 mm registered
   X envelope. The planner chooses a Y-first clear
   corridor when traveling toward waste. The UI shows the serialized shared
   plan and the execution segment stream when core exposes both.
4. **Saved deck · Picus 120 µL batch 1** — a byte-preserved copy of the
   station's current run: Picus 2 120 µL pipette, 42 mm tips, a standard
   vertical tip rack, and batch 1 of the color-matching campaign, with the
   `measure_color` steps dropped (the sim does not support them) and the
   camera `move` steps kept.

The default view shows solid labware. Turn on **Collision geometry** to inspect
the planner's conservative obstacle envelopes over those models. This display
toggle keeps the current playback position and does not change the route.

`examples/saved-user/deck.yaml` is an exact copy of
`CubOS-local-configs/picus1000-color-matching/deck/deck.yaml`. New routing
geometry is additive: it lives in each profile's `motion` labware metadata and
the deck-level `motion_planning.clearance_mm`. The derived side-exit profile
retains all saved coordinates and requests an 8 mm clearance from the nominal
rack edge at X=139.456 mm. Core derives the final exit coordinate from the
registered rack and every mounted-tool envelope (currently X=122.456 mm in
the serialized plan); the original `exit_x: 140` remains unchanged in the
provenance copy.

Run from the CubOS repository root with the checkout's environment:

```sh
PYTHONPATH=packages/core/src:apps/color-twin python -m uvicorn sim.server:app --host 127.0.0.1 --port 8760
```

Open `http://127.0.0.1:8760/`. Playback, seeking, route phase labels, obstacle
geometry, planned paths and native execution traces are offline previews. No
serial port, protocol execution on hardware, Pi deployment or private photo is
used. Nominal CAD envelopes, attached-tip radius, camera geometry and saved
coordinates still require physical fit, collision, Z-stroke and measurement
validation.

For a clean camera-facing presentation of the saved Picus batch, open
`?profile=saved-picus120&view=photo`. The `Photo` view keeps the native
timeline scrubber (`Simulation timeline`) available for deterministic captures
while hiding route traces, planning overlays and review sidebars. The view
selector writes the `view` query parameter, so a capture can return to the
standard UI by changing it to `orbit`, `front`, or `top`.

The synthetic RGB values are illustrative and do not represent measured chemistry.

Tests:

```sh
PYTHONPATH=packages/core/src:apps/color-twin python -m pytest apps/color-twin/tests -q
npm run test:render --prefix apps/color-twin
npm run build --prefix apps/color-twin
```

Export all four bundles, serialized core plans, execution traces, comparison
results, initial poses, assumptions and the saved-deck hash to a reviewable
artifact (including editable YAML under each demo folder and the ordinary
legacy/planned comparison folders):

```sh
PYTHONPATH=packages/core/src:apps/color-twin python -m sim.export_demos --output /tmp/cubos-routing-evidence
```
