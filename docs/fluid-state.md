# Fluid State Tracking

CubOS can save the fluid contents of wells and vials in its SQLite database,
then resume that state in a later protocol run. A fluid state is associated
with the deck YAML that created it, so CubOS can reject a resume attempt made
with a different deck configuration.

Fluid tracking is optional. Protocols that do not create or resume a fluid
state continue to run without tracked contents.

## Prepare the deck and initial fluids

Give each volume-bearing labware item a stable key in the deck YAML. For
example, a reagent grid might be named `reagents` and a well plate
`assay_plate`. Protocol endpoints then use addresses such as
`reagents.buffer` or `assay_plate.A1`.

Aliases are convenient protocol names, but CubOS persists canonical
positions. If `buffer` is an alias for `A1`, a protocol may use
`reagents.buffer` while the saved state records `reagents.A1`.

Create an initial-fluids YAML file containing the starting volume and
composition of each non-empty location:

```yaml
fluids:
  reagents.buffer:
    volume_ul: 1000.0
    composition:
      buffer: 1000.0
  reagents.dye:
    volume_ul: 200.0
    composition:
      dye: 200.0
```

For every location, `volume_ul` must equal the sum of the component volumes.
Locations omitted from the legacy seed are known empty. The operator workflow
can opt into an explicit fresh setup where omitted locations are **unknown**;
unknown is shown separately from a confirmed empty location and cannot be used
as a transfer source or destination until an operator records the observed
volume or confirms empty.

## Keep one physical setup across runs

The operator selects one active setup in **Deck contents**. Starting another
experiment does not create a new setup, and opening history never changes the
active selection. The active selection survives a server restart. Before a run,
review the active setup and its latest changes; the review reports known-volume
shortages and existing protocol validation findings without predicting a
measurement result.

Manual contents changes are records only and never move the gantry. Use the
table or plate view to prepare a batch, preview before/after values, then save
it as one revision-checked batch. Supported record-only actions include setting
or correcting a volume, adding/removing a known amount, confirming empty,
transferring with composition carried to both sides, and replacing a selected
same-shape plate. Every change has an immutable before/after history entry.
Edits are rejected while a run or campaign owns the setup. If a run is
interrupted, reconcile the actual source and destination volumes before
resuming; CubOS does not infer them from the command that was attempted.
An observed volume may exceed a labware working-volume warning while it is
being recorded, but pre-run safety validation must resolve that warning before
the robot is allowed to execute a protocol.

## Create a state

Use your own calibrated gantry and deck YAML files and your own reviewed
protocol. The first run creates and seeds a deck-linked state and prints its
numeric ID:

```bash
python setup/run_protocol.py \
  /path/to/gantry.yaml \
  /path/to/deck.yaml \
  /path/to/first-protocol.yaml \
  --database /path/to/cubos.db \
  --initial-fluids /path/to/initial-fluids.yaml
```

By default, this command connects to the configured gantry and instruments.
Add `--mock` to run it offline. Validate the exact gantry, deck, and protocol
files before running them:

```bash
python setup/validate_setup.py \
  /path/to/gantry.yaml \
  /path/to/deck.yaml \
  /path/to/first-protocol.yaml \
  /path/to/initial-fluids.yaml   # optional
```

When the optional initial-fluids YAML is supplied, validation also simulates
every `transfer`/`serial_transfer`/`mix` step against those starting volumes:
pipette-model volume bounds, vial `dead_volume_ul` floors, and destination
`working_volume_ul` overflow are all checked offline, so a protocol that
would fail its liquid-safety preflight mid-run fails here instead. Containers
not named in the file are treated as starting empty.

## Resume a state

Use the same database, the same deck configuration, and the ID printed by the
first run:

```bash
python setup/run_protocol.py \
  /path/to/gantry.yaml \
  /path/to/deck.yaml \
  /path/to/next-protocol.yaml \
  --database /path/to/cubos.db \
  --fluid-state-id STATE_ID
```

CubOS applies successful transfers and mixes to the persisted state. A mix is
journaled but does not change the tracked total volume or composition.

## Interrupted operations

If a pipette action fails after its journal entry starts, CubOS blocks resume
until the physical result is reconciled through the core `DataStore` fluid
state API. CubOS does not guess whether liquid moved after an interrupted
hardware action.

The repository's fluid-state YAML files under
`tests/fixtures/configs/fluid_state_resume/` contain fake coordinates and an
offline `/dev/null` gantry. They exist only for automated tests and are not
supported hardware configurations or operator examples.
