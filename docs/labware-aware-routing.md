# Labware-aware motion planning

Experimental branch: `feat/labware-aware-routing`. This feature is being validated offline; it is not certified for unattended hardware operation.

The planner sits below protocol commands and above gantry execution. It combines calibrated labware targets, explicitly registered obstacle geometry, mounted-tool envelopes and operation-specific access rules into a segment plan. Execution and preview use that plan rather than choosing axis order independently.

## Coordinate and geometry contract

A1/A2 or another supported calibrated anchor pair places a labware-local XY frame on the deck. A local box offset specifies the physical body's lower corner relative to its anchor; dimensions alone do not tell CubOS where that body is. Recalibrating or rotating the anchors must move the registered box and local access rules together.

This first version uses conservative axis-aligned box representations and flat-deck orientation. It does not infer geometry from an STL or provide mesh-level collision certification. Tool geometry and clearance dimensions must be supplied honestly; missing or unsupported geometry must fail before motion rather than being silently omitted.

Working-volume limits constrain the gantry carriage/reference point. Tool envelopes may extend above or beyond that coordinate volume; their collision checks are against the declared physical scene. These are different constraints.

## Access and routing

Ordinary access is vertical: approach a target from a collision-free entry, engage, and leave through the permitted vertical corridor. Side-exit pickup changes the selected rack operation: engage, attach the tip, lift the carriage by the configured amount, then withdraw through the registered open side. The attached tip changes the swept tool geometry, not the meaning of the carriage lift distance.

Access permission is restricted to the operation's target fixture and corridor. It never permits crossing neighboring labware or unrelated loaded tips. The route to/from that corridor can include Y-before-X or additional intermediate segments. A physically impossible approach or departure is a planning failure.

For ordinary transit at unchanged Z, the transit search first tries one coordinated XY line, even when the active instrument is named in the access scope. Instrument attribution alone does not grant fixture or corridor access. The diagonal is used only when its conservative swept box is clear for every mounted tool; otherwise the planner checks both direct XY orders and a finite set of obstacle-edge detours. It is deterministic, but not a complete search of every possible 3D path: a complex feasible route can still be rejected. Fixture- or corridor-scoped engagement, lift and withdrawal segments remain axis-specific and cannot take detours through an access corridor.

The deck opts in with `motion_planning: {clearance_mm: 2}`. Each labware entry then needs a `motion.box`; each mounted instrument needs a `motion_envelope`. Ordinary labware can omit `motion.access` and use vertical access. The saved rack adds:

```yaml
motion:
  box:
    anchor: A1
    offset: {x: -7, y: -78, z: -70}
    size: {x: 124, y: 84, z: 63}
  occupied_tip_radius_mm: 2.5
  access:
    pick_up_tip:
      strategy: side_exit
      lift_mm: 30
      exit_edge: x_min
      clearance_mm: 8
```

`x_min` refers to the labware-local frame, so rotating A1/A2 also rotates the opening. The supported flat-deck orientations are multiples of 90 degrees. Scalar labware such as waste uses `anchor: location`. A1/A2 do not identify the body's lower corner or height; the explicit offset supplies that registration.

Instrument boxes are relative to the bare tool's calibrated TCP. Positive deck Z points up; a pipette body is above its nozzle, and an attached tip extends below it. Optional `motion_envelope.additional_boxes` represents a narrow nozzle and wider body without moving the collision geometry away from the tool axis. The planner checks every part, including inactive mounted instruments.

The supported commands are `move`, `pick_up_tip`, `transfer`, `mix`, `drop_tip`, and the non-moving camera `capture` command. Planning-enabled protocols containing unsupported commands or durable fluid-state resume fail explicitly. Decks without the opt-in retain the existing execution path. This branch does not change active-learning campaign behavior.

To photograph a well, use `move` with the camera and well target, then `capture` with that same well as its image attribution. The move aligns the camera at the carriage travel ceiling; capture itself does not move the machine. A transition from an engaged pipette first withdraws that pipette through its own access corridor, then travels with all mounted tools checked. Images use the existing timestamped, collision-safe names under `~/.cubos/images` (or `CUBOS_IMAGES_DIR`) and are associated with the run's campaign and well. Simulation supplies a separate temporary image directory.

Configuration is checked before instrument connection. Runtime plans are built from the observed carriage pose before actuation; offline previews identify their assumed starting pose. Planned movement uses an exact segment executor. Clear unscoped XY travel is emitted as one coordinated GRBL command, while Z retract/approach and scoped access remain separate checked segments. Failures do not trigger an automatic retract.

## Saved-deck provenance

The new examples use the user's saved `deck.yaml`, captured on 2026-09-10. Its SHA-256 is `db518dbb124aa0c3c95ece7692a66fa80370c48af536557add067f29977e4cc4`. Keep the original snapshot separate from the additional motion metadata used by a demonstration.

Key preserved positions are:

| Target | X | Y | Z |
|---|---:|---:|---:|
| Plate A1 | 25.803 | 70.035 | 38.5 |
| Stocks A1 | 27.803 | 129.035 | 58.5 |
| Tips A1 | 146.456 | 75.035 | 98.5 |
| Waste | 230 | 129 | 60 |

The tip columns advance +X, so the exposed first pickup at the -X opening is A1. The original rack `location` and `side_exit.exit_x: 140` are retained in the source snapshot but are not sufficient collision geometry for the calibrated layout. With the conservative registration offset (-7,-78,-70) and collision-box size124×84×63 mm, the body begins at X139.456. The extra 4 mm in X covers the CAD rack’s asymmetric open/closed end registration while preserving its physical 120 mm length. The planned negative-X exit therefore needs to lie farther left with enough tool clearance. The demonstration must report that derived access coordinate rather than silently treating X140 as safe.

Nominal dimensions and tool envelopes in these examples are explicit simulation assumptions. They do not replace physical calibration or collision checks on the actual apparatus.

## Verification cases

1. Ordinary vertical pickup and transfer, including comparison against the unchanged planning-disabled legacy run.
2. Side-exit pickup and fluid transfer on the saved deck, with no handwritten corridor moves in the protocol.
3. A plate-to-waste trip on the saved deck where X-first would intersect the rack and a clear detour is required.

Negative checks cover inadequate Z clearance, closed exit lanes, bounds, non-finite inputs, unknown geometry, unsupported operations, rotated anchors and post-attachment failure state. The review should compare serialized planned segments with the actual controller calls and simulator events.

The standalone simulator exposes these cases at:

- `http://127.0.0.1:8760/?profile=ordinary-transfer`
- `http://127.0.0.1:8760/?profile=saved-side-exit`
- `http://127.0.0.1:8760/?profile=saved-detour-to-waste`

The two saved-deck cases start with all 96 tips loaded. The side-exit protocol consumes A1, A2 and A3 in order and produces 300 µL in plate A1. The waste-detour protocol transfers 100 µL to plate A1, then takes a checked Y-first/X-second transit to waste. Neither protocol contains handwritten travel waypoints.

From the repository root, using its Python environment, export all inputs and run evidence with:

```sh
PYTHONPATH=packages/core/src:apps/color-twin python -m sim.export_demos --output /tmp/cubos-routing-evidence
```

The export includes the starting poses, assumptions, saved-deck hash, immutable core plans, native execution events, liquid results and ordinary comparison. It fails if planned/executed segments differ or the comparison's liquid outcomes differ.

The final core verification passed 2,634 tests with 17 skips and 14 subtests; changed-core-line coverage was 92% against `origin/main` (90% required). This includes native execution of an intermediate detour when both direct XY orders are blocked. Color Twin passed 44 tests and its production build. Operator passed 334 tests and its production build. API/SDK passed 317 tests with 2 skips; the offline updater tests needed normal shell `/dev/fd` access outside the execution sandbox. No updater contacted a station.

## Physical validation

No real hardware is used during branch development. Before enabling this on a machine, measure and register every fixture/tool envelope, verify the plotted corridors against the physical deck, and have an operator run a slow clear-space route followed by a dry pickup and supervised water transfer. Keep the E-stop accessible. A successful simulation is not proof of unmodeled mechanical clearance.
