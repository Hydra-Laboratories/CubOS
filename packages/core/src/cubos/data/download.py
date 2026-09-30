"""The Operator "Download Data" export: a campaign's raw data as plain CSVs.

Layout::

    README.txt          what every file and column means
    metadata.json       campaign details, export format, CubOS version
    samples.csv         one row per experiment (labware + well)
    <instrument>/measurements.csv  one row per measurement: settings and recorded values
    <instrument>/curves.csv        one row per recorded data point
    uvvis/spectra_wide.csv     wavelength rows, one column per spectrum
    asmi/raw/                  legacy per-well ASMI CSVs + metadata.csv
    camera/images/             captured image files
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Callable, Optional

from cubos.data.exports import (
    MEASUREMENT_TABLES,
    CampaignNotFoundError,
    DataDatabaseNotFoundError,
    MeasurementExportNotFoundError,
    MeasurementTable,
    _add_camera_images,
    _campaign_exists,
    _connect,
    _ensure_tables,
    _export_timestamp,
    _filename_for_row,
    _json_array,
    _measurement_table_rows,
    _metadata_csv,
    _present_tables,
    _raw_samples_csv,
    _sample_arrays,
    _validate_equal_lengths,
)

FORMAT_VERSION = "cubos.download-data.v1"

def export_campaign_data_zip(db_path: str | Path, campaign_id: int) -> bytes:
    """Build the Download Data ZIP for one campaign."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise DataDatabaseNotFoundError(f"Data database not found: {path}")

    with closing(_connect(path)) as conn:
        _ensure_tables(conn, ("campaigns", "experiments"))
        if not _campaign_exists(conn, campaign_id):
            raise CampaignNotFoundError(f"Campaign {campaign_id} not found")
        campaign = dict(conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone())
        present = _present_tables(conn)
        tables = [
            (table, rows)
            for table in MEASUREMENT_TABLES
            if table.table in present
            for rows in [_measurement_table_rows(conn, table, campaign_id)]
            if rows
        ]
        if not tables:
            raise MeasurementExportNotFoundError(
                f"No instrument measurements found for campaign {campaign_id}"
            )
        experiments = conn.execute(
            "SELECT * FROM experiments WHERE campaign_id = ? ORDER BY id", (campaign_id,),
        ).fetchall()

    start = min(_parse_time(row["timestamp"]) for _, rows in tables for row in rows)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        files: dict[str, int] = {}

        def put(name: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
            zf.writestr(name, _csv(columns, rows))
            files[name] = len(rows)

        put("samples.csv", *_samples(experiments, tables))
        for table, rows in tables:
            _WRITERS[table.instrument](zf, put, rows, start)
        zf.writestr("metadata.json", _metadata(campaign, tables, files))
        zf.writestr("README.txt", _readme(campaign, [t.instrument for t, _ in tables], files))
    return archive.getvalue()


def _uvvis(zf, put, rows, start) -> None:
    measurements, curves, spectra = [], [], []
    for row in rows:
        wavelengths = _json_array(row["wavelengths"], "wavelengths")
        intensities = _json_array(row["intensities"], "intensities")
        _validate_equal_lengths(wavelengths=wavelengths, intensities=intensities)
        measurements.append({
            **_common(row, start),
            "integration_time_s": row["integration_time_s"],
            "points": len(wavelengths),
        })
        for wavelength, intensity in zip(wavelengths, intensities):
            curves.append({
                "measurement_id": row["id"], "well_id": row["experiment_well_id"] or "",
                "wavelength_nm": wavelength, "intensity": intensity,
            })
        spectra.append((row, wavelengths, intensities))
    put("uvvis/measurements.csv", list(measurements[0]), measurements)
    put("uvvis/curves.csv", ["measurement_id", "well_id", "wavelength_nm", "intensity"], curves)

    grid = spectra[0][1]
    if all(len(w) == len(grid) for _, w, _ in spectra):
        headers = _unique([
            f"{row['experiment_well_id'] or 'no_well'} {_parse_time(row['timestamp']):%H:%M:%S}"
            for row, _, _ in spectra
        ])
        wide = [
            {"wavelength_nm": grid[i], **{h: s[2][i] for h, s in zip(headers, spectra)}}
            for i in range(len(grid))
        ]
        put("uvvis/spectra_wide.csv", ["wavelength_nm", *headers], wide)


def _asmi(zf, put, rows, start) -> None:
    measurements, curves, legacy = [], [], []
    for row in rows:
        times, z, raw, corrected, directions = _sample_arrays(row)
        measurements.append({
            **_common(row, start),
            "points": len(z),
            "z_target_mm": row["z_target_mm"],
            "step_size_mm": row["step_size_mm"],
            "force_limit_n": row["force_limit_n"],
            "force_exceeded": bool(row["force_exceeded"]),
            "baseline_avg_n": row["baseline_avg"],
            "baseline_std_n": row["baseline_std"],
        })
        for i in range(len(z)):
            curves.append({
                "measurement_id": row["id"], "well_id": row["experiment_well_id"] or "",
                "sample_index": i, "timestamp_s": times[i],
                "z_position_mm": z[i], "raw_force_n": raw[i], "corrected_force_n": corrected[i],
                "direction": directions[i] or "",
            })
        legacy_row = {**dict(row), "measurement_id": row["id"], "well_id": row["experiment_well_id"]}
        legacy.append(legacy_row)
        zf.writestr(f"asmi/raw/{_filename_for_row(legacy_row)}", _raw_samples_csv(legacy_row))
    zf.writestr("asmi/raw/metadata.csv", _metadata_csv(legacy))
    put("asmi/measurements.csv", list(measurements[0]), measurements)
    put("asmi/curves.csv", [
        "measurement_id", "well_id", "sample_index", "timestamp_s",
        "z_position_mm", "raw_force_n", "corrected_force_n", "direction",
    ], curves)


def _potentiostat(zf, put, rows, start) -> None:
    measurements, curves = [], []
    for row in rows:
        times = _json_array(row["time_s"], "time_s")
        voltages = _json_array(row["voltage_v"], "voltage_v")
        currents = (
            _json_array(row["current_a"], "current_a")
            if row["current_a"] not in (None, "") else [None] * len(times)
        )
        _validate_equal_lengths(time_s=times, voltage_v=voltages, current_a=currents)
        measurements.append({
            **_common(row, start),
            "technique": row["technique"],
            "points": len(times),
            "vendor": row["vendor"],
            "device_id": row["device_id"],
            "channel": row["channel"],
            "sample_period_s": row["sample_period_s"],
            "duration_s": row["duration_s"],
            "step_potential_v": row["step_potential_v"],
            "step_current_a": row["step_current_a"],
            "scan_rate_v_s": row["scan_rate_v_s"],
            "step_size_v": row["step_size_v"],
            "cycles": row["cycles"],
            "started_at": _export_timestamp(row["started_at"]),
            "stopped_at": _export_timestamp(row["stopped_at"]),
            "aborted": "" if row["aborted"] is None else bool(row["aborted"]),
            "stop_reason": row["stop_reason"],
        })
        for i in range(len(times)):
            curves.append({
                "measurement_id": row["id"], "well_id": row["experiment_well_id"] or "",
                "technique": row["technique"], "sample_index": i,
                "time_s": times[i], "voltage_v": voltages[i], "current_a": currents[i],
            })
    put("potentiostat/measurements.csv", list(measurements[0]), measurements)
    put("potentiostat/curves.csv", [
        "measurement_id", "well_id", "technique", "sample_index", "time_s", "voltage_v", "current_a",
    ], curves)


def _uv_curing(zf, put, rows, start) -> None:
    summary = [{
        **_common(row, start),
        "intensity_percent": row["intensity_percent"],
        "exposure_time_s": row["exposure_time_s"],
        "cure_started_at": datetime.fromtimestamp(row["cure_timestamp_s"], timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ") if row["cure_timestamp_s"] else "",
    } for row in rows]
    put("uv_curing/measurements.csv", list(summary[0]), summary)


def _filmetrics(zf, put, rows, start) -> None:
    summary = [{
        **_common(row, start),
        "thickness_nm": row["thickness_nm"],
        "goodness_of_fit": row["goodness_of_fit"],
    } for row in rows]
    put("filmetrics/measurements.csv", list(summary[0]), summary)


def _camera(zf, put, rows, start) -> None:
    images = {
        entry["measurement_id"]: entry
        for entry in _add_camera_images(zf, rows, folder="camera/images")
    }
    summary = [{
        **_common(row, start),
        "image_file": images[row["id"]]["file"],
        "image_status": images[row["id"]]["status"],
        "source_path": images[row["id"]]["image_path"],
    } for row in rows]
    put("camera/measurements.csv", list(summary[0]), summary)


_WRITERS: dict[str, Callable[..., None]] = {
    "uvvis": _uvvis,
    "asmi": _asmi,
    "potentiostat": _potentiostat,
    "uv_curing": _uv_curing,
    "filmetrics": _filmetrics,
    "camera": _camera,
}


def _samples(
    experiments: list[sqlite3.Row],
    tables: list[tuple[MeasurementTable, list[sqlite3.Row]]],
) -> tuple[list[str], list[dict[str, Any]]]:
    instrument_of: dict[int, list[str]] = {}
    for table, rows in tables:
        for row in rows:
            instrument_of.setdefault(row["experiment_id"], []).append(table.instrument)
    instruments = [table.instrument for table, _ in tables]
    wells: dict[tuple[str, str], dict[str, Any]] = {}
    for exp in experiments:
        labware_key = exp["labware_key"] if "labware_key" in exp.keys() else exp["labware_name"]
        well = wells.setdefault((labware_key, exp["well_id"] or ""), {
            "labware_key": labware_key,
            "labware_name": exp["labware_name"],
            "well_id": exp["well_id"] or "",
            "first_measured_at": _export_timestamp(exp["created_at"]),
            "experiment_ids": [],
            **{f"{instrument}_measurements": 0 for instrument in instruments},
        })
        well["experiment_ids"].append(str(exp["id"]))
        contents = _parse_contents(exp["contents"])
        if contents:
            well["contents"] = "; ".join(
                f"{item.get('source', '?')} {item['volume_ul']:g} uL" if "volume_ul" in item else json.dumps(item)
                for item in contents
            )
            well["added_volume_ul"] = sum(item.get("volume_ul", 0) for item in contents) or ""
        for instrument in instrument_of.get(exp["id"], []):
            well[f"{instrument}_measurements"] += 1
    out = [{**well, "experiment_ids": ";".join(well["experiment_ids"])} for well in wells.values()]
    columns = [
        "labware_key", "labware_name", "well_id", "contents", "added_volume_ul",
        *(f"{instrument}_measurements" for instrument in instruments),
        "first_measured_at", "experiment_ids",
    ]
    return columns, out


def _parse_contents(raw: Optional[str]) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return [{"note": raw}]
    return [item if isinstance(item, dict) else {"note": item} for item in parsed] if isinstance(parsed, list) else []


def _metadata(
    campaign: dict[str, Any],
    tables: list[tuple[MeasurementTable, list[sqlite3.Row]]],
    files: dict[str, int],
) -> str:
    return json.dumps({
        "format": FORMAT_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cubos_version": _cubos_version(),
        "campaign": {
            **campaign,
            "created_at": _export_timestamp(campaign.get("created_at")),
        },
        "measurement_counts": {table.instrument: len(rows) for table, rows in tables},
        "files": files,
    }, indent=2, default=str)


def _readme(campaign: dict[str, Any], instruments: list[str], files: dict[str, int]) -> str:
    lines = [
        f"CubOS campaign {campaign['id']} - {campaign.get('description') or ''}".rstrip(" -"),
        f"Exported {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC ({FORMAT_VERSION})",
        "",
        "Raw data only: the values CubOS recorded, with no fitting or analysis.",
        "Every CSV opens directly in Excel, pandas, or R. Times are UTC.",
        "measured_at = when the measurement was saved; elapsed_min = minutes since the",
        "first measurement in this campaign, for time-series plots.",
        "",
        "FILES",
    ]
    lines += [f"  {name:<32} {count} rows" for name, count in sorted(files.items())]
    lines += ["  metadata.json                    campaign details and CubOS version"]
    if "asmi" in instruments:
        lines += ["  asmi/raw/                        original per-well ASMI CSVs + metadata.csv"]
    if "camera" in instruments:
        lines += ["  camera/images/                   captured image files"]
    lines += [""]
    lines += [
        "samples.csv",
        "  One row per labware well: what was dispensed into it and how many measurements",
        "  each instrument took there. experiment_ids links to the experiment_id column",
        "  in every measurements.csv.",
        "",
    ]
    for instrument in instruments:
        lines += _README_SECTIONS[instrument] + [""]
    return "\n".join(lines)


_README_SECTIONS: dict[str, list[str]] = {
    "uvvis": [
        "UV-VIS (uvvis/)",
        "  measurements.csv  one row per spectrum: integration time and number of points.",
        "  curves.csv        one row per wavelength point (long format, for plotting by well).",
        "  spectra_wide.csv  the same spectra side by side: one wavelength column, then one",
        "                    column per spectrum named '<well> <HH:MM:SS>'.",
        "  Intensity is the detector reading as reported by the spectrometer.",
    ],
    "asmi": [
        "ASMI INDENTATION (asmi/)",
        "  measurements.csv  one row per indentation: target Z, step size, force limit,",
        "                    whether the limit was hit, and the pre-contact baseline force.",
        "  curves.csv        one row per force sample: timestamp, Z position, raw force,",
        "                    baseline-corrected force, and direction.",
        "  raw/              the same curves in the original per-well ASMI CSV format, with",
        "                    metadata.csv, for existing analysis scripts.",
    ],
    "potentiostat": [
        "POTENTIOSTAT (potentiostat/)",
        "  measurements.csv  one row per run: technique, device, and the programmed",
        "                    parameters (potentials, scan rate, cycles, duration).",
        "  curves.csv        one row per sample: time (s), potential (V), current (A).",
    ],
    "uv_curing": [
        "UV CURING (uv_curing/)",
        "  measurements.csv  one row per cure: lamp intensity (% of max), exposure time,",
        "                    start time.",
    ],
    "filmetrics": [
        "FILM THICKNESS (filmetrics/)",
        "  measurements.csv  one row per measurement: thickness (nm) and goodness of fit,",
        "                    as reported by the instrument.",
    ],
    "camera": [
        "CAMERA (camera/)",
        "  measurements.csv  one row per image, with its file in camera/images/.",
        "                    image_status 'missing on server' means the file was gone at export time.",
    ],
}


def _common(row: sqlite3.Row, start: datetime) -> dict[str, Any]:
    measured = _parse_time(row["timestamp"])
    return {
        "measurement_id": row["id"],
        "experiment_id": row["experiment_id"],
        "labware_key": row["experiment_labware_key"],
        "well_id": row["experiment_well_id"] or "",
        "measured_at": _export_timestamp(row["timestamp"]),
        "elapsed_min": round((measured - start).total_seconds() / 60.0, 3),
    }


def _parse_time(value: Any) -> datetime:
    text = str(value).replace("T", " ").rstrip("Z")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.min


def _unique(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        out.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    return out


def _csv(columns: list[str], rows: list[dict[str, Any]]) -> str:
    handle = io.StringIO()
    writer = csv.DictWriter(handle, columns, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _cell(row.get(k)) for k in columns})
    return handle.getvalue()


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        return float(f"{value:.12g}")
    return value


def _cubos_version() -> str:
    try:
        return importlib_metadata.version("cubos")
    except importlib_metadata.PackageNotFoundError:
        return "unknown"
