# Data

CubOS stores campaign state, experiment rows, measurement results, and labware
contents in a local SQLite database. Hardware runs save results automatically —
no setup and no external database server required.

## Database Path

By default, results are written to:

```text
data/databases/panda_data.db
```

Set `CUBOS_DATA_DB_PATH` to write a run to a different file. After a
successful run, analysis-friendly CSV exports are also written under
`data/results/`.

## What Gets Stored

| Table | Contents |
| --- | --- |
| `campaigns` | Run grouping, config path metadata, creation time, status. |
| `experiments` | One row per measured labware location within a campaign. |
| `uvvis_measurements` | Wavelengths, intensities, integration time. |
| `filmetrics_measurements` | Thickness and goodness-of-fit. |
| `uv_curing_measurements` | UV intensity, exposure duration, and cure timestamp. |
| `camera_measurements` | Image path results. |
| `asmi_measurements` | Force/z/time series, baseline stats, force-limit metadata. |
| `potentiostat_measurements` | OCP/CA/CV/CP time, voltage, current, and technique metadata. |
| `labware` | Per-well or per-vial volume and contents tracking. |

`scan` stores one experiment row per well, `measure` one row for the target
position, and pipette `transfer` updates labware contents.

## Download Data ZIP

**Results → Download Data** in the Operator UI, or
`GET /api/v1/data/campaigns/<id>/data.zip`, gives one ZIP per campaign:

| File | Contents |
| --- | --- |
| `README.txt` | What every file and column means, written for this campaign. |
| `samples.csv` | One row per well: what was dispensed into it and how many measurements each instrument took. |
| `<instrument>/measurements.csv` | One row per measurement: instrument settings and recorded values. |
| `<instrument>/curves.csv` | One row per recorded data point (long format), ready to plot by well. |
| `uvvis/spectra_wide.csv` | Wavelength rows, one column per spectrum (`<well> <time>`). |
| `asmi/raw/` | The original per-well ASMI CSVs and `metadata.csv`, for existing scripts. |
| `camera/images/` | The captured image files. |
| `metadata.json` | Campaign details, CubOS version, and analysis settings. |

The ZIP holds raw data only: exactly what CubOS recorded, reshaped into plain
CSVs. It does no fitting or analysis (no stiffness, modulus, peaks, or
absorbance); do that in your own analysis tool from `curves.csv`. Every
`measurements.csv` has `well_id`, `measured_at` (UTC) and `elapsed_min`
(minutes since the campaign's first measurement) for time-series plots.

## CSV Export

Export helpers require pandas:

```bash
pip install pandas
```

List experiment IDs for a campaign:

```bash
python -m data.export_helpers \
  --db-path runs/example.db \
  campaign-experiments 1 \
  --csv runs/campaign_1_experiments.csv
```

Export all measurements for one experiment:

```bash
python -m data.export_helpers \
  --db-path runs/example.db \
  experiment-all 42 \
  --csv runs/experiment_42_measurements.csv
```

Export one supported instrument table for an experiment:

```bash
python -m data.export_helpers \
  --db-path runs/example.db \
  experiment-instrument 42 asmi \
  --csv runs/experiment_42_asmi.csv
```

If `--csv` is omitted, the helper prints the table to stdout.

For programmatic access — Python read/write APIs and `export_campaign_data_zip` — see the
[data API reference](reference/cubos/data/index.md).
