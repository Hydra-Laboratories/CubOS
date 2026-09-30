# Set Up Deck and Labware

Use this guide when labware is moved, recalibrated, or replaced.

1. **Complete gantry calibration first.**

    The gantry calibration establishes the deck coordinate frame that all
    labware positions use.

2. **Secure the labware on the deck.**

    Place the labware in its holder or fixture so it cannot shift during the
    run. The holder or deck placement must be repeatable.

3. **Choose which instrument records the labware position.**

    In a single-instrument setup, use the mounted instrument.

    In a multi-instrument setup, use the leftmost/reference instrument selected
    during gantry calibration. This instrument defines the shared deck origin.

4. **Jog to the labware calibration points.**

    Use CubOS, UGS, or another G-code controller to jog the instrument to the
    physical points that define the labware position. The easiest way is the
    [Operator UI](operator-ui/deck-and-labware.md#calibrate-labware-with-the-gantry),
    which shows the live position while you jog and lets you type it
    straight into the deck editor instead of editing YAML.

    For a 96-well plate, jog to A1 and record the displayed position. Then jog
    to A2 and record the displayed position.

5. **Enter those points in the deck YAML.**

    ```yaml
    labware:
      plate:
        load_name: sbs_96_wellplate
        name: asmi_96_well_deck_origin
        model_name: asmi_96_well_deck_origin
        calibration:
          a1:
            x: 347.0
            y: 42.0
            z: 30.0
          a2:
            x: 338.0
            y: 42.0
            z: 30.0
        x_offset: 9.0
        y_offset: 9.0
    ```

    The key directly below `labware:` is the stable instance ID. In this
    example it is `plate`, so protocols address wells as `plate.A1`,
    `plate.B2`, and so on. Choose a short name that describes the labware's
    role in this deck.

    `load_name` selects a predefined physical layout; it is not the instance
    ID. An optional `label` is display text and can be changed without changing
    protocol addresses. Renaming the top-level key changes the labware identity
    used by protocols and saved experiment state.

6. **Fill in the remaining labware values.**

    For a 96-well plate, `x_offset` and `y_offset` are the well spacing
    magnitudes. Standard SBS 96-well plates usually use `9.0` mm in both
    directions. The measured A1 and A2 points define the plate orientation on
    the deck.

    By default, CubOS keeps the legacy convention: when columns advance in +X,
    rows advance in -Y; when columns advance in +Y, rows advance in +X. If your
    physical plate uses the opposite row side, set `row_direction: positive` or
    `row_direction: negative` to choose the signed deck axis for row B from A1.

    The `z` value on `calibration.a1` and `calibration.a2` is the labware
    reference surface for those wells, not the physical plate height.

## Vials In A Grid

Use `vial_grid` when several vials share a regular layout. The predefined
layout supplies the row and column count, spacing, vial model, optional outer
dimensions, and volume defaults. The deck file supplies the measured A1 and A2
coordinates because those values belong to this physical setup.

```yaml
labware:
  reagents:                         # Stable instance ID
    load_name: ursa_9_vial_grid
    label: Reagent Vials            # Display text only
    calibration:
      a1: {x: 10.0, y: 20.0, z: 40.0}
      a2: {x: 10.0, y: 53.0, z: 40.0}
    aliases:
      buffer: A1
      catalyst: A2
```

The generated vial IDs use the same grid notation as a plate: `A1` through
`A9` for this definition. These addresses all identify the same two example
positions:

- `reagents.A1` and `reagents.buffer`
- `reagents.A2` and `reagents.catalyst`

Aliases map a human name to a generated position ID. They improve protocol
readability but do not create additional vials or database rows. Use the
generated ID when an alias is not helpful.

Values from a definition are defaults. Override a value in the deck entry only
when the physical labware differs. For example, a different vial product may
override `vial_model_name`, `vial_height`, `vial_diameter`, `capacity_ul`, or
`working_volume_ul`. Calibration remains deck-specific even when every other
value comes from the definition.

Vial-like labware may also declare an unreachable residual volume:
`dead_volume_ul` on a `vial` (and nested holder vials), or
`vial_dead_volume_ul` on a `vial_grid` (applied to every vial in the grid).
It is optional and defaults to `0`. Tracked `transfer` steps refuse to draw a
source below this floor before any motion, and state-derived aspiration
heights never descend beneath the level at which this volume remains.

## Container Role And Solution Identity

Vial-like labware may declare generic, machine-agnostic metadata that the
compound liquid-handling commands (`rinse_well`, `flush_pipette`,
`purge_pipette`, `clear_well` -- see [Protocol YAML: Compound liquid
commands](protocol-yaml.md#compound-liquid-commands)) use for automatic
container selection instead of naming a specific vial ID:

- `role` -- one of `stock`, `waste`, `process`, `rinse` (see
  `cubos.deck.labware.container_role.KNOWN_CONTAINER_ROLES`). `stock`
  containers are automatic-selection sources; `waste` containers are
  automatic-selection sinks. `process`/`rinse` are not yet consumed by any
  automatic-selection command but are recognized, reserved roles.
- `solution` -- the canonical solution identity (e.g. `water`), distinct
  from any display `label`/alias. Automatic stock selection matches a
  requested `solution=` against this field.
- `allowed_solutions` -- optional, waste containers only: a list of solution
  identities this container may receive. Omitted (the default) means
  accept-all.

```yaml
labware:
  water_stock:
    type: vial
    name: water_stock
    role: stock
    solution: water
    height: 57.0
    diameter: 28.0
    location: {x: -50.0, y: -10.0, z: -70.0}
    capacity_ul: 5000.0
    working_volume_ul: 4500.0
    dead_volume_ul: 200.0

  aqueous_waste:
    type: vial
    name: aqueous_waste
    role: waste
    allowed_solutions: [water, buffer]
    height: 57.0
    diameter: 28.0
    location: {x: -50.0, y: -40.0, z: -70.0}
    capacity_ul: 5000.0
    working_volume_ul: 4500.0
```

`vial_grid` uses grid-uniform `vial_role`/`vial_solution`/
`vial_allowed_solutions` fields (applied to every position, mirroring
`vial_dead_volume_ul`). All three fields are optional and default to
unset/accept-all; a vial with no `role` is simply never a candidate for
automatic selection.

## Cap State

A vial (or vial-grid position) opts into durable capper tracking by
setting `capped` explicitly — `true` if it starts physically capped,
`false` if uncapped:

```yaml
labware:
  reagent:
    type: vial
    name: reagent
    capped: true
    height: 40.0
    diameter: 15.0
    location: {x: 5.0, y: 5.0, z: 20.0}
    capacity_ul: 500.0
    working_volume_ul: 400.0
```

`vial_grid` uses a grid-uniform `vial_capped` field, mirroring
`vial_role`/`vial_dead_volume_ul`. Leaving `capped` unset (the default)
means the vial has **no** durable cap state at all — it is not
capper-managed, and neither the `decap`/`cap` protocol commands nor a
`transfer`'s `require_uncapped` check constrain it. `capped` participates
in the fluid-state session fingerprint exactly like `role`/`solution`: a
deck edit that changes it invalidates an old durable session, so a fresh
one seeds from the new value.

At runtime the durable state is one of `capped`, `uncapped`, or
`reconciliation_required` (see [Fluid State
Tracking](fluid-state.md) for the create/resume session lifecycle this
hangs off of, and [Protocol YAML: Capper
commands](protocol-yaml.md#capper-commands) for `decap`/`cap` and
`transfer`'s `require_uncapped`). `reconciliation_required` means a
`decap`/`cap` action's physical outcome was uncertain (sensor timeout or
a contradictory reading) — an operator must resolve it (via
`DataStore.resolve_cap_operation`) before that vial can be decapped,
capped, or referenced by a `require_uncapped` check again.

## Existing Nested Deck Files

Existing deck YAML files do not need to be rewritten. CubOS continues to load
the holder and its nested labware, and all existing protocol addresses keep
working. It also assigns a flat canonical ID internally:

| Existing address | Canonical address |
| --- | --- |
| `plate_holder.plate.A1` | `plate_holder__plate.A1` |
| `vial_holder.vial_1` | `vial_holder__vials.vial_1` |

The holder object and its old address remain available for holder geometry,
slots, and existing protocols. New deck files should use a top-level plate or
`vial_grid` when the holder does not need to be directly addressed. Saved
liquid state uses only the canonical ID, so an old alias and its canonical
address cannot create duplicate containers.

## Surface Z And Outer Geometry

Grid movement is determined by calibration and spacing, not by outer
dimensions:

- `calibration.a1.z` is the plate surface or vial-rim reference used by
  protocol commands.
- Plate `length`, `width`, `height`, `well_depth`, and `well_attributes` are
  optional physical metadata. Plate height is not used as a motion Z value, and
  the current plate bounding box carries the XY footprint rather than an outer Z
  height. If `well_attributes` is omitted it defaults to empty: the plate
  remains fully addressable, and existing deck files load unchanged. Consumers
  that need a particular attribute must handle its absence.
- Vial `height` and `diameter` are optional physical metadata. If they are
  omitted, the vial remains fully addressable and its outer bounding-box fields
  remain unset. Existing direct and holder-nested vials that specify these
  dimensions continue to load unchanged.
- Legacy holder definitions retain their fixture geometry and seat/surface
  offsets. Those offsets continue to determine nested labware Z for existing
  deck files.

### Per-Well Attributes

`well_attributes` is an open mapping of per-well physical facts. It exists so a
plate can record whatever its drawing specifies without a schema change:

```yaml
well_attributes:
  diameter: 6.86
  bottom: flat
```

Values may be numbers, strings, or booleans. No key is required and none is
reserved — CubOS does not interpret the contents, and no motion, calibration,
or protocol behavior reads them. A plate that omits the block entirely gets an
empty mapping.

Because the mapping is open, a misspelled key is stored rather than rejected.
That is the deliberate trade for not having to change the schema for every new
measurement; treat the keys as a convention between whoever writes the
definition and whoever consumes it. `diameter` in millimeters is the
established key — the fluid-state layout payload reads it, and
`sbs_96_wellplate` supplies it.

The same block is accepted on a top-level plate and on a plate nested inside a
holder.

For arithmetic, read numeric attributes through `WellPlate.well_attribute_float(key)`,
which returns `None` when the key is absent or its value is not a number,
rather than indexing `well_attributes` directly.

7. **Validate before running hardware.**

    ```bash
    python -m cubos.tools.validate_setup \
      packages/core/configs/gantry/cub_xl_asmi.yaml \
      packages/core/configs/deck/asmi_deck.yaml \
      packages/core/configs/protocol/asmi/indentation.yaml
    ```

    Replace the example paths with the gantry, deck, and protocol YAML files
    for your setup.

## Moving-bed visualization

The 2D deck map uses the same deck/work coordinates as Gantry Control, including for `cnc.y_axis_motion: bed`. Labware stays fixed against the axes; HEAD appears at the reported work X/Y, and mounted tools appear at that position plus their calibrated offsets. A zero-offset pipette therefore sits on HEAD. This is a coordinate map, not an animation of the moving bed.

Load `cub_deck.yaml` to continue editing the saved working deck. Importing a seed/template copies it over the working deck and can replace saved calibration positions. To refresh a display after a config update, refresh the page rather than re-importing the original seed.
