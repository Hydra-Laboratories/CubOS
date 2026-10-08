# Picus routing review configs

These files are offline calibration/review inputs. They are not authorization
for an automatic or unattended hardware run. Nothing in this folder has been
activated, imported into the Operator, connected to hardware, or executed.

## Provenance and intentional changes

- `gantry/picus1000_routing_review.yaml` is based on the exact live-backup
  `before/configs/gantry/picus1000_arducam_usb_seed.yaml`. It retains the real
  GRBL serial path, Picus 2 model `picus2_1ch_1000`, baud rate, verification
  flag, camera vendor/device settings,
  calibrated instrument offsets/depths, the 56 mm factory Z span, and the
  saved working volume Z=10.5..66.5. Only this review copy changes `cnc.safe_z`
  from 56.0 to 66.5: the planner needs the saved ceiling to accommodate the
  declared 30 mm lift from a tip-pickup carriage Z of 28.5 mm. The original
  backup is unchanged.
- Nominal multipart motion envelopes match the tested Color Twin saved-user
  simulation profile. They describe a narrow TCP section plus wider mounted
  body for both pipette and camera. They require measurement and physical
  calibration against the installed tools; simulation success is not collision
  certification.
- `deck/color_matching_routing_review.yaml` starts from the exact live-backup
  `before/configs/deck/cub_deck.yaml`, retaining all saved XYZ anchors, XY
  pitches, row directions, tip occupancy, dimensions, and waste location. It
  adds only the tested nominal planner root and per-fixture geometry/access
  metadata.
- The deck retains legacy `tips.side_exit.exit_x: 140`. Routing v1 derives the
  side exit from `tips.motion.access.pick_up_tip` and the registered fixture and
  mounted-tool envelopes, so the planner override owns the actual exit. Do not
  interpret the legacy value as a physically verified clearance coordinate.
- Protocol 03 uses native `pick_up_tip`, `transfer`, `mix`, and `drop_tip`
  commands with exposed tips A1, A2, and A3. Protocol 04 uses the same native
  command set to review the planner-owned transit to waste. Neither protocol
  contains `move`, named/custom travel, `measure`, or another unsupported
  operation.
- `service/cubos-routing-sim.service` stages the Color Twin review server on
  port 8760, separate from the Operator on port 8742. Its entrypoint uses the
  simulator's bundled demo defaults and does not pass or load a live config
  path. `PrivateDevices=true` prevents device-node access, `PrivateTmp=true`
  gives runtime temporary files an isolated `/tmp`, and the live config
  directory is explicitly read-only. Root installation and service activation
  are separate deployment steps and were not performed while preparing this
  bundle.

## Current gates

- The connected Sartorius Picus was freshly identified by USB as serial
  `47683878` at `/dev/serial/by-id/usb-Sartorius_Picus_2_47683878-if00`. The new
  review gantry copy uses that identity because the backed-up `47684951` path
  does not match the connected instrument. Identification did not actuate the
  pipette or run a protocol.
- Routing v1 rejects initial fluids and durable fluid-state resume. The Operator
  may therefore be unable to offer a physical run with its normal durable-state
  workflow yet. Do not bypass that gate to make these review files runnable.
- Before physical use, calibrate and verify every fixture and multipart tool
  envelope, confirm the side opening and route clearances on the real deck, and
  follow the CubOS supervised hardware-validation procedure with the E-stop
  accessible. A reviewer should begin with plotted/offline route inspection,
  then a slow clear-space route and dry pickup before any supervised water
  transfer.

## Offline validation

Run with the routing branch core and the existing CubOS virtual environment;
`validate_setup` creates an offline gantry and mock instruments and does not
open the retained serial or camera ports:

```sh
PYTHONPATH=/Users/alexchan/Documents/Ursa/CubOS-wt/labware-aware-routing/packages/core/src \
  /Users/alexchan/Documents/Ursa/CubOS/.venv/bin/python -m cubos.tools.validate_setup \
  gantry/picus1000_routing_review.yaml \
  deck/color_matching_routing_review.yaml \
  protocol/03_routing_side_exit_review.yaml

PYTHONPATH=/Users/alexchan/Documents/Ursa/CubOS-wt/labware-aware-routing/packages/core/src \
  /Users/alexchan/Documents/Ursa/CubOS/.venv/bin/python -m cubos.tools.validate_setup \
  gantry/picus1000_routing_review.yaml \
  deck/color_matching_routing_review.yaml \
  protocol/04_routing_waste_review.yaml
```

Observed offline results on 2026-09-14:

- Protocol 03: **PASS** — 32 protocol targets, 31 immutable routing plans,
  nominal initial carriage pose `(258.205, 144.645, 66.5)`.
- Protocol 04: **PASS** — 10 protocol targets, 8 immutable routing plans,
  nominal initial carriage pose `(258.205, 144.645, 66.5)`.

Both validations reported `offline/mock - hardware not contacted`. The CLI's
generic final line says a passing protocol is ready to run; for these files,
the physical-calibration and supervised-validation gates above still apply.
