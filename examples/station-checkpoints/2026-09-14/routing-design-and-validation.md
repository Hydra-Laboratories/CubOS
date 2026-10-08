# Labware-aware routing and three-protocol simulation

## Scope

Build branch `feat/labware-aware-routing` in `CubOS-wt/labware-aware-routing` from current main `bc769716`. Import only the side-exit core/simulator prerequisites from PR335; exclude active learning PR337 and unrelated calibration/UI fixes. No live hardware, Pi deployment, or hardware protocol execution in this task. GPT-5.6 subagents perform code implementation; root owns design, integration review and verification.

The authoritative user deck is `CubOS-local-configs/picus1000-color-matching/deck/deck.yaml` (saved Sept10). Preserve an exact source copy. New demos retain all calibrated plate/vial/tip/waste positions and pitches. Add explicit geometry registration and access metadata; do not pretend the stale original tip-rack `location` locates its newly calibrated box.

## Design constraints

- Opt-in planning preserves existing decks/protocol behavior without changes.
- Labware targets, registered physical boxes and access policy are distinct. A1/A2 establish XY orientation, and a local geometry offset positions a box relative to the calibrated anchor. Flat-deck/yaw placements for v1; reject unsupported or incomplete geometry.
- Conservative scene geometry includes every declared fixture, occupied tips and every mounted tool's explicit envelope, including attached tip extension. Simulation tool dimensions are labeled assumptions.
- Default operation access is vertical. `pick_up_tip` can declare a side-exit strategy with at least 30 mm lift and a local exit edge/clearance. Access permissions are restricted to the target fixture/corridor and active tool; never globally ignore obstacles.
- Route selection may choose Y-before-X or intermediate waypoints. Runtime, validation and preview consume the same immutable segment plans, with explicit tool-state changes. No downstream driver may silently change the planned axis order or add a retract.
- Validate complete pickup/departure before motion/attachment. Fail closed on no route, missing geometry, out-of-bounds paths, blocked slots, or inadequate clearance. No automatic recovery or motion after failure.
- Centralize automatic protocol movement. Unsupported commands in planning-enabled runs must reject explicitly rather than bypass planning; legacy runs remain supported.

## Phases

1. Complete: current-base review, isolated branch and scoped prerequisite commit.
2. Complete initial integration: core planner/config/access, exact executor, native command hooks, and API/operator metadata preservation.
3. Complete: integrated native-protocol runs, adversarial regression coverage, shared-plan parity and independent review. Final root core run: 2,634 passed, 17 skipped, 14 subtests; diff coverage 92%. Operator: 334 passed plus build. API/SDK: 317 passed, 2 skipped. Color Twin: 44 passed plus build. Core and metadata preservation committed as `2711fdd7`; simulator and final native detour integration committed as `1e765846`.
4. Complete: all three named protocols ran to completion in the browser on localhost:8760. Ordinary Planner and Legacy variants both produced 300 µL/#7d5d55. Saved side exit produced the same 300 µL mixture; the lift/exit buttons were verified at 5.0574s/5.743114s. Saved waste transit completed with 100 µL and exact Y-before-X controller events. Moving-bed geometry/route overlays and selected-variant YAML state were corrected and visually reviewed. Editable YAML files and full JSON evidence are saved under `CubOS-local-configs/routing-review-2026-09-11`.

## Required demonstrations

1. Ordinary vertical rack / normal liquid transfer: legacy baseline versus planning-enabled equivalent, identical liquid outcome and ordinary vertical access. Use a physically feasible roomy reference setup; show when no detour is needed.
2. Saved user deck: side-exit pickup from exposed A1, then native fluid transfer. The 70 mm tip attaches before exactly 30 mm carriage lift and exits inward along -X. No handwritten travel waypoints in the protocol.
3. Saved user deck: plate-to-waste or equivalent transit that would cross the tall rack with X-first movement; planner chooses Y-first/a clear detour, with identical segments in preview and execution. Include blocked/no-route negative tests separately.

## Known geometry issues to resolve explicitly

User tip coordinates now run +X, so the opening is at A1, not A12. With nominal CAD registration at A1 offset (-7,-78,-70) mm and size120/84/63, the negative-X body edge is X139.456; legacy exit_x140 is not sufficient clearance. The new profile should derive an exit outside the registered body with margin; retain the original file and explain the derived access change. Geometry/clearance and tool-body dimensions remain nominal, not physical collision certification.

## Design review refinements

- Final configuration contract: per-labware optional `motion` contains registered box and operation access; deck-level `motion_planning` enables/settings. Instrument `motion_envelope` is relative to the bare tool TCP and resolves via existing calibrated offsets/depth.
- Stock/waste scalar anchors must explicitly use `location` when no A1 exists; no nonexistent-anchor fallback.
- Latest actual calibrated carriage Z range is10.5–66.5 (still56mm travel). The saved-deck pickup lift reaches58.5 and is feasible within that range; do not use stale seed Z0–56 or increase the physical travel to make the demo pass.
- CAD registration review found asymmetric end margins. The conservative rack collision box uses offset(-7,-78,-70), size124×84×63 to cover both end registrations; physical mesh length120 remains unchanged. The source STL hash/size was checked independently.
- Existing setup failure handling automatically retracts; planning-enabled failures must suppress that recovery so a trapped/uncertain tool is never moved automatically.
- Native API/operator config roundtrips must retain planning metadata. A separate GPT-5.6 agent is implementing this preservation without adding route controls.
- Simulator loader errors must never strip planning metadata and retry legacy execution. Missing/invalid planning data must surface as errors.
- Independent review added multipart tool envelopes (coaxial nozzle plus wider body), actual tool-state checks, and non-finite readback rejection. All parts are collision checked under the same instrument identity.
- Final demo acceptance requires all96 saved tip slots populated, matching supported ordinary protocols/starting poses in both modes, preview/execution segment equality, and an assertion specific to the plate-to-waste transit.
- An unchanged legacy run was compared against the prerequisite checkout: events, fluids, steps, duration, and calibrated points matched exactly. Operator metadata roundtrip checks passed78 frontend tests and19API tests before final integration.
- A final native regression verifies an intermediate detour when both direct XY orders are blocked. Tool-name-only attribution does not disable detour search; actual fixture/corridor permissions remain direct-access-only.
- No hardware or Pi access occurred. This branch is an offline, axis-aligned routing prototype using nominal registered geometry; physical validation and broader command/resume support remain outside this delivery.

## Rendering follow-up

The user reported that the default scene looked translucent and difficult to interpret. The planned view was replacing physical labware with collision envelopes. Restore solid labware at the calibrated geometry and show the envelopes only through an optional Collision geometry toggle. Keep the same plans, moving-bed frame and consumed-tip state. A GPT-5.6 implementation agent handles the view, with an independent CAD registration check for the side-exit rack. Verification will cover the production build, default/overlay views and playback without resetting the timeline. No hardware behavior is changed.

CAD registration audit: all 96 tip-hole rings at native Y=-60 have centers `(X,Z)=(6+10*r,-117+10*c)`, radius 1.5 mm. The saved-deck transform is `deckX=nativeZ+263.456`, `deckY=-nativeX+81.035`, `deckZ=nativeY+91.5`. It aligns every calibrated tip center and places the physical rack at X143.456–263.456, Y-2.965–81.035, Z28.5–91.5. The planner's extra 4 mm is an open-edge reserve, not a reason to stretch the rendered CAD model.

The box-aligned display transform reflects the native mesh; its triangle winding must be corrected. A proper rigid rotation with the row labels reversed would instead shift the physical Y envelope +2 mm. This display follows the existing nominal box registration, not a new physical metrology claim. Confirm handedness and the asymmetric end margins during physical registration before treating the nominal envelopes as measured hardware geometry.

Completed: solid rack CAD, plate frames, visible tips and vial bodies now render by default; conservative wireframe envelopes are optional. Triangle winding is repaired for the reflected display transform. Three render-helper tests and the production build pass. Browser screenshots verified the normal and overlay views; toggling preserved the paused 5.0574-second lift pose and camera view. The simulator was left in normal view. No core motion, deck calibration, protocol data or hardware was changed.
