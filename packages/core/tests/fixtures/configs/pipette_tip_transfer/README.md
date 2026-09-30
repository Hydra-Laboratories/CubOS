# Pipette Tip Transfer Simulation Fixture

Offline-only Cub XL fixture for replaying pipette tip pickup, attached-tip
height, transfer, blowout, drop-tip, and home behavior.

Files:

- `gantry.yaml` - simulated Cub XL gantry with an offline Opentrons P300.
- `gantry_sartorius.yaml` - the same gantry with an offline Sartorius Picus 2
  (1000 uL) instead, so the identical deck and protocol can be replayed across
  two very different actuation models.
- `deck.yaml` - well plate, tip rack, and tip disposal positions inside the
  simulated reach envelope.
- `protocol.yaml` - six-step protocol used by `digital-sim` examples.

Validate without hardware:

```bash
python -m cubos.tools.validate_setup \
  packages/core/tests/fixtures/configs/pipette_tip_transfer/gantry.yaml \
  packages/core/tests/fixtures/configs/pipette_tip_transfer/deck.yaml \
  packages/core/tests/fixtures/configs/pipette_tip_transfer/protocol.yaml
```
