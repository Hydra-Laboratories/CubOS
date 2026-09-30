"""Tests for campaign data browsing and CSV export routes."""

from __future__ import annotations

import csv
import json
import io
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

from cubos.data.data_store import DataStore
from cubos.protocol_engine.measurements import InstrumentMeasurement, MeasurementType

from tests.api_client import api_request
from cubos_api.app import create_app
from cubos_api.config import get_settings


def _seed_asmi_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE campaigns (
            id INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            status TEXT NOT NULL DEFAULT 'running'
        );
        CREATE TABLE experiments (
            id INTEGER PRIMARY KEY,
            campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
            labware_name TEXT NOT NULL,
            well_id TEXT,
            contents TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE asmi_measurements (
            id INTEGER PRIMARY KEY,
            experiment_id INTEGER NOT NULL REFERENCES experiments(id),
            sample_timestamps TEXT,
            z_positions TEXT NOT NULL,
            raw_forces TEXT NOT NULL,
            corrected_forces TEXT NOT NULL,
            directions TEXT,
            baseline_avg REAL NOT NULL,
            baseline_std REAL NOT NULL,
            force_exceeded INTEGER NOT NULL DEFAULT 0,
            data_points INTEGER NOT NULL,
            step_size_mm REAL,
            z_target_mm REAL,
            force_limit_n REAL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.execute(
        "INSERT INTO campaigns (id, description, created_at, status) VALUES (?, ?, ?, ?)",
        (1, "ASMI sample campaign", "2025-10-30 12:20:00", "complete"),
    )
    conn.execute(
        "INSERT INTO experiments "
        "(id, campaign_id, labware_name, well_id, contents, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (7, 1, "asmi_96_well_deck_origin", "E5", "[]", "2025-10-30 12:21:07"),
    )
    conn.execute(
        "INSERT INTO experiments "
        "(id, campaign_id, labware_name, well_id, contents, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (8, 1, "asmi_96_well_deck_origin", "E6", "[]", "2025-10-30 12:22:07"),
    )
    conn.execute(
        "INSERT INTO asmi_measurements "
        "(id, experiment_id, sample_timestamps, z_positions, raw_forces, "
        "corrected_forces, directions, baseline_avg, baseline_std, "
        "force_exceeded, data_points, step_size_mm, z_target_mm, force_limit_n, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            11,
            7,
            json.dumps([1761841220.199, 1761841220.327]),
            json.dumps([-74.01, -74.02]),
            json.dumps([0.463, 0.457]),
            json.dumps([0.004, -0.002]),
            json.dumps(["down", "down"]),
            0.459,
            0.003,
            1,
            2,
            0.01,
            -80.0,
            10.0,
            "2025-10-30 12:21:07",
        ),
    )
    conn.execute(
        "INSERT INTO asmi_measurements "
        "(id, experiment_id, sample_timestamps, z_positions, raw_forces, "
        "corrected_forces, directions, baseline_avg, baseline_std, "
        "force_exceeded, data_points, step_size_mm, z_target_mm, force_limit_n, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            12,
            8,
            json.dumps([1761841280.199]),
            json.dumps([-74.03]),
            json.dumps([0.461]),
            json.dumps([0.002]),
            json.dumps(["down"]),
            0.459,
            0.003,
            0,
            1,
            0.01,
            -80.0,
            10.0,
            "2025-10-30 12:22:07",
        ),
    )
    conn.commit()
    conn.close()


def _seed_all_measurement_tables(path: Path) -> int:
    store = DataStore(db_path=path)
    campaign_id = store.create_campaign(
        description="All instruments",
        deck_config="deck.yaml",
        gantry_config="gantry.yaml",
        protocol_config="protocol.yaml",
    )
    store._conn.execute(
        "UPDATE campaigns SET created_at = ? WHERE id = ?",
        ("2026-06-11 10:00:00", campaign_id),
    )

    experiments = {
        "uvvis": store.create_experiment(campaign_id, "plate", "A1", "[]"),
        "filmetrics": store.create_experiment(campaign_id, "plate", "B2", "[]"),
        "uv_curing": store.create_experiment(campaign_id, "plate", "C3", "[]"),
        "camera": store.create_experiment(campaign_id, "plate", "D4", "[]"),
        "asmi": store.create_experiment(campaign_id, "plate", "E5", "[]"),
        "potentiostat": store.create_experiment(campaign_id, "plate", "F6", "[]"),
    }

    uvvis_id = store.log_measurement(
        experiments["uvvis"],
        InstrumentMeasurement(
            measurement_type=MeasurementType.UVVIS_SPECTRUM,
            payload={
                "wavelength_nm": [400.0, 500.0],
                "intensity_au": [0.1, 0.2],
            },
            metadata={"integration_time_s": 0.24},
        ),
    )
    filmetrics_id = store.log_measurement(
        experiments["filmetrics"],
        InstrumentMeasurement(
            measurement_type=MeasurementType.FILMETRICS_THICKNESS,
            payload={"thickness_nm": 151.2, "goodness_of_fit": 0.96},
        ),
    )
    uv_curing_id = store.log_measurement(
        experiments["uv_curing"],
        InstrumentMeasurement(
            measurement_type=MeasurementType.UV_CURING_EXPOSURE,
            payload={
                "intensity_percent": 55.0,
                "exposure_time_s": 1.25,
                "cure_timestamp_s": 123.4,
            },
        ),
    )
    camera_id = store.log_measurement(experiments["camera"], "/images/D4.png")
    asmi_id = store.log_measurement(
        experiments["asmi"],
        InstrumentMeasurement(
            measurement_type=MeasurementType.ASMI_INDENTATION,
            payload={
                "sample_timestamps": [1.0, 1.1],
                "z_positions_mm": [-74.0, -74.1],
                "raw_forces_n": [0.5, 0.6],
                "corrected_forces_n": [0.1, 0.2],
                "directions": ["down", "up"],
            },
            metadata={
                "baseline_avg": 0.4,
                "baseline_std": 0.01,
                "force_exceeded": False,
                "data_points": 2,
                "step_size_mm": 0.01,
                "z_target_mm": -80.0,
                "force_limit_n": 10.0,
            },
        ),
    )
    potentiostat_id = store.log_measurement(
        experiments["potentiostat"],
        InstrumentMeasurement(
            measurement_type=MeasurementType.POTENTIOSTAT_CA,
            payload={
                "time_s": [0.0, 1.0],
                "voltage_v": [0.2, 0.25],
                "current_a": [0.001, 0.002],
            },
            metadata={
                "technique": "ca",
                "sample_period_s": 1.0,
                "duration_s": 1.0,
                "vendor": "mock",
                "device_id": "pstat-1",
                "channel": 0,
                "started_at": "2026-06-11T10:06:00",
                "stopped_at": "2026-06-11T10:06:01",
                "aborted": False,
                "stop_reason": "complete",
            },
        ),
    )

    timestamp_updates = [
        ("uvvis_measurements", uvvis_id, "2026-06-11 10:01:00"),
        ("filmetrics_measurements", filmetrics_id, "2026-06-11 10:02:00"),
        ("uv_curing_measurements", uv_curing_id, "2026-06-11 10:03:00"),
        ("camera_measurements", camera_id, "2026-06-11 10:04:00"),
        ("asmi_measurements", asmi_id, "2026-06-11 10:05:00"),
        ("potentiostat_measurements", potentiostat_id, "2026-06-11 10:06:00"),
    ]
    for table_name, measurement_id, timestamp in timestamp_updates:
        store._conn.execute(
            f"UPDATE {table_name} SET timestamp = ? WHERE id = ?",
            (timestamp, measurement_id),
        )
    store._conn.commit()
    store.close()
    return campaign_id


def _seed_uv_curing_measurement_only(path: Path) -> int:
    store = DataStore(db_path=path)
    campaign_id = store.create_campaign(
        description="UV curing only",
        deck_config="deck.yaml",
        gantry_config="gantry.yaml",
        protocol_config="protocol.yaml",
    )
    experiment_id = store.create_experiment(campaign_id, "plate", "A1", "[]")
    store.log_measurement(
        experiment_id,
        InstrumentMeasurement(
            measurement_type=MeasurementType.UV_CURING_EXPOSURE,
            payload={
                "intensity_percent": 55.0,
                "exposure_time_s": 1.25,
                "cure_timestamp_s": 123.4,
            },
        ),
    )
    store.close()
    return campaign_id


def test_list_campaigns_returns_run_metadata(monkeypatch, tmp_path):
    db_path = tmp_path / "panda_data.db"
    _seed_asmi_database(db_path)
    monkeypatch.setattr(get_settings(), "data_db_path", db_path)

    response = api_request(create_app(), "GET", "/api/v1/data/campaigns")

    assert response.status_code == 200
    assert response.json() == [
        {
            "campaign_id": 1,
            "campaign_description": "ASMI sample campaign",
            "created_at": "2025-10-30 12:20:00",
            "latest_measurement_at": "2025-10-30 12:22:07",
            "experiment_count": 2,
            "well_count": 2,
            "measurement_count": 2,
            "measurement_counts": {
                "uvvis": 0,
                "filmetrics": 0,
                "uv_curing": 0,
                "camera": 0,
                "asmi": 2,
                "potentiostat": 0,
            },
            "asmi_measurement_count": 2,
        }
    ]


def test_list_campaigns_counts_all_cubos_measurement_tables(monkeypatch, tmp_path):
    db_path = tmp_path / "panda_data.db"
    campaign_id = _seed_all_measurement_tables(db_path)
    monkeypatch.setattr(get_settings(), "data_db_path", db_path)

    response = api_request(create_app(), "GET", "/api/v1/data/campaigns")

    assert response.status_code == 200
    assert response.json() == [
        {
            "campaign_id": campaign_id,
            "campaign_description": "All instruments",
            "created_at": "2026-06-11 10:00:00",
            "latest_measurement_at": "2026-06-11 10:06:00",
            "experiment_count": 6,
            "well_count": 6,
            "measurement_count": 6,
            "measurement_counts": {
                "uvvis": 1,
                "filmetrics": 1,
                "uv_curing": 1,
                "camera": 1,
                "asmi": 1,
                "potentiostat": 1,
            },
            "asmi_measurement_count": 1,
        }
    ]


def test_download_data_zip_includes_legacy_asmi_raw_files(monkeypatch, tmp_path):
    db_path = tmp_path / "panda_data.db"
    _seed_asmi_database(db_path)
    monkeypatch.setattr(get_settings(), "data_db_path", db_path)

    response = api_request(create_app(), "GET", "/api/v1/data/campaigns/1/data.zip")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert response.headers["content-disposition"] == (
        'attachment; filename="campaign_1_data.zip"'
    )
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert [name for name in archive.namelist() if name.startswith("asmi/raw/")] == [
            "asmi/raw/well_E5_20251030_122107.csv",
            "asmi/raw/well_E6_20251030_122207.csv",
            "asmi/raw/metadata.csv",
        ]
        assert archive.read("asmi/raw/metadata.csv").decode().splitlines() == [
            "File,Measurement_ID,Test_Time,Well,Target_Z(mm),Step_Size(mm),"
            "Force_Limit(N),Baseline_Force(N),Baseline_Std(N),Force_Exceeded,Data_Points",
            "well_E5_20251030_122107.csv,11,2025-10-30T12:21:07Z,E5,"
            "-80.000,0.010,10.0,0.459,0.003,True,2",
            "well_E6_20251030_122207.csv,12,2025-10-30T12:22:07Z,E6,"
            "-80.000,0.010,10.0,0.459,0.003,False,1",
        ]
        assert archive.read("asmi/raw/well_E5_20251030_122107.csv").decode().splitlines() == [
            "Timestamp(s),Z_Position(mm),Raw_Force(N),Corrected_Force(N),Direction",
            "1761841220.199,-74.010,0.463,0.004,down",
            "1761841220.327,-74.020,0.457,-0.002,down",
        ]
        assert archive.read("asmi/raw/well_E6_20251030_122207.csv").decode().splitlines() == [
            "Timestamp(s),Z_Position(mm),Raw_Force(N),Corrected_Force(N),Direction",
            "1761841280.199,-74.030,0.461,0.002,down",
        ]


def test_download_data_zip_includes_all_cubos_measurement_tables(monkeypatch, tmp_path):
    db_path = tmp_path / "panda_data.db"
    campaign_id = _seed_all_measurement_tables(db_path)
    monkeypatch.setattr(get_settings(), "data_db_path", db_path)

    response = api_request(
        create_app(), "GET", f"/api/v1/data/campaigns/{campaign_id}/data.zip",
    )

    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        f'attachment; filename="campaign_{campaign_id}_data.zip"'
    )
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {
            "README.txt",
            "metadata.json",
            "samples.csv",
            "uvvis/measurements.csv",
            "uvvis/curves.csv",
            "uvvis/spectra_wide.csv",
            "filmetrics/measurements.csv",
            "uv_curing/measurements.csv",
            "camera/measurements.csv",
            "asmi/measurements.csv",
            "asmi/curves.csv",
            "asmi/raw/metadata.csv",
            "potentiostat/measurements.csv",
            "potentiostat/curves.csv",
        } <= names
        assert not any(name.startswith("measurements/") for name in names)

        uvvis_curves = list(csv.DictReader(io.StringIO(archive.read("uvvis/curves.csv").decode())))
        assert [(row["well_id"], row["wavelength_nm"], row["intensity"]) for row in uvvis_curves] == [
            ("A1", "400.0", "0.1"),
            ("A1", "500.0", "0.2"),
        ]
        potentiostat = list(csv.DictReader(io.StringIO(archive.read("potentiostat/curves.csv").decode())))
        assert [(row["technique"], row["voltage_v"], row["current_a"]) for row in potentiostat] == [
            ("ca", "0.2", "0.001"),
            ("ca", "0.25", "0.002"),
        ]
        camera = list(csv.DictReader(io.StringIO(archive.read("camera/measurements.csv").decode())))
        assert camera[0]["source_path"] == "/images/D4.png"
        samples = list(csv.DictReader(io.StringIO(archive.read("samples.csv").decode())))
        assert len(samples) == 6


def test_download_data_zip_omits_empty_instruments(monkeypatch, tmp_path):
    db_path = tmp_path / "panda_data.db"
    campaign_id = _seed_uv_curing_measurement_only(db_path)
    monkeypatch.setattr(get_settings(), "data_db_path", db_path)

    response = api_request(
        create_app(), "GET", f"/api/v1/data/campaigns/{campaign_id}/data.zip",
    )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {
            "README.txt",
            "metadata.json",
            "samples.csv",
            "uv_curing/measurements.csv",
        }
        rows = list(csv.DictReader(io.StringIO(archive.read("uv_curing/measurements.csv").decode())))
        assert (rows[0]["intensity_percent"], rows[0]["exposure_time_s"]) == ("55.0", "1.25")


def test_download_data_zip_reads_cubos_datastore_asmi_schema(monkeypatch, tmp_path):
    db_path = tmp_path / "panda_data.db"
    store = DataStore(db_path=db_path)
    campaign_id = store.create_campaign(
        description="CubOS seeded ASMI",
        deck_config="deck.yaml",
        gantry_config="gantry.yaml",
        protocol_config="protocol.yaml",
    )
    experiment_id = store.create_experiment(campaign_id, "plate", "A1", "[]")
    store.log_measurement(
        experiment_id,
        InstrumentMeasurement(
            measurement_type=MeasurementType.ASMI_INDENTATION,
            payload={
                "sample_timestamps": [1.0],
                "z_positions_mm": [-74.0],
                "raw_forces_n": [0.5],
                "corrected_forces_n": [0.1],
                "directions": ["down"],
            },
            metadata={
                "baseline_avg": 0.4,
                "baseline_std": 0.01,
                "force_exceeded": False,
                "data_points": 1,
                "step_size_mm": 0.01,
                "z_target_mm": -80.0,
                "force_limit_n": 10.0,
            },
        ),
    )
    store.close()
    monkeypatch.setattr(get_settings(), "data_db_path", db_path)

    response = api_request(
        create_app(), "GET", f"/api/v1/data/campaigns/{campaign_id}/data.zip",
    )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read("asmi/raw/metadata.csv").decode().splitlines()[1].endswith(
            "A1,-80.000,0.010,10.0,0.400,0.010,False,1"
        )


def test_download_data_zip_rejects_missing_sample_arrays(monkeypatch, tmp_path):
    db_path = tmp_path / "panda_data.db"
    _seed_asmi_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "UPDATE asmi_measurements SET sample_timestamps = NULL WHERE id = 11"
        )
        conn.commit()
    monkeypatch.setattr(get_settings(), "data_db_path", db_path)

    response = api_request(create_app(), "GET", "/api/v1/data/campaigns/1/data.zip")

    assert response.status_code == 400
    assert response.json()["detail"] == "ASMI field 'sample_timestamps' is missing"


def test_download_data_zip_returns_404_for_missing_database(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_db_path", tmp_path / "missing.db")

    response = api_request(create_app(), "GET", "/api/v1/data/campaigns/1/data.zip")

    assert response.status_code == 404
    assert "Data database not found" in response.json()["detail"]


def test_data_routes_reject_corrupt_sqlite_database(monkeypatch, tmp_path):
    db_path = tmp_path / "panda_data.db"
    db_path.write_text("not a sqlite database", encoding="utf-8")
    monkeypatch.setattr(get_settings(), "data_db_path", db_path)
    app = create_app()

    for path in (
        "/api/v1/data/campaigns",
        "/api/v1/data/campaigns/1/data.zip",
    ):
        response = api_request(app, "GET", path)
        assert response.status_code == 400
        assert "Data database is unreadable" in response.json()["detail"]


def test_list_campaigns_rejects_corrupt_schema_database(monkeypatch, tmp_path):
    db_path = tmp_path / "panda_data.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE campaigns (id INTEGER PRIMARY KEY)")
        conn.commit()
    monkeypatch.setattr(get_settings(), "data_db_path", db_path)

    response = api_request(create_app(), "GET", "/api/v1/data/campaigns")

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Data database is missing table(s): experiments"
    )


def test_download_data_zip_returns_404_for_unknown_campaign(monkeypatch, tmp_path):
    db_path = tmp_path / "panda_data.db"
    _seed_asmi_database(db_path)
    monkeypatch.setattr(get_settings(), "data_db_path", db_path)

    response = api_request(create_app(), "GET", "/api/v1/data/campaigns/999/data.zip")

    assert response.status_code == 404
    assert response.json()["detail"] == "Campaign 999 not found"


def test_download_data_zip_returns_404_for_campaign_without_measurements(monkeypatch, tmp_path):
    db_path = tmp_path / "panda_data.db"
    store = DataStore(db_path=db_path)
    campaign_id = store.create_campaign(description="empty")
    store.close()
    monkeypatch.setattr(get_settings(), "data_db_path", db_path)

    response = api_request(
        create_app(), "GET", f"/api/v1/data/campaigns/{campaign_id}/data.zip",
    )

    assert response.status_code == 404
    assert response.json()["detail"] == (
        f"No instrument measurements found for campaign {campaign_id}"
    )


def test_download_data_zip_rejects_missing_tables(monkeypatch, tmp_path):
    db_path = tmp_path / "panda_data.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE campaigns (id INTEGER PRIMARY KEY)")
        conn.commit()
    monkeypatch.setattr(get_settings(), "data_db_path", db_path)

    response = api_request(create_app(), "GET", "/api/v1/data/campaigns/1/data.zip")

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Data database is missing table(s): experiments"
    )


def test_download_data_zip_rejects_non_array_json(monkeypatch, tmp_path):
    db_path = tmp_path / "panda_data.db"
    _seed_asmi_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("UPDATE asmi_measurements SET raw_forces = ? WHERE id = 11", (json.dumps({}),))
        conn.commit()
    monkeypatch.setattr(get_settings(), "data_db_path", db_path)

    response = api_request(create_app(), "GET", "/api/v1/data/campaigns/1/data.zip")

    assert response.status_code == 400
    assert response.json()["detail"] == "ASMI field 'raw_forces' must be a JSON array"


def test_download_data_zip_rejects_mismatched_array_lengths(monkeypatch, tmp_path):
    db_path = tmp_path / "panda_data.db"
    _seed_asmi_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "UPDATE asmi_measurements SET corrected_forces = ? WHERE id = 11",
            (json.dumps([0.004]),),
        )
        conn.commit()
    monkeypatch.setattr(get_settings(), "data_db_path", db_path)

    response = api_request(create_app(), "GET", "/api/v1/data/campaigns/1/data.zip")

    assert response.status_code == 400
    assert response.json()["detail"].startswith(
        "ASMI measurement arrays must have equal lengths:"
    )


def test_experiments_endpoint_aliases_campaigns(monkeypatch, tmp_path):
    db_path = tmp_path / "panda_data.db"
    _seed_asmi_database(db_path)
    monkeypatch.setattr(get_settings(), "data_db_path", db_path)

    campaigns_response = api_request(create_app(), "GET", "/api/v1/data/campaigns")
    experiments_response = api_request(create_app(), "GET", "/api/v1/data/experiments")

    assert experiments_response.status_code == 200
    assert experiments_response.json() == campaigns_response.json()


def test_missing_database_returns_empty_campaign_list(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_db_path", tmp_path / "missing.db")

    response = api_request(create_app(), "GET", "/api/v1/data/campaigns")

    assert response.status_code == 200
    assert response.json() == []
