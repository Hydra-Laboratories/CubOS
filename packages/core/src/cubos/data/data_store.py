"""SQLite-backed data store for self-driving lab campaigns."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Mapping, Optional, Union

from cubos.instruments.filmetrics.models import MeasurementResult
from cubos.instruments.uv_curing.models import CureResult
from cubos.instruments.uvvis_ccs.models import UVVisSpectrum
from cubos.protocol_engine.measurements import InstrumentMeasurement, MeasurementType

if TYPE_CHECKING:
    from .fluid_state import FluidContainerSnapshot, FluidStateSnapshot, FluidStateSummary

DATA_DB_PATH_ENV = "CUBOS_DATA_DB_PATH"
SQLITE_MEMORY_DATABASE = ":memory:"
LEGACY_PACKAGE_DATABASE_PATH = Path(__file__).resolve().parent / "databases" / "panda_data.db"
logger = logging.getLogger(__name__)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def default_database_path() -> Path:
    """Return the default SQLite path for CubOS runtime data."""
    override = os.environ.get(DATA_DB_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cubos" / "panda_data.db"

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS fluid_state_sessions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    deck_path          TEXT    NOT NULL,
    deck_fingerprint   TEXT    NOT NULL,
    deck_descriptor_json TEXT  NOT NULL DEFAULT '{}',
    deck_snapshot_json TEXT    NOT NULL,
    layout_json        TEXT    NOT NULL,
    label              TEXT,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS campaigns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    description     TEXT    NOT NULL,
    deck_config     TEXT,
    board_config    TEXT,
    gantry_config   TEXT,
    protocol_config TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    status          TEXT    NOT NULL DEFAULT 'running',
    finished_at     TEXT,
    fluid_state_id  INTEGER REFERENCES fluid_state_sessions(id)
);

CREATE TABLE IF NOT EXISTS experiments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id),
    labware_key  TEXT    NOT NULL,
    labware_name TEXT    NOT NULL,
    well_id      TEXT,
    contents     TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS uvvis_measurements (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id     INTEGER NOT NULL REFERENCES experiments(id),
    wavelengths       TEXT    NOT NULL,
    intensities       TEXT    NOT NULL,
    integration_time_s REAL   NOT NULL,
    timestamp         TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS filmetrics_measurements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id   INTEGER NOT NULL REFERENCES experiments(id),
    thickness_nm    REAL,
    goodness_of_fit REAL,
    timestamp       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS uv_curing_measurements (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id     INTEGER NOT NULL REFERENCES experiments(id),
    intensity_percent REAL    NOT NULL,
    exposure_time_s   REAL    NOT NULL,
    cure_timestamp_s  REAL    NOT NULL,
    timestamp         TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS camera_measurements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id),
    image_path    TEXT    NOT NULL,
    timestamp     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS asmi_measurements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id   INTEGER NOT NULL REFERENCES experiments(id),
    sample_timestamps TEXT NOT NULL,
    z_positions     TEXT    NOT NULL,
    raw_forces      TEXT    NOT NULL,
    corrected_forces TEXT   NOT NULL,
    directions      TEXT    NOT NULL,
    baseline_avg    REAL    NOT NULL,
    baseline_std    REAL    NOT NULL,
    force_exceeded  INTEGER NOT NULL DEFAULT 0,
    data_points     INTEGER NOT NULL,
    step_size_mm    REAL,
    z_target_mm     REAL,
    force_limit_n   REAL,
    timestamp       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS potentiostat_measurements (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id     INTEGER NOT NULL REFERENCES experiments(id),
    technique         TEXT    NOT NULL,
    time_s            TEXT    NOT NULL,
    voltage_v         TEXT    NOT NULL,
    current_a         TEXT,
    sample_period_s   REAL,
    duration_s        REAL,
    step_potential_v  REAL,
    step_current_a    REAL,
    scan_rate_v_s     REAL,
    step_size_v       REAL,
    cycles            INTEGER,
    vendor            TEXT,
    device_id         TEXT,
    channel           INTEGER,
    started_at        TEXT,
    stopped_at        TEXT,
    aborted           INTEGER,
    stop_reason       TEXT,
    timestamp         TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS labware (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id       INTEGER NOT NULL REFERENCES campaigns(id),
    labware_key       TEXT    NOT NULL,
    labware_type      TEXT    NOT NULL,
    well_id           TEXT,
    total_volume_ul   REAL    NOT NULL,
    working_volume_ul REAL    NOT NULL,
    current_volume_ul REAL    NOT NULL DEFAULT 0.0,
    contents          TEXT,
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(campaign_id, labware_key, well_id)
);

CREATE TABLE IF NOT EXISTS fluid_containers (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    fluid_state_id     INTEGER NOT NULL REFERENCES fluid_state_sessions(id)
                                   ON DELETE CASCADE,
    labware_key        TEXT    NOT NULL,
    location_id        TEXT    NOT NULL DEFAULT '',
    labware_type       TEXT    NOT NULL,
    capacity_ul        REAL    NOT NULL CHECK (capacity_ul > 0),
    working_volume_ul  REAL    NOT NULL CHECK (
                                      working_volume_ul > 0
                                      AND working_volume_ul <= capacity_ul
                                  ),
    current_volume_ul  REAL    NOT NULL DEFAULT 0.0 CHECK (current_volume_ul >= 0),
    composition_json   TEXT    NOT NULL DEFAULT '{}',
    version            INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(fluid_state_id, labware_key, location_id)
);

CREATE TABLE IF NOT EXISTS fluid_operations (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    fluid_state_id           INTEGER NOT NULL REFERENCES fluid_state_sessions(id)
                                        ON DELETE CASCADE,
    operation_key            TEXT    NOT NULL UNIQUE,
    operation_type           TEXT    NOT NULL CHECK (
                                        operation_type IN ('transfer', 'mix')
                                    ),
    source_labware_key       TEXT    NOT NULL,
    source_location_id       TEXT    NOT NULL DEFAULT '',
    destination_labware_key  TEXT    NOT NULL,
    destination_location_id  TEXT    NOT NULL DEFAULT '',
    volume_ul                REAL    NOT NULL CHECK (volume_ul > 0),
    composition_json         TEXT    NOT NULL,
    parameters_json          TEXT    NOT NULL DEFAULT '{}',
    source_version           INTEGER NOT NULL,
    destination_version      INTEGER NOT NULL,
    status                   TEXT    NOT NULL CHECK (
                                        status IN (
                                            'started',
                                            'applied',
                                            'reconciliation_required',
                                            'cancelled',
                                            'reconciled'
                                        )
                                    ),
    campaign_id              INTEGER REFERENCES campaigns(id),
    detail                   TEXT,
    created_at               TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT    NOT NULL DEFAULT (datetime('now')),
    applied_at               TEXT
);

CREATE TABLE IF NOT EXISTS tip_containers (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    fluid_state_id     INTEGER NOT NULL REFERENCES fluid_state_sessions(id)
                                   ON DELETE CASCADE,
    rack_key           TEXT    NOT NULL,
    slot_id            TEXT    NOT NULL,
    tip_length_mm      REAL    NOT NULL CHECK (tip_length_mm > 0),
    status             TEXT    NOT NULL CHECK (
                                   status IN (
                                       'available',
                                       'reserved',
                                       'attached',
                                       'consumed',
                                       'reconciliation_required'
                                   )
                               ),
    version            INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(fluid_state_id, rack_key, slot_id)
);

CREATE TABLE IF NOT EXISTS tip_operations (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    fluid_state_id           INTEGER NOT NULL REFERENCES fluid_state_sessions(id)
                                        ON DELETE CASCADE,
    operation_key            TEXT    NOT NULL UNIQUE,
    operation_type           TEXT    NOT NULL CHECK (
                                        operation_type IN ('pick_up_tip', 'drop_tip')
                                    ),
    rack_key                 TEXT    NOT NULL,
    slot_id                  TEXT    NOT NULL,
    tip_extension_mm         REAL    NOT NULL,
    previous_slot_status     TEXT    NOT NULL,
    slot_version             INTEGER NOT NULL,
    status                   TEXT    NOT NULL CHECK (
                                        status IN (
                                            'started',
                                            'applied',
                                            'reconciliation_required',
                                            'cancelled',
                                            'reconciled'
                                        )
                                    ),
    campaign_id              INTEGER REFERENCES campaigns(id),
    detail                   TEXT,
    created_at               TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT    NOT NULL DEFAULT (datetime('now')),
    applied_at               TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS tip_operations_one_pending_per_state
ON tip_operations(fluid_state_id)
WHERE status IN ('started', 'reconciliation_required');

CREATE TABLE IF NOT EXISTS cap_containers (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    fluid_state_id     INTEGER NOT NULL REFERENCES fluid_state_sessions(id)
                                   ON DELETE CASCADE,
    labware_key        TEXT    NOT NULL,
    location_id        TEXT    NOT NULL DEFAULT '',
    status             TEXT    NOT NULL CHECK (
                                   status IN (
                                       'capped',
                                       'uncapped',
                                       'reconciliation_required'
                                   )
                               ),
    version            INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(fluid_state_id, labware_key, location_id)
);

CREATE TABLE IF NOT EXISTS cap_operations (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    fluid_state_id           INTEGER NOT NULL REFERENCES fluid_state_sessions(id)
                                        ON DELETE CASCADE,
    operation_key            TEXT    NOT NULL UNIQUE,
    operation_type           TEXT    NOT NULL CHECK (
                                        operation_type IN ('decap', 'cap')
                                    ),
    labware_key              TEXT    NOT NULL,
    location_id              TEXT    NOT NULL DEFAULT '',
    previous_status          TEXT    NOT NULL,
    container_version        INTEGER NOT NULL,
    status                   TEXT    NOT NULL CHECK (
                                        status IN (
                                            'started',
                                            'applied',
                                            'reconciliation_required',
                                            'cancelled',
                                            'reconciled'
                                        )
                                    ),
    campaign_id              INTEGER REFERENCES campaigns(id),
    detail                   TEXT,
    created_at               TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT    NOT NULL DEFAULT (datetime('now')),
    applied_at               TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS cap_operations_one_pending_per_state
ON cap_operations(fluid_state_id)
WHERE status IN ('started', 'reconciliation_required');

CREATE TABLE IF NOT EXISTS pipette_attachment (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    fluid_state_id        INTEGER NOT NULL REFERENCES fluid_state_sessions(id)
                                       ON DELETE CASCADE,
    pipette_key           TEXT    NOT NULL DEFAULT 'pipette',
    rack_key              TEXT,
    slot_id               TEXT,
    tip_extension_mm      REAL,
    contents_known_empty  INTEGER NOT NULL DEFAULT 1,
    attachment_uncertain  INTEGER NOT NULL DEFAULT 0,
    version               INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    updated_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(fluid_state_id, pipette_key)
);
"""


class DataStore:
    """Local SQLite data store for experiment campaigns and measurements."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        resolved_path = default_database_path() if db_path is None else db_path
        self.db_path = resolved_path
        if str(resolved_path) == SQLITE_MEMORY_DATABASE:
            sqlite_path = SQLITE_MEMORY_DATABASE
        else:
            path = Path(resolved_path).expanduser()
            if db_path is None and LEGACY_PACKAGE_DATABASE_PATH.exists() and not path.exists():
                logger.warning(
                    "Legacy CubOS data DB exists at %s. The default runtime DB is "
                    "now %s; move/copy the legacy file manually if those results "
                    "are still needed.",
                    LEGACY_PACKAGE_DATABASE_PATH,
                    path,
                )
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise RuntimeError(
                    f"Cannot create CubOS data database directory for {path}: {exc}. "
                    f"Set {DATA_DB_PATH_ENV} to a writable SQLite path."
                ) from exc
            sqlite_path = str(path)
        try:
            self._conn = sqlite3.connect(sqlite_path)
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"Cannot open CubOS data database at {sqlite_path}: {exc}. "
                f"Set {DATA_DB_PATH_ENV} to a writable SQLite path."
            ) from exc
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        self._add_column_if_missing("campaigns", "finished_at", "TEXT")
        self._add_column_if_missing(
            "campaigns",
            "fluid_state_id",
            "INTEGER REFERENCES fluid_state_sessions(id)",
        )
        self._add_column_if_missing("experiments", "labware_key", "TEXT")
        self._add_column_if_missing(
            "fluid_state_sessions",
            "deck_descriptor_json",
            "TEXT NOT NULL DEFAULT '{}'",
        )
        self._conn.execute(
            "UPDATE experiments SET labware_key = labware_name "
            "WHERE labware_key IS NULL"
        )
        self._relax_asmi_metadata_columns()
        self._migrate_fluid_operations_schema()
        self._create_fluid_operation_indexes()
        self._conn.commit()

    def _migrate_fluid_operations_schema(self) -> None:
        """Expand the operation journal without weakening legacy data.

        SQLite cannot alter CHECK constraints in place. Rebuild only databases
        created by the earlier fluid-state prototype; fresh databases already
        have the current shape from ``_SCHEMA_SQL``.
        """
        table_row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'fluid_operations'"
        ).fetchone()
        if table_row is None:
            return
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(fluid_operations)")
        }
        table_sql = (table_row[0] or "").lower()
        if (
            "parameters_json" in columns
            and "'mix'" in table_sql
            and "'cancelled'" in table_sql
            and "'reconciled'" in table_sql
        ):
            return

        try:
            self._conn.executescript(
                """
            BEGIN IMMEDIATE;
            DROP TABLE IF EXISTS fluid_operations_new;
            CREATE TABLE fluid_operations_new (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                fluid_state_id           INTEGER NOT NULL REFERENCES fluid_state_sessions(id)
                                                    ON DELETE CASCADE,
                operation_key            TEXT    NOT NULL UNIQUE,
                operation_type           TEXT    NOT NULL CHECK (
                                                    operation_type IN ('transfer', 'mix')
                                                ),
                source_labware_key       TEXT    NOT NULL,
                source_location_id       TEXT    NOT NULL DEFAULT '',
                destination_labware_key  TEXT    NOT NULL,
                destination_location_id  TEXT    NOT NULL DEFAULT '',
                volume_ul                REAL    NOT NULL CHECK (volume_ul > 0),
                composition_json         TEXT    NOT NULL,
                parameters_json          TEXT    NOT NULL DEFAULT '{}',
                source_version           INTEGER NOT NULL,
                destination_version      INTEGER NOT NULL,
                status                   TEXT    NOT NULL CHECK (
                                                    status IN (
                                                        'started',
                                                        'applied',
                                                        'reconciliation_required',
                                                        'cancelled',
                                                        'reconciled'
                                                    )
                                                ),
                campaign_id              INTEGER REFERENCES campaigns(id),
                detail                   TEXT,
                created_at               TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at               TEXT    NOT NULL DEFAULT (datetime('now')),
                applied_at               TEXT
            );
            INSERT INTO fluid_operations_new (
                id, fluid_state_id, operation_key, operation_type,
                source_labware_key, source_location_id,
                destination_labware_key, destination_location_id,
                volume_ul, composition_json, parameters_json,
                source_version, destination_version, status, campaign_id,
                detail, created_at, updated_at, applied_at
            )
            SELECT
                id, fluid_state_id, operation_key, operation_type,
                source_labware_key, source_location_id,
                destination_labware_key, destination_location_id,
                volume_ul, composition_json, '{}',
                source_version, destination_version, status, campaign_id,
                detail, created_at, updated_at, applied_at
            FROM fluid_operations;
            DROP TABLE fluid_operations;
            ALTER TABLE fluid_operations_new RENAME TO fluid_operations;
            COMMIT;
            """
            )
        except BaseException:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise

    def _create_fluid_operation_indexes(self) -> None:
        duplicate = self._conn.execute(
            "SELECT fluid_state_id, COUNT(*) FROM fluid_operations "
            "WHERE status IN ('started', 'reconciliation_required') "
            "GROUP BY fluid_state_id HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        if duplicate is not None:
            raise RuntimeError(
                "Fluid state database has multiple pending operations for state "
                f"{duplicate[0]}; reconcile it before upgrading CubOS."
            )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "fluid_operations_one_pending_per_state "
            "ON fluid_operations(fluid_state_id) "
            "WHERE status IN ('started', 'reconciliation_required')"
        )

    def _add_column_if_missing(
        self,
        table_name: str,
        column_name: str,
        definition: str,
    ) -> None:
        columns = {
            row[1]
            for row in self._conn.execute(f"PRAGMA table_info({table_name})")
        }
        if column_name not in columns:
            self._conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
            )

    def _relax_asmi_metadata_columns(self) -> None:
        columns = {
            row[1]: row
            for row in self._conn.execute("PRAGMA table_info(asmi_measurements)")
        }
        metadata_columns = ("step_size_mm", "z_target_mm", "force_limit_n")
        if not any(columns[name][3] for name in metadata_columns if name in columns):
            return

        self._conn.executescript(
            """
            CREATE TABLE asmi_measurements_new (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id   INTEGER NOT NULL REFERENCES experiments(id),
                sample_timestamps TEXT NOT NULL,
                z_positions     TEXT    NOT NULL,
                raw_forces      TEXT    NOT NULL,
                corrected_forces TEXT   NOT NULL,
                directions      TEXT    NOT NULL,
                baseline_avg    REAL    NOT NULL,
                baseline_std    REAL    NOT NULL,
                force_exceeded  INTEGER NOT NULL DEFAULT 0,
                data_points     INTEGER NOT NULL,
                step_size_mm    REAL,
                z_target_mm     REAL,
                force_limit_n   REAL,
                timestamp       TEXT    NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO asmi_measurements_new (
                id, experiment_id, sample_timestamps, z_positions, raw_forces,
                corrected_forces, directions, baseline_avg, baseline_std,
                force_exceeded, data_points, step_size_mm, z_target_mm,
                force_limit_n, timestamp
            )
            SELECT
                id, experiment_id, sample_timestamps, z_positions, raw_forces,
                corrected_forces, directions, baseline_avg, baseline_std,
                force_exceeded, data_points, step_size_mm, z_target_mm,
                force_limit_n, timestamp
            FROM asmi_measurements;
            DROP TABLE asmi_measurements;
            ALTER TABLE asmi_measurements_new RENAME TO asmi_measurements;
            """
        )

    def create_campaign(
        self,
        description: str,
        deck_config: Optional[str] = None,
        board_config: Optional[str] = None,
        gantry_config: Optional[str] = None,
        protocol_config: Optional[str] = None,
        fluid_state_id: Optional[int] = None,
    ) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO campaigns (description, deck_config, board_config, "
                "gantry_config, protocol_config, fluid_state_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    description,
                    deck_config,
                    board_config,
                    gantry_config,
                    protocol_config,
                    fluid_state_id,
                ),
            )
        return cursor.lastrowid

    def finish_campaign(
        self,
        campaign_id: int,
        status: str,
        finished_at: Optional[str] = None,
    ) -> None:
        """Mark a campaign as finished with a terminal status."""
        if status not in {"completed", "failed"}:
            raise ValueError("Campaign status must be 'completed' or 'failed'")
        timestamp_expr = "datetime('now')" if finished_at is None else "?"
        params: tuple[Any, ...]
        if finished_at is None:
            params = (status, campaign_id)
        else:
            params = (status, finished_at, campaign_id)
        with self._conn:
            cursor = self._conn.execute(
                f"UPDATE campaigns SET status = ?, finished_at = {timestamp_expr} "
                "WHERE id = ?",
                params,
            )
        if cursor.rowcount == 0:
            raise ValueError(f"Campaign {campaign_id} not found")

    def attach_campaign_fluid_state(
        self,
        campaign_id: int,
        fluid_state_id: int,
    ) -> None:
        """Attach a durable fluid state to an existing campaign.

        Repeating the same attachment is safe. Replacing a campaign's state is
        rejected because its recorded operations and measurements must retain
        one unambiguous fluid-state identity.
        """
        if self._conn.in_transaction:
            raise RuntimeError(
                "Campaign fluid-state attachment cannot start inside an existing "
                "SQLite transaction; commit or roll it back first."
            )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            state = self._conn.execute(
                "SELECT 1 FROM fluid_state_sessions WHERE id = ?",
                (fluid_state_id,),
            ).fetchone()
            if state is None:
                raise ValueError(f"Fluid state {fluid_state_id} not found")

            campaign = self._conn.execute(
                "SELECT fluid_state_id FROM campaigns WHERE id = ?",
                (campaign_id,),
            ).fetchone()
            if campaign is None:
                raise ValueError(f"Campaign {campaign_id} not found")
            attached_state = campaign[0]
            if attached_state is not None and int(attached_state) != fluid_state_id:
                raise ValueError(
                    f"Campaign {campaign_id} is already attached to fluid state "
                    f"{attached_state}, not {fluid_state_id}."
                )

            self._conn.execute(
                "UPDATE campaigns SET fluid_state_id = ? WHERE id = ?",
                (fluid_state_id, campaign_id),
            )
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def get_campaign_fluid_state_id(self, campaign_id: int) -> int | None:
        """Return the fluid-state ID linked to a campaign, if any."""
        row = self._conn.execute(
            "SELECT fluid_state_id FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Campaign {campaign_id} not found")
        return None if row[0] is None else int(row[0])

    def create_experiment(
        self,
        campaign_id: int,
        labware_name: str,
        well_id: Optional[str],
        contents_json: Optional[str] = None,
        labware_key: Optional[str] = None,
    ) -> int:
        with self._conn:
            exp_id = self._create_experiment_row(
                campaign_id=campaign_id,
                labware_key=labware_key or labware_name,
                labware_name=labware_name,
                well_id=well_id,
                contents_json=contents_json,
            )
        return exp_id

    def _create_experiment_row(
        self,
        *,
        campaign_id: int,
        labware_key: str,
        labware_name: str,
        well_id: Optional[str],
        contents_json: Optional[str],
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO experiments "
            "(campaign_id, labware_key, labware_name, well_id, contents) "
            "VALUES (?, ?, ?, ?, ?)",
            (campaign_id, labware_key, labware_name, well_id, contents_json),
        )
        return cursor.lastrowid

    def log_experiment_measurement(
        self,
        *,
        campaign_id: int,
        labware_key: str,
        labware_name: str,
        well_id: Optional[str],
        contents_json: Optional[str],
        result: Union[
            InstrumentMeasurement, UVVisSpectrum, MeasurementResult, CureResult, str,
        ],
    ) -> tuple[int, int]:
        """Atomically create an experiment and its measurement row."""
        with self._conn:
            experiment_id = self._create_experiment_row(
                campaign_id=campaign_id,
                labware_key=labware_key,
                labware_name=labware_name,
                well_id=well_id,
                contents_json=contents_json,
            )
            measurement_id = self._log_measurement_row(experiment_id, result)
        return experiment_id, measurement_id

    def log_measurement(
        self,
        experiment_id: int,
        result: Union[
            InstrumentMeasurement, UVVisSpectrum, MeasurementResult, CureResult, str,
        ],
    ) -> int:
        """Log a measurement result, dispatching by type.

        Args:
            experiment_id: FK to the experiments table.
            result: An InstrumentMeasurement or measurement value.

        Returns:
            The newly inserted measurement row ID.

        Raises:
            TypeError: If *result* is not a recognised measurement type.
        """
        with self._conn:
            measurement_id = self._log_measurement_row(experiment_id, result)
        return measurement_id

    def _log_measurement_row(
        self,
        experiment_id: int,
        result: Union[
            InstrumentMeasurement, UVVisSpectrum, MeasurementResult, CureResult, str,
        ],
    ) -> int:
        if isinstance(result, InstrumentMeasurement):
            return self._log_instrument_measurement(experiment_id, result)
        if isinstance(result, UVVisSpectrum):
            return self._log_uvvis(experiment_id, result)
        if isinstance(result, MeasurementResult):
            return self._log_filmetrics(experiment_id, result)
        if isinstance(result, CureResult):
            return self._log_uv_curing(experiment_id, result)
        if isinstance(result, str):
            return self._log_camera(experiment_id, result)
        raise TypeError(
            f"Unsupported measurement type: {type(result).__name__}"
        )

    def _log_instrument_measurement(
        self,
        experiment_id: int,
        measurement: InstrumentMeasurement,
    ) -> int:
        if measurement.measurement_type == MeasurementType.UVVIS_SPECTRUM:
            wavelengths = tuple(measurement.payload["wavelength_nm"])
            intensities = tuple(measurement.payload["intensity_au"])
            integration_time_s = float(measurement.metadata["integration_time_s"])
            return self._log_uvvis_values(
                experiment_id=experiment_id,
                wavelengths=wavelengths,
                intensities=intensities,
                integration_time_s=integration_time_s,
            )

        if measurement.measurement_type == MeasurementType.ASMI_INDENTATION:
            return self._log_asmi(
                experiment_id=experiment_id,
                sample_timestamps=tuple(measurement.payload["sample_timestamps"]),
                z_positions=tuple(measurement.payload["z_positions_mm"]),
                raw_forces=tuple(measurement.payload["raw_forces_n"]),
                corrected_forces=tuple(measurement.payload["corrected_forces_n"]),
                directions=tuple(measurement.payload["directions"]),
                baseline_avg=float(measurement.metadata["baseline_avg"]),
                baseline_std=float(measurement.metadata["baseline_std"]),
                force_exceeded=bool(measurement.metadata["force_exceeded"]),
                data_points=int(measurement.metadata["data_points"]),
                step_size_mm=_optional_float(measurement.metadata.get("step_size_mm")),
                z_target_mm=_optional_float(measurement.metadata.get("z_target_mm")),
                force_limit_n=_optional_float(measurement.metadata.get("force_limit_n")),
            )

        if measurement.measurement_type == MeasurementType.FILMETRICS_THICKNESS:
            return self._log_filmetrics_values(
                experiment_id=experiment_id,
                thickness_nm=measurement.payload["thickness_nm"],
                goodness_of_fit=measurement.payload["goodness_of_fit"],
            )

        if measurement.measurement_type == MeasurementType.UV_CURING_EXPOSURE:
            return self._log_uv_curing_values(
                experiment_id=experiment_id,
                intensity_percent=float(measurement.payload["intensity_percent"]),
                exposure_time_s=float(measurement.payload["exposure_time_s"]),
                cure_timestamp_s=float(measurement.payload["cure_timestamp_s"]),
            )

        if measurement.measurement_type in {
            MeasurementType.POTENTIOSTAT_OCP,
            MeasurementType.POTENTIOSTAT_CA,
            MeasurementType.POTENTIOSTAT_CV,
            MeasurementType.POTENTIOSTAT_CP,
        }:
            payload = measurement.payload
            meta = measurement.metadata
            return self._log_potentiostat(
                experiment_id=experiment_id,
                technique=str(meta["technique"]),
                time_s=tuple(payload["time_s"]),
                voltage_v=tuple(payload["voltage_v"]),
                current_a=(
                    tuple(payload["current_a"])
                    if "current_a" in payload
                    else None
                ),
                sample_period_s=meta.get("sample_period_s"),
                duration_s=meta.get("duration_s"),
                step_potential_v=meta.get("step_potential_v"),
                step_current_a=meta.get("step_current_a"),
                scan_rate_v_s=meta.get("scan_rate_v_s"),
                step_size_v=meta.get("step_size_v"),
                cycles=meta.get("cycles"),
                vendor=meta.get("vendor"),
                device_id=meta.get("device_id"),
                channel=meta.get("channel"),
                started_at=meta.get("started_at"),
                stopped_at=meta.get("stopped_at"),
                aborted=meta.get("aborted"),
                stop_reason=meta.get("stop_reason"),
            )

        raise TypeError(
            "Unsupported instrument measurement type: "
            f"{measurement.measurement_type}"
        )

    def _log_uvvis(self, experiment_id: int, spectrum: UVVisSpectrum) -> int:
        return self._log_uvvis_values(
            experiment_id=experiment_id,
            wavelengths=spectrum.wavelengths,
            intensities=spectrum.intensities,
            integration_time_s=spectrum.integration_time_s,
        )

    def _log_uvvis_values(
        self,
        experiment_id: int,
        wavelengths: tuple[float, ...],
        intensities: tuple[float, ...],
        integration_time_s: float,
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO uvvis_measurements "
            "(experiment_id, wavelengths, intensities, integration_time_s) "
            "VALUES (?, ?, ?, ?)",
            (
                experiment_id,
                json.dumps(list(wavelengths)),
                json.dumps(list(intensities)),
                integration_time_s,
            ),
        )
        return cursor.lastrowid

    def _log_filmetrics(self, experiment_id: int, result: MeasurementResult) -> int:
        return self._log_filmetrics_values(
            experiment_id=experiment_id,
            thickness_nm=result.thickness_nm,
            goodness_of_fit=result.goodness_of_fit,
        )

    def _log_filmetrics_values(
        self,
        experiment_id: int,
        thickness_nm: Optional[float],
        goodness_of_fit: Optional[float],
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO filmetrics_measurements "
            "(experiment_id, thickness_nm, goodness_of_fit) VALUES (?, ?, ?)",
            (experiment_id, thickness_nm, goodness_of_fit),
        )
        return cursor.lastrowid

    def _log_uv_curing(self, experiment_id: int, result: CureResult) -> int:
        return self._log_uv_curing_values(
            experiment_id=experiment_id,
            intensity_percent=result.intensity_percent,
            exposure_time_s=result.exposure_time_s,
            cure_timestamp_s=result.timestamp,
        )

    def _log_uv_curing_values(
        self,
        experiment_id: int,
        intensity_percent: float,
        exposure_time_s: float,
        cure_timestamp_s: float,
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO uv_curing_measurements "
            "(experiment_id, intensity_percent, exposure_time_s, cure_timestamp_s) "
            "VALUES (?, ?, ?, ?)",
            (
                experiment_id,
                intensity_percent,
                exposure_time_s,
                cure_timestamp_s,
            ),
        )
        return cursor.lastrowid

    def _log_asmi(
        self,
        experiment_id: int,
        sample_timestamps: tuple[float, ...],
        z_positions: tuple[float, ...],
        raw_forces: tuple[float, ...],
        corrected_forces: tuple[float, ...],
        directions: tuple[str, ...],
        baseline_avg: float,
        baseline_std: float,
        force_exceeded: bool,
        data_points: int,
        step_size_mm: float,
        z_target_mm: float,
        force_limit_n: float,
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO asmi_measurements "
            "(experiment_id, sample_timestamps, z_positions, raw_forces, "
            "corrected_forces, directions, baseline_avg, baseline_std, "
            "force_exceeded, data_points, step_size_mm, z_target_mm, "
            "force_limit_n) "
            "VALUES (:experiment_id, :sample_timestamps, :z_positions, "
            ":raw_forces, :corrected_forces, :directions, :baseline_avg, "
            ":baseline_std, :force_exceeded, :data_points, :step_size_mm, "
            ":z_target_mm, :force_limit_n)",
            {
                "experiment_id": experiment_id,
                "sample_timestamps": json.dumps(list(sample_timestamps)),
                "z_positions": json.dumps(list(z_positions)),
                "raw_forces": json.dumps(list(raw_forces)),
                "corrected_forces": json.dumps(list(corrected_forces)),
                "directions": json.dumps(list(directions)),
                "baseline_avg": baseline_avg,
                "baseline_std": baseline_std,
                "force_exceeded": int(force_exceeded),
                "data_points": data_points,
                "step_size_mm": step_size_mm,
                "z_target_mm": z_target_mm,
                "force_limit_n": force_limit_n,
            },
        )
        return cursor.lastrowid

    def _log_potentiostat(
        self,
        experiment_id: int,
        technique: str,
        time_s: tuple[float, ...],
        voltage_v: tuple[float, ...],
        current_a: Optional[tuple[float, ...]],
        sample_period_s: Optional[float],
        duration_s: Optional[float],
        step_potential_v: Optional[float],
        step_current_a: Optional[float],
        scan_rate_v_s: Optional[float],
        step_size_v: Optional[float],
        cycles: Optional[int],
        vendor: Optional[str],
        device_id: Optional[str],
        channel: Optional[int],
        started_at: Optional[str],
        stopped_at: Optional[str],
        aborted: Optional[bool],
        stop_reason: Optional[str],
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO potentiostat_measurements "
            "(experiment_id, technique, time_s, voltage_v, current_a, "
            "sample_period_s, duration_s, step_potential_v, step_current_a, "
            "scan_rate_v_s, step_size_v, cycles, vendor, device_id, channel, "
            "started_at, stopped_at, aborted, stop_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                experiment_id,
                technique,
                json.dumps(list(time_s)),
                json.dumps(list(voltage_v)),
                json.dumps(list(current_a)) if current_a is not None else None,
                sample_period_s,
                duration_s,
                step_potential_v,
                step_current_a,
                scan_rate_v_s,
                step_size_v,
                cycles,
                vendor,
                device_id,
                channel,
                started_at,
                stopped_at,
                int(aborted) if aborted is not None else None,
                stop_reason,
            ),
        )
        return cursor.lastrowid

    def _log_camera(self, experiment_id: int, image_path: str) -> int:
        cursor = self._conn.execute(
            "INSERT INTO camera_measurements (experiment_id, image_path) "
            "VALUES (?, ?)",
            (experiment_id, image_path),
        )
        self._conn.commit()
        return cursor.lastrowid

    # ── Durable fluid state ────────────────────────────────────────────────

    def create_fluid_state(
        self,
        deck_path: str | Path,
        deck: Any,
        *,
        label: str | None = None,
        initial_fluids: Mapping[str, Any] | None = None,
    ) -> int:
        """Create a deck-associated fluid-state session."""
        from .fluid_state import create_fluid_state

        return create_fluid_state(
            self._conn,
            deck_path,
            deck,
            label=label,
            initial_fluids=initial_fluids,
        )

    def resume_fluid_state(
        self,
        fluid_state_id: int,
        deck_path: str | Path,
        deck: Any,
    ) -> int:
        """Validate and reopen an existing fluid-state session."""
        from .fluid_state import resume_fluid_state

        return resume_fluid_state(self._conn, fluid_state_id, deck_path, deck)

    def get_fluid_snapshot(self, fluid_state_id: int) -> FluidStateSnapshot:
        """Return a deterministic JSON-ready snapshot of fluid state."""
        from .fluid_state import get_fluid_snapshot

        return get_fluid_snapshot(self._conn, fluid_state_id)

    def list_fluid_states(self) -> list[FluidStateSummary]:
        """Return deterministic summaries of all fluid-state sessions."""
        from .fluid_state import list_fluid_states

        return list_fluid_states(self._conn)

    def get_fluid_container(
        self,
        fluid_state_id: int,
        labware_key: str,
        location_id: str,
    ) -> FluidContainerSnapshot:
        """Return one container's current durable volume/composition."""
        from .fluid_state import get_fluid_container

        return get_fluid_container(
            self._conn, fluid_state_id, labware_key, location_id,
        )

    def seed_fluid(
        self,
        fluid_state_id: int,
        target: Any,
        volume_ul: float,
        composition: Mapping[str, float] | None = None,
    ) -> None:
        """Replace one container's volume/composition with an explicit seed."""
        from .fluid_state import seed_fluid

        seed_fluid(
            self._conn,
            fluid_state_id,
            target,
            volume_ul,
            composition,
        )

    def begin_fluid_transfer(
        self,
        fluid_state_id: int,
        operation_key: str,
        source: Any,
        *target_args: Any,
        campaign_id: int | None = None,
    ) -> bool:
        """Preflight and journal a transfer before hardware acts.

        ``True`` means a new started operation was written and hardware should
        execute. ``False`` means this operation was already applied and should
        be skipped. Targets may be passed as ``(source, destination, volume)``
        or as resolved parts ``(source_key, source_location, destination_key,
        destination_location, volume)``.
        """
        from .fluid_state import begin_fluid_transfer

        if len(target_args) == 2:
            destination, volume_ul = target_args
        elif len(target_args) == 4:
            source_location, destination_key, destination_location, volume_ul = (
                target_args
            )
            if not isinstance(source, str) or not isinstance(destination_key, str):
                raise TypeError("Resolved fluid target keys must be strings.")
            source = (
                f"{source}.{source_location}" if source_location not in (None, "")
                else source
            )
            destination = (
                f"{destination_key}.{destination_location}"
                if destination_location not in (None, "")
                else destination_key
            )
        else:
            raise TypeError(
                "begin_fluid_transfer expects source/destination/volume or "
                "resolved source/destination key and location parts."
            )

        return begin_fluid_transfer(
            self._conn,
            fluid_state_id,
            operation_key,
            source,
            destination,
            volume_ul,
            campaign_id,
        )

    def complete_fluid_transfer(self, operation_key: str) -> None:
        """Atomically apply a successfully actuated transfer."""
        from .fluid_state import complete_fluid_transfer

        complete_fluid_transfer(self._conn, operation_key)

    def begin_fluid_mix(
        self,
        fluid_state_id: int,
        operation_key: str,
        target: Any,
        volume_ul: float,
        repetitions: int,
        speed: float,
        height: float = 0.0,
        *,
        campaign_id: int | None = None,
    ) -> bool:
        """Preflight and journal a net-zero mix before hardware acts."""
        from .fluid_state import begin_fluid_mix

        return begin_fluid_mix(
            self._conn,
            fluid_state_id,
            operation_key,
            target,
            volume_ul,
            repetitions,
            speed,
            height,
            campaign_id,
        )

    def complete_fluid_mix(self, operation_key: str) -> None:
        """Mark a successfully actuated, net-zero mix as applied."""
        from .fluid_state import complete_fluid_mix

        complete_fluid_mix(self._conn, operation_key)

    def mark_fluid_reconciliation_required(
        self,
        operation_key: str,
        detail: str,
    ) -> None:
        """Flag an indeterminate physical transfer for operator review."""
        from .fluid_state import mark_fluid_reconciliation_required

        mark_fluid_reconciliation_required(self._conn, operation_key, detail)

    def resolve_fluid_operation(
        self,
        operation_key: str,
        resolution: str,
        *,
        detail: str,
        source_volume_ul: float | None = None,
        source_composition: Mapping[str, float] | None = None,
        destination_volume_ul: float | None = None,
        destination_composition: Mapping[str, float] | None = None,
    ) -> None:
        """Resolve an uncertain operation using an explicit operator decision.

        ``resolution`` accepts ``applied``, ``not_applied``/``cancelled``, or
        ``partial``/``reconciled``. Partial reconciliation requires exact
        source and destination replacement volumes and compositions.
        """
        from .fluid_state import resolve_fluid_operation

        resolve_fluid_operation(
            self._conn,
            operation_key,
            resolution,
            detail=detail,
            source_volume_ul=source_volume_ul,
            source_composition=source_composition,
            destination_volume_ul=destination_volume_ul,
            destination_composition=destination_composition,
        )

    # ── Durable tip / pipette-attachment state ──────────────────────────────
    #
    # Tip and attached-pipette state hangs off the same fluid_state_id session
    # as fluid state (create_fluid_state/resume_fluid_state seed and verify
    # both). See cubos.data.tip_state for the full journal semantics.

    def get_tip_snapshot(self, fluid_state_id: int) -> Any:
        """Return a deterministic JSON-ready snapshot of tip/pipette state."""
        from .tip_state import get_tip_snapshot

        return get_tip_snapshot(self._conn, fluid_state_id)

    def begin_pick_up_tip(
        self,
        fluid_state_id: int,
        operation_key: str,
        rack_key: str,
        slot_id: str | None,
        tip_length_mm: float,
        *,
        campaign_id: int | None = None,
    ) -> tuple[bool, str, float]:
        """Preflight and journal a tip pickup before hardware acts.

        ``slot_id=None`` requests next-available selection within
        *rack_key*, using durable per-slot status. Returns
        ``(should_execute, resolved_slot_id, tip_extension_mm)``.
        """
        from .tip_state import begin_pick_up_tip

        return begin_pick_up_tip(
            self._conn,
            fluid_state_id,
            operation_key,
            rack_key,
            slot_id,
            tip_length_mm,
            campaign_id,
        )

    def complete_pick_up_tip(self, operation_key: str) -> None:
        """Atomically apply a successfully actuated tip pickup."""
        from .tip_state import complete_pick_up_tip

        complete_pick_up_tip(self._conn, operation_key)

    def begin_drop_tip(
        self,
        fluid_state_id: int,
        operation_key: str,
        *,
        campaign_id: int | None = None,
    ) -> tuple[bool, str, str]:
        """Preflight and journal dropping the currently attached tip."""
        from .tip_state import begin_drop_tip

        return begin_drop_tip(
            self._conn, fluid_state_id, operation_key, campaign_id,
        )

    def complete_drop_tip(self, operation_key: str) -> None:
        """Atomically apply a successfully actuated tip drop."""
        from .tip_state import complete_drop_tip

        complete_drop_tip(self._conn, operation_key)

    def mark_tip_reconciliation_required(
        self,
        operation_key: str,
        detail: str,
    ) -> None:
        """Flag an indeterminate physical tip action for operator review."""
        from .tip_state import mark_tip_reconciliation_required

        mark_tip_reconciliation_required(self._conn, operation_key, detail)

    def resolve_tip_operation(
        self,
        operation_key: str,
        resolution: str,
        *,
        detail: str,
        final_slot_status: str | None = None,
    ) -> None:
        """Resolve an uncertain tip operation using an operator decision."""
        from .tip_state import resolve_tip_operation

        resolve_tip_operation(
            self._conn,
            operation_key,
            resolution,
            detail=detail,
            final_slot_status=final_slot_status,
        )

    def restore_pipette_attachment(self, fluid_state_id: int, pipette: Any) -> None:
        """Restore the pipette's attached-tip extension after resume.

        Raises when attachment is uncertain -- callers must block liquid
        handling until it is reconciled.
        """
        from .tip_state import restore_pipette_attachment

        restore_pipette_attachment(self._conn, fluid_state_id, pipette)

    # ── Durable per-vial cap state ──────────────────────────────────────────
    #
    # Cap state hangs off the same fluid_state_id session as fluid/tip state
    # (create_fluid_state/resume_fluid_state seed and verify all three). See
    # cubos.data.cap_state for the full journal semantics.

    def get_cap_snapshot(self, fluid_state_id: int) -> Any:
        """Return a deterministic JSON-ready snapshot of cap state."""
        from .cap_state import get_cap_snapshot

        return get_cap_snapshot(self._conn, fluid_state_id)

    def get_cap_state(
        self,
        fluid_state_id: int,
        labware_key: str,
        location_id: str,
    ) -> Optional[str]:
        """Return one vial's durable cap status, or None if not capper-managed."""
        from .cap_state import get_cap_state

        return get_cap_state(self._conn, fluid_state_id, labware_key, location_id)

    def begin_cap_operation(
        self,
        fluid_state_id: int,
        operation_key: str,
        operation_type: str,
        labware_key: str,
        location_id: str,
        *,
        campaign_id: int | None = None,
    ) -> bool:
        """Preflight and journal a decap/cap operation before hardware acts."""
        from .cap_state import begin_cap_operation

        return begin_cap_operation(
            self._conn,
            fluid_state_id,
            operation_key,
            operation_type,
            labware_key,
            location_id,
            campaign_id,
        )

    def complete_cap_operation(self, operation_key: str) -> None:
        """Atomically apply a successfully actuated decap/cap."""
        from .cap_state import complete_cap_operation

        complete_cap_operation(self._conn, operation_key)

    def mark_cap_reconciliation_required(
        self,
        operation_key: str,
        detail: str,
    ) -> None:
        """Flag an indeterminate physical decap/cap action for operator review."""
        from .cap_state import mark_cap_reconciliation_required

        mark_cap_reconciliation_required(self._conn, operation_key, detail)

    def resolve_cap_operation(
        self,
        operation_key: str,
        resolution: str,
        *,
        detail: str,
        final_status: str | None = None,
    ) -> None:
        """Resolve an uncertain cap operation using an operator decision."""
        from .cap_state import resolve_cap_operation

        resolve_cap_operation(
            self._conn,
            operation_key,
            resolution,
            detail=detail,
            final_status=final_status,
        )

    # ── Labware tracking ────────────────────────────────────────────────────

    def register_labware(self, campaign_id: int, labware_key: str, labware: Any) -> None:
        """Register a labware item for volume/content tracking.

        For a WellPlate, one row is created per well.
        For a Vial, a single row is created (well_id = NULL).
        For a VialGrid, one row is created per canonical vial position.

        Raises:
            TypeError: If *labware* is not a supported volume-bearing Labware.
            ValueError: If *labware_key* is already registered for the given campaign.
        """
        from cubos.deck.labware.labware import Labware
        from cubos.deck.labware.vial_grid import VialGrid
        from cubos.deck.labware.well_plate import WellPlate
        from cubos.deck.labware.vial import Vial

        if not isinstance(labware, Labware):
            raise TypeError(
                f"Expected a Labware instance, got {type(labware).__name__}"
            )

        existing = self._conn.execute(
            "SELECT COUNT(*) FROM labware WHERE campaign_id = ? AND labware_key = ?",
            (campaign_id, labware_key),
        ).fetchone()[0]
        if existing > 0:
            raise ValueError(
                f"Labware '{labware_key}' already registered for campaign {campaign_id}"
            )

        with self._conn:
            if isinstance(labware, WellPlate):
                for well_id in labware.wells:
                    self._conn.execute(
                        "INSERT INTO labware (campaign_id, labware_key, labware_type, "
                        "well_id, total_volume_ul, working_volume_ul) "
                        "VALUES (?, ?, 'well_plate', ?, ?, ?)",
                        (campaign_id, labware_key, well_id,
                         labware.capacity_ul, labware.working_volume_ul),
                    )
            elif isinstance(labware, Vial):
                self._conn.execute(
                    "INSERT INTO labware (campaign_id, labware_key, labware_type, "
                    "total_volume_ul, working_volume_ul) "
                    "VALUES (?, ?, 'vial', ?, ?)",
                    (campaign_id, labware_key,
                     labware.capacity_ul, labware.working_volume_ul),
                )
            elif isinstance(labware, VialGrid):
                for position_id, vial in labware.vials.items():
                    self._conn.execute(
                        "INSERT INTO labware (campaign_id, labware_key, labware_type, "
                        "well_id, total_volume_ul, working_volume_ul) "
                        "VALUES (?, ?, 'vial_grid', ?, ?, ?)",
                        (
                            campaign_id,
                            labware_key,
                            position_id,
                            vial.capacity_ul,
                            vial.working_volume_ul,
                        ),
                    )
            else:
                raise TypeError(
                    f"Unsupported labware type: {type(labware).__name__}. "
                    f"Expected WellPlate, Vial, or VialGrid."
                )

    def record_dispense(
        self,
        campaign_id: int,
        labware_key: str,
        well_id: Optional[str],
        source_name: str,
        volume_ul: float,
    ) -> None:
        """Record a dispense into a labware slot, updating volume and contents."""
        if well_id is not None:
            where = "campaign_id = ? AND labware_key = ? AND well_id = ?"
            params = (campaign_id, labware_key, well_id)
        else:
            where = "campaign_id = ? AND labware_key = ? AND well_id IS NULL"
            params = (campaign_id, labware_key)

        row = self._conn.execute(
            f"SELECT id, contents, current_volume_ul, working_volume_ul "
            f"FROM labware WHERE {where}", params
        ).fetchone()

        if row is None:
            raise ValueError(
                f"Labware '{labware_key}' well '{well_id}' not registered "
                f"for campaign {campaign_id}"
            )

        try:
            existing = json.loads(row[1]) if row[1] else []
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Corrupt contents JSON for labware '{labware_key}' well '{well_id}' "
                f"in campaign {campaign_id}. Manual database inspection required."
            ) from exc
        existing.append({"source": source_name, "volume_ul": volume_ul})
        projected_volume = float(row[2]) + volume_ul
        working_volume = float(row[3])
        if projected_volume > working_volume:
            logger.warning(
                "Dispense into labware '%s' well '%s' in campaign %s exceeds "
                "working volume: %.3f uL > %.3f uL.",
                labware_key,
                well_id,
                campaign_id,
                projected_volume,
                working_volume,
            )

        with self._conn:
            self._conn.execute(
                f"UPDATE labware SET current_volume_ul = current_volume_ul + ?, "
                f"contents = ?, updated_at = datetime('now') WHERE {where}",
                (volume_ul, json.dumps(existing)) + params,
            )

    def record_transfer(
        self,
        campaign_id: int,
        source_labware_key: str,
        source_well_id: Optional[str],
        destination_labware_key: str,
        destination_well_id: Optional[str],
        volume_ul: float,
    ) -> None:
        """Record source depletion and destination dispense for a pipette transfer."""
        with self._conn:
            self._adjust_volume(
                campaign_id,
                source_labware_key,
                source_well_id,
                -volume_ul,
                contents_json=None,
            )
            destination_contents = self._contents_for_update(
                campaign_id,
                destination_labware_key,
                destination_well_id,
            )
            destination_contents.append({
                "source": source_labware_key,
                "volume_ul": volume_ul,
            })
            self._adjust_volume(
                campaign_id,
                destination_labware_key,
                destination_well_id,
                volume_ul,
                contents_json=json.dumps(destination_contents),
            )

    def _contents_for_update(
        self,
        campaign_id: int,
        labware_key: str,
        well_id: Optional[str],
    ) -> list[dict[str, Any]]:
        row = self._labware_row(campaign_id, labware_key, well_id)
        try:
            return json.loads(row["contents"]) if row["contents"] else []
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Corrupt contents JSON for labware '{labware_key}' well '{well_id}' "
                f"in campaign {campaign_id}. Manual database inspection required."
            ) from exc

    def _adjust_volume(
        self,
        campaign_id: int,
        labware_key: str,
        well_id: Optional[str],
        delta_ul: float,
        *,
        contents_json: Optional[str],
    ) -> None:
        row = self._labware_row(campaign_id, labware_key, well_id)
        projected_volume = float(row["current_volume_ul"]) + delta_ul
        working_volume = float(row["working_volume_ul"])
        if delta_ul > 0 and projected_volume > working_volume:
            logger.warning(
                "Dispense into labware '%s' well '%s' in campaign %s exceeds "
                "working volume: %.3f uL > %.3f uL.",
                labware_key,
                well_id,
                campaign_id,
                projected_volume,
                working_volume,
            )
        if contents_json is None:
            self._conn.execute(
                "UPDATE labware SET current_volume_ul = current_volume_ul + ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (delta_ul, row["id"]),
            )
        else:
            self._conn.execute(
                "UPDATE labware SET current_volume_ul = current_volume_ul + ?, "
                "contents = ?, updated_at = datetime('now') WHERE id = ?",
                (delta_ul, contents_json, row["id"]),
            )

    def _labware_row(
        self,
        campaign_id: int,
        labware_key: str,
        well_id: Optional[str],
    ) -> dict[str, Any]:
        if well_id is not None:
            where = "campaign_id = ? AND labware_key = ? AND well_id = ?"
            params = (campaign_id, labware_key, well_id)
        else:
            where = "campaign_id = ? AND labware_key = ? AND well_id IS NULL"
            params = (campaign_id, labware_key)
        cursor = self._conn.execute(
            f"SELECT id, contents, current_volume_ul, working_volume_ul "
            f"FROM labware WHERE {where}",
            params,
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError(
                f"Labware '{labware_key}' well '{well_id}' not registered "
                f"for campaign {campaign_id}"
            )
        columns = [description[0] for description in cursor.description]
        return dict(zip(columns, row))

    def get_contents(
        self,
        campaign_id: int,
        labware_key: str,
        well_id: Optional[str],
    ) -> Optional[List[dict]]:
        """Return the parsed contents list for a labware slot, or None."""
        if well_id is not None:
            where = "campaign_id = ? AND labware_key = ? AND well_id = ?"
            params = (campaign_id, labware_key, well_id)
        else:
            where = "campaign_id = ? AND labware_key = ? AND well_id IS NULL"
            params = (campaign_id, labware_key)

        row = self._conn.execute(
            f"SELECT contents FROM labware WHERE {where}", params
        ).fetchone()

        if row is None or row[0] is None:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Corrupt contents JSON for labware '{labware_key}' well '{well_id}' "
                f"in campaign {campaign_id}. Manual database inspection required."
            ) from exc

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> DataStore:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
