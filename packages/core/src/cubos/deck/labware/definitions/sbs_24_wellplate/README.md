# SBS 24-Well Plate

Generic SBS ANSI-standard 24-well microplate template: standard SLAS
1-2004 footprint with a 6 column x 4 row grid on the SLAS 4-2004 19 mm
pitch.

Maps to `cubos.src.deck.labware.well_plate.WellPlate`.

## Files

| File | Purpose |
| --- | --- |
| `SBS24WellPlate.yaml` | Class-attribute template consumed by the definitions registry. |

## Standard dimensions

| Attribute | Value | Notes |
| --- | --- | --- |
| Outer footprint | 127.76 x 85.47 mm | ANSI SLAS 1-2004 |
| Plate height (outer) | 20.2 mm | Rim → underside of plate; override for vendor variants |
| Well depth (inside) | 17.4 mm | Rim → inside floor; typical flat-bottom 24-well |
| Well grid | 6 x 4 (A1 - D6) | Letters are rows, numbers are columns |
| Well pitch | 19.0 mm in both x and y | ANSI SLAS 4-2004 24-well pitch; verified against a physical plate with a ruler on 2026-08-26 |
| A1 offset from plate corner | (16.38, 14.24) mm | Centers the 19 mm grid on the standard footprint |
| Default capacity | 3400 uL | Override for vendor-specific variants |
| Default working volume | 2000 uL | Override as needed |

## Usage

Reference the definition from a deck YAML via `load_name`, then override
at least `calibration.a1` and `calibration.a2` with real deck coordinates:

```yaml
labware:
  my_plate:
    load_name: sbs_24_wellplate
    calibration:
      a1: { x: -17.88, y: -42.23, z: -20.0 }
      a2: { x: -17.88, y: -60.23, z: -20.0 }
    x_offset: 19.0    # positive spacing magnitude; A1/A2 determine direction
    y_offset: 19.0
```

Vendor-specific variants (well diameter, skirt height, capacity) should
override the relevant fields (`height`, `well_depth`, `well_attributes`,
`capacity_ul`, `working_volume_ul`) in the deck YAML. The pair
`(height, well_depth)` is intentionally separate: `height` is the outer
dimension (rim → underside), `well_depth` is the inside (rim → sample
floor).

## Compatibility

- Any deck supported by PANDA-BEAR / cubos that has room for a 127.76 x
  85.47 mm footprint.
- Not a printable part — this is a catalog definition for a commercially
  manufactured consumable, so there is no STL/GLB.
