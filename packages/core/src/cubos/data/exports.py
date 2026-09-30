"""Campaign result summaries and CSV export helpers for CubOS runtime data."""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import sqlite3
import zipfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MeasurementTable:
    instrument: str
    table: str


@dataclass(frozen=True)
class CampaignSummary:
    campaign_id: int
    campaign_description: str
    created_at: str
    latest_measurement_at: str | None
    experiment_count: int
    well_count: int
    measurement_count: int
    measurement_counts: dict[str, int]
    asmi_measurement_count: int


MEASUREMENT_TABLES = (
    MeasurementTable("uvvis", "uvvis_measurements"),
    MeasurementTable("filmetrics", "filmetrics_measurements"),
    MeasurementTable("uv_curing", "uv_curing_measurements"),
    MeasurementTable("camera", "camera_measurements"),
    MeasurementTable("asmi", "asmi_measurements"),
    MeasurementTable("potentiostat", "potentiostat_measurements"),
)


class DataExportError(Exception):
    """Base class for CubOS data export/query failures."""


class DataDatabaseNotFoundError(DataExportError):
    """Raised when the configured runtime database is missing."""


class DataSchemaError(DataExportError):
    """Raised when a database exists but lacks required CubOS tables."""


class CampaignNotFoundError(DataExportError):
    """Raised when a campaign-specific export targets an unknown campaign."""


class MeasurementExportNotFoundError(DataExportError):
    """Raised when the requested export has no measurement rows."""


class MeasurementDataError(DataExportError):
    """Raised when stored measurement rows are malformed."""


def export_campaign_results_csvs(
    db_path: str | Path,
    campaign_id: int,
    output_dir: str | Path = "data/results",
) -> list[Path]:
    """Export one campaign to analysis-friendly CSV files in *output_dir*.

    The generic ZIP export preserves table shape. This filesystem export is for
    operators and analysis scripts: array-heavy instruments are flattened into
    one row per physical sample point rather than a JSON array in one cell.
    """
    db = Path(db_path).expanduser().resolve()
    if not db.is_file():
        raise DataDatabaseNotFoundError(f"Data database not found: {db}")

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    with closing(_connect(db)) as conn:
        _ensure_tables(conn, ("campaigns", "experiments"))
        campaign = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
        if campaign is None:
            raise CampaignNotFoundError(f"Campaign {campaign_id} not found")

        campaign_dict = dict(campaign)

        if campaign_dict.get("created_at"):
            campaign_dict["created_at"] = _export_timestamp(
                campaign_dict["created_at"]
            )

        run_dir = output_root / _result_dir_name(campaign)
        run_dir.mkdir(parents=True, exist_ok=True)

        written.append(_write_csv_path(
            run_dir / "campaign.csv",
            _rows_csv(list(campaign_dict.keys()), [campaign_dict])
        ))
        written.append(_write_csv_path(
            run_dir / "experiments.csv",
            _experiments_csv(conn, campaign_id),
        ))

        manifest_rows: list[dict[str, Any]] = []
        for table in MEASUREMENT_TABLES:
            if table.table not in _present_tables(conn):
                continue
            rows = _measurement_table_rows(conn, table, campaign_id)
            if not rows:
                continue
            filename = f"{table.instrument}.csv"
            csv_text = _sane_measurement_csv(table.instrument, rows)
            written.append(_write_csv_path(run_dir / filename, csv_text))
            manifest_rows.append({
                "instrument": table.instrument,
                "row_count": _csv_data_row_count(csv_text),
                "file": filename,
            })

        written.append(_write_csv_path(
            run_dir / "manifest.csv",
            _result_manifest_csv(manifest_rows),
        ))
    return written


def list_campaign_summaries(db_path: str | Path) -> list[CampaignSummary]:
    """Return campaign rows with measurement metadata for the CubOS UI/API."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        return []

    with closing(_connect(path)) as conn:
        _ensure_tables(conn, ("campaigns", "experiments"))
        rows = conn.execute(
            """
            SELECT
                c.id AS campaign_id,
                c.description AS campaign_description,
                c.created_at,
                COUNT(DISTINCT e.id) AS experiment_count,
                COUNT(DISTINCT e.well_id) AS well_count
            FROM campaigns c
            LEFT JOIN experiments e ON e.campaign_id = c.id
            GROUP BY c.id, c.description, c.created_at
            """
        ).fetchall()
        present_tables = _present_tables(conn)
        measurement_counts = _campaign_measurement_counts(conn, present_tables)
        latest_measurements = _campaign_latest_measurements(conn, present_tables)

    summaries = []
    for row in rows:
        campaign_id = row["campaign_id"]
        counts = {
            table.instrument: measurement_counts.get(campaign_id, {}).get(
                table.instrument, 0,
            )
            for table in MEASUREMENT_TABLES
        }
        summaries.append(
            CampaignSummary(
                **dict(row),
                latest_measurement_at=latest_measurements.get(campaign_id),
                measurement_count=sum(counts.values()),
                measurement_counts=counts,
                asmi_measurement_count=counts["asmi"],
            )
        )

    summaries.sort(
        key=lambda summary: (
            summary.latest_measurement_at or summary.created_at,
            summary.campaign_id,
        ),
        reverse=True,
    )
    return summaries


def _add_camera_images(
    zip_handle: zipfile.ZipFile, rows: list[sqlite3.Row], folder: str = "images",
) -> list[dict[str, Any]]:
    """Bundle each camera measurement's image file into the archive.

    ``camera_measurements.image_path`` is a server-local filesystem path;
    the zip is the only artifact that leaves the machine, so the referenced
    files must travel inside it. A missing source file is recorded in the
    returned manifest rows instead of failing the whole export.
    """
    manifest: list[dict[str, Any]] = []
    for row in rows:
        source = Path(str(row["image_path"]))
        entry = {
            "measurement_id": row["id"],
            "image_path": str(source),
        }
        if source.is_file():
            arcname = f"{folder}/camera_{row['id']}_{source.name}"
            zip_handle.write(source, arcname)
            entry["file"] = arcname
            entry["status"] = "included"
        else:
            entry["file"] = ""
            entry["status"] = "missing on server"
        manifest.append(entry)
    return manifest


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tables(conn: sqlite3.Connection, tables: tuple[str, ...]) -> None:
    present = _present_tables(conn)
    missing = [table for table in tables if table not in present]
    if missing:
        raise DataSchemaError(
            f"Data database is missing table(s): {', '.join(missing)}"
        )


def _present_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        ).fetchall()
    }


def _campaign_exists(conn: sqlite3.Connection, campaign_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM campaigns WHERE id = ?",
        (campaign_id,),
    ).fetchone()
    return row is not None


def _campaign_measurement_counts(
    conn: sqlite3.Connection,
    present_tables: set[str],
) -> dict[int, dict[str, int]]:
    counts: dict[int, dict[str, int]] = {}
    for table in MEASUREMENT_TABLES:
        if table.table not in present_tables:
            continue
        rows = conn.execute(
            f"""
            SELECT e.campaign_id, COUNT(m.id) AS measurement_count
            FROM {table.table} m
            JOIN experiments e ON e.id = m.experiment_id
            GROUP BY e.campaign_id
            """
        ).fetchall()
        for row in rows:
            campaign_counts = counts.setdefault(row["campaign_id"], {})
            campaign_counts[table.instrument] = row["measurement_count"]
    return counts


def _campaign_latest_measurements(
    conn: sqlite3.Connection,
    present_tables: set[str],
) -> dict[int, str]:
    latest: dict[int, str] = {}
    for table in MEASUREMENT_TABLES:
        if table.table not in present_tables:
            continue
        rows = conn.execute(
            f"""
            SELECT e.campaign_id, MAX(m.timestamp) AS latest_measurement_at
            FROM {table.table} m
            JOIN experiments e ON e.id = m.experiment_id
            GROUP BY e.campaign_id
            """
        ).fetchall()
        for row in rows:
            timestamp = row["latest_measurement_at"]
            if timestamp is None:
                continue
            campaign_id = row["campaign_id"]
            if campaign_id not in latest or timestamp > latest[campaign_id]:
                latest[campaign_id] = timestamp
    return latest


def _measurement_table_rows(
    conn: sqlite3.Connection,
    table: MeasurementTable,
    campaign_id: int,
) -> list[sqlite3.Row]:
    labware_key = (
        "e.labware_key" if "labware_key" in _table_columns(conn, "experiments")
        else "e.labware_name"
    )
    return conn.execute(
        f"""
        SELECT
            m.*,
            {labware_key} AS experiment_labware_key,
            e.labware_name AS experiment_labware_name,
            e.well_id AS experiment_well_id,
            e.contents AS experiment_contents,
            e.created_at AS experiment_created_at
        FROM {table.table} m
        JOIN experiments e ON e.id = m.experiment_id
        WHERE e.campaign_id = ?
        ORDER BY m.id
        """,
        (campaign_id,),
    ).fetchall()


def _experiments_csv(conn: sqlite3.Connection, campaign_id: int) -> str:
    columns = _table_columns(conn, "experiments")

    rows = conn.execute(
        """
        SELECT *
        FROM experiments
        WHERE campaign_id = ?
        ORDER BY id
        """,
        (campaign_id,),
    ).fetchall()

    export_rows = []

    for row in rows:
        row_dict = dict(row)

        if row_dict.get("created_at"):
            row_dict["created_at"] = _export_timestamp(
                row_dict["created_at"]
            )

        export_rows.append(row_dict)

    return _dict_rows_csv(columns, export_rows)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    return [
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    ]

def _rows_csv(
    columns: list[str],
    rows: list[sqlite3.Row | dict[str, Any]],
) -> str:
    handle = io.StringIO()
    writer = csv.writer(handle, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_format_cell(row[column]) for column in columns])
    return handle.getvalue()


def _write_csv_path(path: Path, content: str) -> Path:
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def _result_dir_name(campaign: sqlite3.Row) -> str:
    timestamp = _timestamp_suffix(str(campaign["created_at"]))
    return f"campaign_{campaign['id']}_{timestamp}"


def _csv_data_row_count(csv_text: str) -> int:
    lines = [line for line in csv_text.splitlines() if line.strip()]
    return max(0, len(lines) - 1)


def _result_manifest_csv(rows: list[dict[str, Any]]) -> str:
    columns = ["instrument", "row_count", "file"]
    handle = io.StringIO()
    writer = csv.DictWriter(handle, columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return handle.getvalue()


def _sane_measurement_csv(instrument: str, rows: list[sqlite3.Row]) -> str:
    if instrument == "uvvis":
        return _uvvis_results_csv(rows)
    if instrument == "asmi":
        return _asmi_results_csv(rows)
    if instrument == "potentiostat":
        return _potentiostat_results_csv(rows)
    if instrument == "filmetrics":
        return _scalar_results_csv(
            rows,
            [
                "thickness_nm",
                "goodness_of_fit",
            ],
        )
    if instrument == "uv_curing":
        return _scalar_results_csv(
            rows,
            [
                "intensity_percent",
                "exposure_time_s",
                "cure_timestamp_s",
            ],
        )
    if instrument == "camera":
        return _scalar_results_csv(rows, ["image_path"])
    raise MeasurementDataError(f"Unsupported instrument export: {instrument}")


def _common_result_fields(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "measurement_id": row["id"],
        "experiment_id": row["experiment_id"],
        "labware_key": row["experiment_labware_key"],
        "labware_name": row["experiment_labware_name"],
        "well_id": row["experiment_well_id"] or "",
        "measurement_timestamp": _export_timestamp(row["timestamp"]),
        "experiment_created_at": _export_timestamp(row["experiment_created_at"]),
    }


def _uvvis_results_csv(rows: list[sqlite3.Row]) -> str:
    columns = [
        "measurement_id",
        "experiment_id",
        "labware_key",
        "labware_name",
        "well_id",
        "measurement_timestamp",
        "experiment_created_at",
        "sample_index",
        "wavelength_nm",
        "intensity_au",
        "integration_time_s",
    ]
    output_rows: list[dict[str, Any]] = []
    for row in rows:
        wavelengths = _json_array(row["wavelengths"], "wavelengths")
        intensities = _json_array(row["intensities"], "intensities")
        _validate_equal_lengths(wavelengths=wavelengths, intensities=intensities)
        for index, (wavelength, intensity) in enumerate(zip(wavelengths, intensities)):
            output_rows.append({
                **_common_result_fields(row),
                "sample_index": index,
                "wavelength_nm": wavelength,
                "intensity_au": intensity,
                "integration_time_s": row["integration_time_s"],
            })
    return _dict_rows_csv(columns, output_rows)


def _asmi_results_csv(rows: list[sqlite3.Row]) -> str:
    columns = [
        "measurement_id",
        "experiment_id",
        "labware_key",
        "labware_name",
        "well_id",
        "measurement_timestamp",
        "experiment_created_at",
        "sample_index",
        "sample_timestamp_s",
        "z_position_mm",
        "raw_force_n",
        "corrected_force_n",
        "direction",
        "baseline_avg_n",
        "baseline_std_n",
        "force_exceeded",
        "data_points",
        "step_size_mm",
        "z_target_mm",
        "force_limit_n",
    ]
    output_rows: list[dict[str, Any]] = []
    for row in rows:
        timestamps, z_positions, raw_forces, corrected_forces, directions = _sample_arrays(row)
        for index, (timestamp, z_pos, raw_force, corrected_force, direction) in enumerate(zip(
            timestamps,
            z_positions,
            raw_forces,
            corrected_forces,
            directions,
        )):
            output_rows.append({
                **_common_result_fields(row),
                "sample_index": index,
                "sample_timestamp_s": timestamp,
                "z_position_mm": z_pos,
                "raw_force_n": raw_force,
                "corrected_force_n": corrected_force,
                "direction": "" if direction is None else direction,
                "baseline_avg_n": row["baseline_avg"],
                "baseline_std_n": row["baseline_std"],
                "force_exceeded": bool(row["force_exceeded"]),
                "data_points": row["data_points"],
                "step_size_mm": row["step_size_mm"],
                "z_target_mm": row["z_target_mm"],
                "force_limit_n": row["force_limit_n"],
            })
    return _dict_rows_csv(columns, output_rows)


def _potentiostat_results_csv(rows: list[sqlite3.Row]) -> str:
    columns = [
        "measurement_id",
        "experiment_id",
        "labware_key",
        "labware_name",
        "well_id",
        "measurement_timestamp",
        "experiment_created_at",
        "sample_index",
        "technique",
        "time_s",
        "voltage_v",
        "current_a",
        "sample_period_s",
        "duration_s",
        "step_potential_v",
        "step_current_a",
        "scan_rate_v_s",
        "step_size_v",
        "cycles",
        "vendor",
        "device_id",
        "channel",
        "started_at",
        "stopped_at",
        "aborted",
        "stop_reason",
    ]
    output_rows: list[dict[str, Any]] = []
    for row in rows:
        times = _json_array(row["time_s"], "time_s")
        voltages = _json_array(row["voltage_v"], "voltage_v")
        currents = (
            _json_array(row["current_a"], "current_a")
            if row["current_a"] not in (None, "")
            else [None] * len(times)
        )
        _validate_equal_lengths(time_s=times, voltage_v=voltages, current_a=currents)
        for index, (time_s, voltage, current) in enumerate(zip(times, voltages, currents)):
            output_rows.append({
                **_common_result_fields(row),
                "sample_index": index,
                "technique": row["technique"],
                "time_s": time_s,
                "voltage_v": voltage,
                "current_a": "" if current is None else current,
                "sample_period_s": row["sample_period_s"],
                "duration_s": row["duration_s"],
                "step_potential_v": row["step_potential_v"],
                "step_current_a": row["step_current_a"],
                "scan_rate_v_s": row["scan_rate_v_s"],
                "step_size_v": row["step_size_v"],
                "cycles": row["cycles"],
                "vendor": row["vendor"],
                "device_id": row["device_id"],
                "channel": row["channel"],
                "started_at": _export_timestamp(row["started_at"]),
                "stopped_at": _export_timestamp(row["stopped_at"]),
                "aborted": "" if row["aborted"] is None else bool(row["aborted"]),
                "stop_reason": row["stop_reason"],
            })
    return _dict_rows_csv(columns, output_rows)


def _scalar_results_csv(rows: list[sqlite3.Row], value_columns: list[str]) -> str:
    columns = [
        "measurement_id",
        "experiment_id",
        "labware_key",
        "labware_name",
        "well_id",
        "measurement_timestamp",
        "experiment_created_at",
        *value_columns,
    ]
    output_rows = [
        {
            **_common_result_fields(row),
            **{column: row[column] for column in value_columns},
        }
        for row in rows
    ]
    return _dict_rows_csv(columns, output_rows)


def _dict_rows_csv(columns: list[str], rows: list[dict[str, Any]]) -> str:
    handle = io.StringIO()
    writer = csv.DictWriter(handle, columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _format_cell(row.get(column)) for column in columns})
    return handle.getvalue()


def _format_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return "base64:" + base64.b64encode(value).decode("ascii")
    if isinstance(value, str):
        return _format_json_text(value)
    return value


def _format_json_text(value: str) -> str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, (list, dict)):
        return json.dumps(parsed, ensure_ascii=False)
    return value


def _raw_samples_csv(row: sqlite3.Row) -> str:
    timestamps, z_positions, raw_forces, corrected_forces, directions = _sample_arrays(row)
    has_directions = any(direction not in (None, "") for direction in directions)
    handle = io.StringIO()
    writer = csv.writer(handle, lineterminator="\n")
    header = [
        "Timestamp(s)",
        "Z_Position(mm)",
        "Raw_Force(N)",
        "Corrected_Force(N)",
    ]
    if has_directions:
        header.append("Direction")
    writer.writerow(header)

    for timestamp, z_pos, raw_force, corrected_force, direction in zip(
        timestamps,
        z_positions,
        raw_forces,
        corrected_forces,
        directions,
    ):
        sample_row = [
            _format_optional(timestamp, 3),
            _format_optional(z_pos, 3),
            _format_optional(raw_force, 3),
            _format_optional(corrected_force, 3),
        ]
        if has_directions:
            sample_row.append("" if direction is None else str(direction))
        writer.writerow(sample_row)
    return handle.getvalue()


def _metadata_csv(rows: list[sqlite3.Row]) -> str:
    handle = io.StringIO()
    writer = csv.writer(handle, lineterminator="\n")
    writer.writerow(
        [
            "File",
            "Measurement_ID",
            "Test_Time",
            "Well",
            "Target_Z(mm)",
            "Step_Size(mm)",
            "Force_Limit(N)",
            "Baseline_Force(N)",
            "Baseline_Std(N)",
            "Force_Exceeded",
            "Data_Points",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                _filename_for_row(row),
                row["measurement_id"],
                _export_timestamp(row["timestamp"]),
                row["well_id"] or "",
                _format_optional(row["z_target_mm"], 3),
                _format_optional(row["step_size_mm"], 3),
                _format_optional(row["force_limit_n"], 1),
                _format_optional(row["baseline_avg"], 3),
                _format_optional(row["baseline_std"], 3),
                str(bool(row["force_exceeded"])),
                row["data_points"],
            ]
        )
    return handle.getvalue()


def _sample_arrays(
    row: sqlite3.Row,
) -> tuple[list[Any], list[Any], list[Any], list[Any], list[Any]]:
    z_positions = _json_array(row["z_positions"], "z_positions")
    raw_forces = _json_array(row["raw_forces"], "raw_forces")
    corrected_forces = _json_array(row["corrected_forces"], "corrected_forces")
    timestamps = _json_array(row["sample_timestamps"], "sample_timestamps")
    directions = _json_array(row["directions"], "directions")
    _validate_equal_lengths(
        z_positions=z_positions,
        raw_forces=raw_forces,
        corrected_forces=corrected_forces,
        sample_timestamps=timestamps,
        directions=directions,
    )
    return timestamps, z_positions, raw_forces, corrected_forces, directions


def _filename_for_row(row: sqlite3.Row) -> str:
    well = row["well_id"] or f"experiment_{row['measurement_id']}"
    timestamp = _timestamp_suffix(str(row["timestamp"]))
    return f"well_{well}_{timestamp}.csv"


def _timestamp_suffix(timestamp: str) -> str:
    match = re.match(
        r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})",
        timestamp,
    )
    if match is None:
        safe = re.sub(r"[^0-9A-Za-z]+", "_", timestamp).strip("_")
        return safe or "unknown_time"
    return "".join(match.groups()[:3]) + "_" + "".join(match.groups()[3:])


def _export_timestamp(timestamp: str | None) -> str:
    if not timestamp:
        return ""

    timestamp = str(timestamp)

    if " " in timestamp:
        timestamp = timestamp.replace(" ", "T")

    if not timestamp.endswith("Z"):
        timestamp += "Z"

    return timestamp


def _json_array(value: Any, field_name: str) -> list[Any]:
    if value is None:
        raise MeasurementDataError(f"ASMI field '{field_name}' is missing")
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, list):
        raise MeasurementDataError(
            f"ASMI field '{field_name}' must be a JSON array"
        )
    return parsed


def _validate_equal_lengths(**arrays: list[Any]) -> None:
    lengths = {name: len(value) for name, value in arrays.items()}
    expected = next(iter(lengths.values()), 0)
    mismatches = {
        name: length for name, length in lengths.items()
        if length != expected
    }
    if mismatches:
        details = ", ".join(f"{name}={length}" for name, length in lengths.items())
        raise MeasurementDataError(
            f"ASMI measurement arrays must have equal lengths: {details}"
        )


def _format_optional(value: Any, digits: int) -> str:
    if value is None:
        return ""
    return f"{float(value):.{digits}f}"
