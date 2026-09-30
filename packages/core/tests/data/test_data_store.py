"""Tests for the DataStore SQLite persistence layer."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

import pytest

from cubos.data.data_store import DATA_DB_PATH_ENV, DataStore, default_database_path
from cubos.deck.labware.labware import Coordinate3D
from cubos.deck.labware.vial import Vial
from cubos.deck.labware.well_plate import WellPlate
from cubos.instruments.filmetrics.models import MeasurementResult
from cubos.instruments.uv_curing.models import CureResult
from cubos.instruments.uvvis_ccs.models import UVVisSpectrum
from cubos.protocol_engine.measurements import InstrumentMeasurement, MeasurementType


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_store() -> DataStore:
    """Create an in-memory DataStore for testing."""
    return DataStore(db_path=":memory:")


def _create_legacy_fluid_operations_table(
    connection: sqlite3.Connection,
) -> None:
    connection.executescript(
        """
        CREATE TABLE fluid_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fluid_state_id INTEGER NOT NULL,
            operation_key TEXT NOT NULL UNIQUE,
            operation_type TEXT NOT NULL CHECK (operation_type = 'transfer'),
            source_labware_key TEXT NOT NULL,
            source_location_id TEXT NOT NULL DEFAULT '',
            destination_labware_key TEXT NOT NULL,
            destination_location_id TEXT NOT NULL DEFAULT '',
            volume_ul REAL NOT NULL CHECK (volume_ul > 0),
            composition_json TEXT NOT NULL,
            source_version INTEGER NOT NULL,
            destination_version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('started', 'applied', 'reconciliation_required')
            ),
            campaign_id INTEGER,
            detail TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            applied_at TEXT
        );
        INSERT INTO fluid_operations (
            fluid_state_id, operation_key, operation_type,
            source_labware_key, destination_labware_key, volume_ul,
            composition_json, source_version, destination_version, status
        ) VALUES (
            1, 'legacy-transfer', 'transfer',
            'source', 'plate', 10.0, '{"water":10.0}', 1, 1, 'applied'
        );
        """
    )
    connection.commit()


def _make_uvvis_spectrum(n: int = 10) -> UVVisSpectrum:
    wavelengths = tuple(400.0 + i for i in range(n))
    intensities = tuple(0.1 * i for i in range(n))
    return UVVisSpectrum(
        wavelengths=wavelengths,
        intensities=intensities,
        integration_time_s=0.24,
    )


def _make_filmetrics_result() -> MeasurementResult:
    return MeasurementResult(thickness_nm=150.5, goodness_of_fit=0.95)


def _make_cure_result() -> CureResult:
    return CureResult(
        intensity_percent=45.0,
        exposure_time_s=2.5,
        timestamp=123.456,
    )


# ─── Schema creation ─────────────────────────────────────────────────────────


class TestSchemaCreation:

    def test_tables_exist(self):
        store = _make_store()
        cursor = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cursor.fetchall()}
        expected = {
            "campaigns", "experiments",
            "uvvis_measurements", "filmetrics_measurements",
            "uv_curing_measurements", "camera_measurements",
            "asmi_measurements", "labware",
        }
        assert expected.issubset(tables)
        store.close()

    def test_idempotent_table_creation(self):
        store = _make_store()
        store._create_tables()
        store._create_tables()
        store.close()

    def test_in_memory_db_works(self):
        store = _make_store()
        assert store._conn is not None
        store.close()

    def test_default_db_path_uses_env_override(self, monkeypatch, tmp_path):
        db_path = tmp_path / "runtime" / "panda_data.db"
        monkeypatch.setenv(DATA_DB_PATH_ENV, str(db_path))

        store = DataStore()
        store.close()

        assert db_path.is_file()

    def test_default_db_path_uses_user_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.delenv(DATA_DB_PATH_ENV, raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        assert default_database_path() == tmp_path / ".cubos" / "panda_data.db"

    def test_default_db_creation_error_names_override(self, monkeypatch, tmp_path):
        monkeypatch.delenv(DATA_DB_PATH_ENV, raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        def fail_mkdir(self, *args, **kwargs):
            raise PermissionError("read only")

        monkeypatch.setattr(Path, "mkdir", fail_mkdir)

        with pytest.raises(RuntimeError, match=DATA_DB_PATH_ENV):
            DataStore()

    def test_foreign_key_enforcement(self):
        store = _make_store()
        with pytest.raises(Exception):
            store.create_experiment(
                campaign_id=9999,
                labware_name="plate_1",
                well_id="A1",
                contents_json="[]",
            )
        store.close()

    def test_fluid_operation_rebuild_rolls_back_every_step_on_failure(self):
        connection = sqlite3.connect(":memory:")
        _create_legacy_fluid_operations_table(connection)
        store = object.__new__(DataStore)
        store.db_path = ":memory:"
        store._conn = connection

        def deny_final_rename(action, _arg1, _arg2, _database, _source):
            if action == sqlite3.SQLITE_ALTER_TABLE:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(deny_final_rename)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            store._migrate_fluid_operations_schema()
        connection.set_authorizer(None)

        assert connection.in_transaction is False
        columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(fluid_operations)"
            )
        }
        assert "parameters_json" not in columns
        assert connection.execute(
            "SELECT operation_key, status FROM fluid_operations"
        ).fetchall() == [("legacy-transfer", "applied")]
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'fluid_operations_new'"
        ).fetchone() is None
        store.close()


# ─── Campaign CRUD ────────────────────────────────────────────────────────────


class TestCampaignCRUD:

    @staticmethod
    def _fluid_state_id(store: DataStore) -> int:
        with store._conn:
            cursor = store._conn.execute(
                "INSERT INTO fluid_state_sessions "
                "(deck_path, deck_fingerprint, deck_snapshot_json, layout_json) "
                "VALUES (?, ?, ?, ?)",
                ("deck.yaml", "fingerprint", "{}", "{}"),
            )
        return int(cursor.lastrowid)

    def test_create_returns_id(self):
        store = _make_store()
        cid = store.create_campaign(description="test campaign")
        assert isinstance(cid, int)
        assert cid > 0
        store.close()

    def test_stores_description(self):
        store = _make_store()
        cid = store.create_campaign(description="MOF screening run")
        row = store._conn.execute(
            "SELECT description FROM campaigns WHERE id = ?", (cid,)
        ).fetchone()
        assert row[0] == "MOF screening run"
        store.close()

    def test_stores_config_paths(self):
        store = _make_store()
        cid = store.create_campaign(
            description="test",
            deck_config="mock/deck.yaml",
            board_config="legacy-board.yaml",
            gantry_config="mock/gantry.yaml",
            protocol_config="mock/protocol.yaml",
        )
        row = store._conn.execute(
            "SELECT deck_config, board_config, gantry_config, protocol_config "
            "FROM campaigns WHERE id = ?",
            (cid,),
        ).fetchone()
        assert row == ("mock/deck.yaml", "legacy-board.yaml",
                       "mock/gantry.yaml", "mock/protocol.yaml")
        store.close()

    def test_default_status(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        row = store._conn.execute(
            "SELECT status FROM campaigns WHERE id = ?", (cid,)
        ).fetchone()
        assert row[0] == "running"
        store.close()

    def test_finish_campaign_sets_status_and_finished_at(self):
        store = _make_store()
        cid = store.create_campaign(description="test")

        store.finish_campaign(cid, "completed", finished_at="2026-07-07 12:00:00")

        row = store._conn.execute(
            "SELECT status, finished_at FROM campaigns WHERE id = ?", (cid,)
        ).fetchone()
        assert row == ("completed", "2026-07-07 12:00:00")
        store.close()

    def test_attach_campaign_fluid_state_is_queryable_and_idempotent(self):
        store = _make_store()
        state_id = self._fluid_state_id(store)
        campaign_id = store.create_campaign(description="tracked")

        store.attach_campaign_fluid_state(campaign_id, state_id)
        store.attach_campaign_fluid_state(campaign_id, state_id)

        assert store.get_campaign_fluid_state_id(campaign_id) == state_id
        store.close()

    def test_attach_campaign_fluid_state_refuses_replacement(self):
        store = _make_store()
        first_state = self._fluid_state_id(store)
        second_state = self._fluid_state_id(store)
        campaign_id = store.create_campaign(
            description="tracked",
            fluid_state_id=first_state,
        )

        with pytest.raises(ValueError, match="already attached"):
            store.attach_campaign_fluid_state(campaign_id, second_state)

        assert store.get_campaign_fluid_state_id(campaign_id) == first_state
        store.close()

    def test_attach_campaign_fluid_state_validates_both_ids(self):
        store = _make_store()
        state_id = self._fluid_state_id(store)
        campaign_id = store.create_campaign(description="tracked")

        with pytest.raises(ValueError, match="Campaign 999 not found"):
            store.attach_campaign_fluid_state(999, state_id)
        with pytest.raises(ValueError, match="Fluid state 999 not found"):
            store.attach_campaign_fluid_state(campaign_id, 999)
        with pytest.raises(ValueError, match="Campaign 999 not found"):
            store.get_campaign_fluid_state_id(999)
        store.close()

    def test_two_connections_cannot_attach_different_states(self, tmp_path):
        db_path = tmp_path / "campaign-attachment.db"
        setup = DataStore(db_path)
        first_state = self._fluid_state_id(setup)
        second_state = self._fluid_state_id(setup)
        campaign_id = setup.create_campaign(description="concurrent attach")
        setup.close()

        barrier = threading.Barrier(2)
        results: list[tuple[int, str]] = []

        def attach(fluid_state_id: int) -> None:
            store = None
            try:
                store = DataStore(db_path)
                barrier.wait(timeout=5)
                store.attach_campaign_fluid_state(campaign_id, fluid_state_id)
                results.append((fluid_state_id, "attached"))
            except BaseException as exc:
                results.append(
                    (fluid_state_id, f"{type(exc).__name__}: {exc}")
                )
            finally:
                if store is not None:
                    store.close()

        threads = [
            threading.Thread(target=attach, args=(first_state,)),
            threading.Thread(target=attach, args=(second_state,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert all(not thread.is_alive() for thread in threads)
        assert [result for _, result in results].count("attached") == 1
        failures = [result for _, result in results if result != "attached"]
        assert len(failures) == 1
        assert "ValueError: Campaign" in failures[0]
        assert "already attached" in failures[0]

        winner = next(state for state, result in results if result == "attached")
        reopened = DataStore(db_path)
        assert reopened.get_campaign_fluid_state_id(campaign_id) == winner
        reopened.close()


# ─── Experiment CRUD ──────────────────────────────────────────────────────────


class TestExperimentCRUD:

    def test_create_returns_id(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        eid = store.create_experiment(
            campaign_id=cid,
            labware_name="plate_1",
            well_id="A1",
            contents_json="[]",
        )
        assert isinstance(eid, int)
        assert eid > 0
        store.close()

    def test_stores_labware_and_well(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        eid = store.create_experiment(
            campaign_id=cid,
            labware_key="plate_key",
            labware_name="plate_1",
            well_id="B3",
            contents_json='[{"source_name": "vial_1", "volume_ul": 50.0}]',
        )
        row = store._conn.execute(
            "SELECT labware_key, labware_name, well_id, contents "
            "FROM experiments WHERE id = ?",
            (eid,),
        ).fetchone()
        assert row[0] == "plate_key"
        assert row[1] == "plate_1"
        assert row[2] == "B3"
        parsed = json.loads(row[3])
        assert parsed[0]["source_name"] == "vial_1"
        store.close()


# ─── UVVis measurement logging ───────────────────────────────────────────────


class TestUVVisMeasurementLogging:

    def test_blob_round_trip(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        eid = store.create_experiment(cid, "plate_1", "A1", "[]")

        spectrum = _make_uvvis_spectrum(20)
        mid = store.log_measurement(eid, spectrum)
        assert isinstance(mid, int)

        row = store._conn.execute(
            "SELECT wavelengths, intensities, integration_time_s "
            "FROM uvvis_measurements WHERE id = ?",
            (mid,),
        ).fetchone()

        assert tuple(json.loads(row[0])) == spectrum.wavelengths
        assert tuple(json.loads(row[1])) == spectrum.intensities
        assert row[2] == pytest.approx(0.24)
        store.close()

    def test_integration_time_stored(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        eid = store.create_experiment(cid, "plate_1", "A1", "[]")

        spectrum = UVVisSpectrum(
            wavelengths=(500.0,), intensities=(0.5,), integration_time_s=1.5
        )
        mid = store.log_measurement(eid, spectrum)

        row = store._conn.execute(
            "SELECT integration_time_s FROM uvvis_measurements WHERE id = ?",
            (mid,),
        ).fetchone()
        assert row[0] == pytest.approx(1.5)
        store.close()


# ─── Filmetrics measurement logging ──────────────────────────────────────────


class TestFilmetricsMeasurementLogging:

    def test_stores_thickness_and_gof(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        eid = store.create_experiment(cid, "plate_1", "A1", "[]")

        result = _make_filmetrics_result()
        mid = store.log_measurement(eid, result)

        row = store._conn.execute(
            "SELECT thickness_nm, goodness_of_fit "
            "FROM filmetrics_measurements WHERE id = ?",
            (mid,),
        ).fetchone()
        assert row[0] == pytest.approx(150.5)
        assert row[1] == pytest.approx(0.95)
        store.close()

    def test_handles_none_values(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        eid = store.create_experiment(cid, "plate_1", "A1", "[]")

        result = MeasurementResult(thickness_nm=None, goodness_of_fit=None)
        mid = store.log_measurement(eid, result)

        row = store._conn.execute(
            "SELECT thickness_nm, goodness_of_fit "
            "FROM filmetrics_measurements WHERE id = ?",
            (mid,),
        ).fetchone()
        assert row[0] is None
        assert row[1] is None
        store.close()

    def test_routes_normalized_filmetrics_measurement(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        eid = store.create_experiment(cid, "plate_1", "A1", "[]")

        measurement = InstrumentMeasurement(
            measurement_type=MeasurementType.FILMETRICS_THICKNESS,
            payload={"thickness_nm": 222.2, "goodness_of_fit": 0.91},
            metadata={},
        )
        mid = store.log_measurement(eid, measurement)

        row = store._conn.execute(
            "SELECT thickness_nm, goodness_of_fit "
            "FROM filmetrics_measurements WHERE id = ?",
            (mid,),
        ).fetchone()
        assert row[0] == pytest.approx(222.2)
        assert row[1] == pytest.approx(0.91)
        store.close()


# ─── UV curing measurement logging ───────────────────────────────────────────


class TestUVCuringMeasurementLogging:

    def test_stores_cure_exposure(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        eid = store.create_experiment(cid, "plate_1", "A1", "[]")

        result = _make_cure_result()
        mid = store.log_measurement(eid, result)

        row = store._conn.execute(
            "SELECT intensity_percent, exposure_time_s, cure_timestamp_s "
            "FROM uv_curing_measurements WHERE id = ?",
            (mid,),
        ).fetchone()
        assert row[0] == pytest.approx(45.0)
        assert row[1] == pytest.approx(2.5)
        assert row[2] == pytest.approx(123.456)
        store.close()

    def test_routes_normalized_cure_exposure(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        eid = store.create_experiment(cid, "plate_1", "A1", "[]")

        measurement = InstrumentMeasurement(
            measurement_type=MeasurementType.UV_CURING_EXPOSURE,
            payload={
                "intensity_percent": 80.0,
                "exposure_time_s": 0.75,
                "cure_timestamp_s": 555.0,
            },
            metadata={},
        )
        mid = store.log_measurement(eid, measurement)

        row = store._conn.execute(
            "SELECT intensity_percent, exposure_time_s, cure_timestamp_s "
            "FROM uv_curing_measurements WHERE id = ?",
            (mid,),
        ).fetchone()
        assert row[0] == pytest.approx(80.0)
        assert row[1] == pytest.approx(0.75)
        assert row[2] == pytest.approx(555.0)
        store.close()


# ─── Camera measurement logging ──────────────────────────────────────────────


class TestCameraMeasurementLogging:

    def test_stores_image_path(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        eid = store.create_experiment(cid, "plate_1", "A1", "[]")

        mid = store.log_measurement(eid, "/images/A1_001.png")

        row = store._conn.execute(
            "SELECT image_path FROM camera_measurements WHERE id = ?",
            (mid,),
        ).fetchone()
        assert row[0] == "/images/A1_001.png"
        store.close()


# ─── Dispatch ─────────────────────────────────────────────────────────────────


class TestLogMeasurementDispatch:

    def test_routes_uvvis(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        eid = store.create_experiment(cid, "plate_1", "A1", "[]")
        mid = store.log_measurement(eid, _make_uvvis_spectrum())
        assert store._conn.execute(
            "SELECT COUNT(*) FROM uvvis_measurements WHERE id = ?", (mid,)
        ).fetchone()[0] == 1
        store.close()

    def test_routes_filmetrics(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        eid = store.create_experiment(cid, "plate_1", "A1", "[]")
        mid = store.log_measurement(eid, _make_filmetrics_result())
        assert store._conn.execute(
            "SELECT COUNT(*) FROM filmetrics_measurements WHERE id = ?", (mid,)
        ).fetchone()[0] == 1
        store.close()

    def test_routes_uv_curing(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        eid = store.create_experiment(cid, "plate_1", "A1", "[]")
        mid = store.log_measurement(eid, _make_cure_result())
        assert store._conn.execute(
            "SELECT COUNT(*) FROM uv_curing_measurements WHERE id = ?", (mid,)
        ).fetchone()[0] == 1
        store.close()

    def test_routes_camera(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        eid = store.create_experiment(cid, "plate_1", "A1", "[]")
        mid = store.log_measurement(eid, "/path/to/image.png")
        assert store._conn.execute(
            "SELECT COUNT(*) FROM camera_measurements WHERE id = ?", (mid,)
        ).fetchone()[0] == 1
        store.close()

    def test_unknown_type_raises_type_error(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        eid = store.create_experiment(cid, "plate_1", "A1", "[]")
        with pytest.raises(TypeError, match="Unsupported measurement type"):
            store.log_measurement(eid, 42)
        store.close()

    def test_routes_uvvis_instrument_measurement(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        eid = store.create_experiment(cid, "plate_1", "A1", "[]")

        measurement = InstrumentMeasurement(
            measurement_type=MeasurementType.UVVIS_SPECTRUM,
            payload={
                "wavelength_nm": [500.0, 501.0],
                "intensity_au": [0.5, 0.6],
            },
            metadata={"integration_time_s": 0.24},
        )
        mid = store.log_measurement(eid, measurement)
        assert store._conn.execute(
            "SELECT COUNT(*) FROM uvvis_measurements WHERE id = ?", (mid,)
        ).fetchone()[0] == 1
        store.close()

    def test_uvvis_instrument_measurement_blob_round_trip(self):
        store = _make_store()
        cid = store.create_campaign(description="test")
        eid = store.create_experiment(cid, "plate_1", "A1", "[]")

        measurement = InstrumentMeasurement(
            measurement_type=MeasurementType.UVVIS_SPECTRUM,
            payload={
                "wavelength_nm": [500.0, 501.0, 502.0],
                "intensity_au": [0.5, 0.6, 0.7],
            },
            metadata={"integration_time_s": 1.5},
        )
        mid = store.log_measurement(eid, measurement)
        row = store._conn.execute(
            "SELECT wavelengths, intensities, integration_time_s "
            "FROM uvvis_measurements WHERE id = ?",
            (mid,),
        ).fetchone()

        assert json.loads(row[0]) == [500.0, 501.0, 502.0]
        assert json.loads(row[1]) == [0.5, 0.6, 0.7]
        assert row[2] == pytest.approx(1.5)
        store.close()


# ─── ASMI InstrumentMeasurement round-trip ───────────────────────────────────


class TestASMIInstrumentMeasurementLogging:

    def test_asmi_json_round_trip(self):
        store = _make_store()
        cid = store.create_campaign(description="asmi test")
        eid = store.create_experiment(cid, "film_plate", "B1", "[]")

        measurement = InstrumentMeasurement(
            measurement_type=MeasurementType.ASMI_INDENTATION,
            payload={
                "sample_timestamps": [1.0, 1.1, 1.2],
                "z_positions_mm": [0.0, 0.1, 0.2],
                "raw_forces_n": [0.01, 0.02, 0.03],
                "corrected_forces_n": [0.005, 0.015, 0.025],
                "directions": ["down", "down", "up"],
            },
            metadata={
                "baseline_avg": 0.005,
                "baseline_std": 0.001,
                "force_exceeded": False,
                "data_points": 3,
                "step_size_mm": 0.1,
                "z_target_mm": -2.0,
                "force_limit_n": 10.0,
            },
        )
        mid = store.log_measurement(eid, measurement)

        row = store._conn.execute(
            "SELECT sample_timestamps, z_positions, raw_forces, "
            "corrected_forces, directions, step_size_mm, z_target_mm, "
            "force_limit_n FROM asmi_measurements WHERE id = ?",
            (mid,),
        ).fetchone()

        assert json.loads(row[0]) == [1.0, 1.1, 1.2]
        assert json.loads(row[1]) == [0.0, 0.1, 0.2]
        assert json.loads(row[2]) == [0.01, 0.02, 0.03]
        assert json.loads(row[3]) == [0.005, 0.015, 0.025]
        assert json.loads(row[4]) == ["down", "down", "up"]
        assert row[5] == pytest.approx(0.1)
        assert row[6] == pytest.approx(-2.0)
        assert row[7] == pytest.approx(10.0)
        store.close()

    def test_missing_asmi_scalar_metadata_persists_nulls(self):
        store = _make_store()
        cid = store.create_campaign(description="asmi test")
        eid = store.create_experiment(cid, "film_plate", "B1", "[]")

        measurement = InstrumentMeasurement(
            measurement_type=MeasurementType.ASMI_INDENTATION,
            payload={
                "sample_timestamps": [1.0],
                "z_positions_mm": [0.0],
                "raw_forces_n": [0.01],
                "corrected_forces_n": [0.005],
                "directions": ["down"],
            },
            metadata={
                "baseline_avg": 0.005,
                "baseline_std": 0.001,
                "force_exceeded": False,
                "data_points": 1,
            },
        )
        mid = store.log_measurement(eid, measurement)

        row = store._conn.execute(
            "SELECT step_size_mm, z_target_mm, force_limit_n "
            "FROM asmi_measurements WHERE id = ?",
            (mid,),
        ).fetchone()
        assert row == (None, None, None)
        store.close()


class TestLabwareTracking:

    def test_register_labware_mid_loop_failure_rolls_back(self):
        class BrokenWells(dict):
            def __iter__(self):
                yield "A1"
                raise RuntimeError("boom")

        plate = WellPlate(
            name="plate",
            model_name="test",
            rows=1,
            columns=2,
            wells={
                "A1": Coordinate3D(x=0.0, y=0.0, z=0.0),
                "A2": Coordinate3D(x=1.0, y=0.0, z=0.0),
            },
            capacity_ul=200.0,
            working_volume_ul=150.0,
        )
        plate.wells = BrokenWells(plate.wells)
        store = _make_store()
        cid = store.create_campaign(description="labware")

        with pytest.raises(RuntimeError, match="boom"):
            store.register_labware(cid, "plate", plate)

        count = store._conn.execute("SELECT COUNT(*) FROM labware").fetchone()[0]
        assert count == 0
        store.close()

    def test_record_transfer_decrements_source_and_increments_destination(self):
        store = _make_store()
        cid = store.create_campaign(description="transfer")
        vial = Vial(
            name="source",
            model_name="standard",
            height=10.0,
            diameter=5.0,
            location=Coordinate3D(x=0.0, y=0.0, z=0.0),
            capacity_ul=1000.0,
            working_volume_ul=800.0,
        )
        plate = WellPlate(
            name="plate",
            model_name="test",
            rows=1,
            columns=1,
            wells={"A1": Coordinate3D(x=1.0, y=0.0, z=0.0)},
            capacity_ul=200.0,
            working_volume_ul=150.0,
        )
        store.register_labware(cid, "vial_1", vial)
        store.register_labware(cid, "plate_1", plate)
        store._conn.execute(
            "UPDATE labware SET current_volume_ul = 100.0 "
            "WHERE labware_key = 'vial_1'"
        )
        store._conn.commit()

        store.record_transfer(cid, "vial_1", None, "plate_1", "A1", 30.0)

        rows = dict(store._conn.execute(
            "SELECT labware_key || COALESCE('.' || well_id, ''), current_volume_ul "
            "FROM labware"
        ).fetchall())
        assert rows["vial_1"] == pytest.approx(70.0)
        assert rows["plate_1.A1"] == pytest.approx(30.0)
        store.close()

    def test_record_transfer_overfill_warns(self, caplog):
        store = _make_store()
        cid = store.create_campaign(description="transfer")
        vial = Vial(
            name="source",
            model_name="standard",
            height=10.0,
            diameter=5.0,
            location=Coordinate3D(x=0.0, y=0.0, z=0.0),
            capacity_ul=1000.0,
            working_volume_ul=800.0,
        )
        plate = WellPlate(
            name="plate",
            model_name="test",
            rows=1,
            columns=1,
            wells={"A1": Coordinate3D(x=1.0, y=0.0, z=0.0)},
            capacity_ul=200.0,
            working_volume_ul=10.0,
        )
        store.register_labware(cid, "vial_1", vial)
        store.register_labware(cid, "plate_1", plate)

        with caplog.at_level(logging.WARNING, logger="cubos.data.data_store"):
            store.record_transfer(cid, "vial_1", None, "plate_1", "A1", 20.0)

        assert "exceeds working volume" in caplog.text
        store.close()


# ─── Context manager ─────────────────────────────────────────────────────────


class TestContextManager:

    def test_context_manager(self):
        with DataStore(db_path=":memory:") as store:
            cid = store.create_campaign(description="ctx test")
            assert cid > 0
