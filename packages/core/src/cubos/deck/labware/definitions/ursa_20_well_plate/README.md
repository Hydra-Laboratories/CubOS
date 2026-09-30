# Ursa 20-Well Plate

20-well plate ("ursa plate"), 4 rows x 5 columns on a 12.5 mm pitch in
both x and y. Uses the SBS ANSI SLAS 1-2004 footprint (127.76 x 85.47 mm)
as a placeholder footprint — verify against the actual plate before
assuming it applies as-is.

Maps to `cubos.src.deck.labware.well_plate.WellPlate`.

## Files

| File | Purpose |
| --- | --- |
| `20WellPlate.yaml` | Class-attribute template consumed by the definitions registry. |

## Standard dimensions

| Attribute | Value | Notes |
| --- | --- | --- |
| Outer footprint | 127.76 x 85.47 mm | ANSI SLAS 1-2004 placeholder |
| Well grid | 5 x 4 (A1 - D5) | Letters are rows, numbers are columns |
| Well pitch | 12.5 mm in both x and y | |
| A1 offset from plate corner | (38.88, 23.99) mm | Computed placeholder (grid centered on footprint) in `calibration.a1` — not from a vendor drawing |
| Plate height (outer) | unset | No vendor drawing supplied — set before hardware use |
| Well depth (inside) | unset | No vendor drawing supplied — set before hardware use |
| Default capacity | unset | No vendor drawing supplied — set before hardware use |

## Usage

Reference the definition from a deck YAML via `load_name`, then override
at least `calibration.a1` and `calibration.a2` with real deck coordinates,
and supply `height`/`well_depth`/`capacity_ul` once you have real
measurements for your specific plate:

```yaml
labware:
  my_plate:
    load_name: ursa_20_well_plate
    calibration:
      a1: { x: -17.88, y: -42.23, z: -20.0 }
      a2: { x: -17.88, y: -54.73, z: -20.0 }
    x_offset: 12.5    # positive spacing magnitude; A1/A2 determine direction
    y_offset: 12.5
    height: 17.0        # measure from your plate before running on hardware
    well_depth: 13.0     # measure from your plate before running on hardware
    capacity_ul: 1500.0  # measure/lookup from your plate before running on hardware
```

`height`/`well_depth` directly affect pipette and instrument Z travel —
do not run on hardware with unset or guessed values.

## Compatibility

- Any deck supported by PANDA-BEAR / cubos that has room for a 127.76 x
  85.47 mm footprint.
- Not a printable part — this is a catalog definition for a commercially
  manufactured consumable, so there is no STL/GLB.
