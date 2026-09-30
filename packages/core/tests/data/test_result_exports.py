"""Tests for CubOS campaign summaries, CSV exports, and the Download Data ZIP."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

import pytest

from cubos.data.data_store import DataStore
from cubos.data.download import export_campaign_data_zip
from cubos.data.exports import (
    MeasurementDataError,
    MeasurementExportNotFoundError,
    export_campaign_results_csvs,
    list_campaign_summaries,
    _json_array,
    _export_timestamp
)

from cubos.instruments.filmetrics.models import MeasurementResult
from cubos.instruments.uv_curing.models import CureResult
from cubos.protocol_engine.measurements import InstrumentMeasurement, MeasurementType


def _seed_store(path):
    store = DataStore(db_path=path)
    campaign_id = store.create_campaign(
        description="Export test",
        deck_config="deck.yaml",
        gantry_config="gantry.yaml",
        protocol_config="protocol.yaml",
    )
    store._conn.execute(
        "UPDATE campaigns SET created_at = ? WHERE id = ?",
        ("2026-06-11 10:00:00", campaign_id),
    )
    uvvis_experiment = store.create_experiment(campaign_id, "plate", "A1", "[]")
    asmi_experiment = store.create_experiment(campaign_id, "plate", "B2", "[]")
    uvvis_id = store.log_measurement(
        uvvis_experiment,
        InstrumentMeasurement(
            measurement_type=MeasurementType.UVVIS_SPECTRUM,
            payload={
                "wavelength_nm": [400.0, 500.0],
                "intensity_au": [0.1, 0.2],
            },
            metadata={"integration_time_s": 0.24},
        ),
    )
    asmi_id = store.log_measurement(
        asmi_experiment,
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
    store._conn.execute(
        "UPDATE uvvis_measurements SET timestamp = ? WHERE id = ?",
        ("2026-06-11 10:01:00", uvvis_id),
    )
    store._conn.execute(
        "UPDATE asmi_measurements SET timestamp = ? WHERE id = ?",
        ("2026-06-11 10:02:00", asmi_id),
    )
    store._conn.commit()
    store.close()
    return campaign_id


def _seed_all_instruments_store(path):
    store = DataStore(db_path=path)
    campaign_id = store.create_campaign(
        description="Filesystem export test",
        deck_config="deck.yaml",
        gantry_config="gantry.yaml",
        protocol_config="protocol.yaml",
    )
    store._conn.execute(
        "UPDATE campaigns SET created_at = ? WHERE id = ?",
        ("2026-06-12 11:00:00", campaign_id),
    )
    uvvis_experiment = store.create_experiment(campaign_id, "plate", "A1", "[]")
    asmi_experiment = store.create_experiment(campaign_id, "plate", "B2", "[]")
    filmetrics_experiment = store.create_experiment(campaign_id, "plate", "C3", "[]")
    uv_curing_experiment = store.create_experiment(campaign_id, "plate", "D4", "[]")
    potentiostat_experiment = store.create_experiment(campaign_id, "plate", "E5", "[]")

    store.log_measurement(
        uvvis_experiment,
        InstrumentMeasurement(
            measurement_type=MeasurementType.UVVIS_SPECTRUM,
            payload={
                "wavelength_nm": [400.0, 500.0],
                "intensity_au": [0.1, 0.2],
            },
            metadata={"integration_time_s": 0.24},
        ),
    )
    store.log_measurement(
        asmi_experiment,
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
    store.log_measurement(
        filmetrics_experiment,
        MeasurementResult(thickness_nm=151.2, goodness_of_fit=0.96),
    )
    store.log_measurement(
        uv_curing_experiment,
        CureResult(intensity_percent=60.0, exposure_time_s=1.5, timestamp=123.4),
    )
    store.log_measurement(
        potentiostat_experiment,
        InstrumentMeasurement(
            measurement_type=MeasurementType.POTENTIOSTAT_CA,
            payload={
                "time_s": [0.0, 0.1],
                "voltage_v": [0.5, 0.5],
                "current_a": [1e-6, 2e-6],
            },
            metadata={
                "technique": "ca",
                "vendor": "admiral",
                "sample_period_s": 0.1,
                "duration_s": 0.2,
                "step_potential_v": 0.5,
                "device_id": "dev-1",
            },
        ),
    )
    store.close()
    return campaign_id


def test_list_campaign_summaries_counts_measurements(tmp_path):
    db_path = tmp_path / "panda_data.db"
    campaign_id = _seed_store(db_path)

    summaries = list_campaign_summaries(db_path)

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.campaign_id == campaign_id
    assert summary.latest_measurement_at == "2026-06-11 10:02:00"
    assert summary.experiment_count == 2
    assert summary.measurement_count == 2
    assert summary.measurement_counts["uvvis"] == 1
    assert summary.measurement_counts["asmi"] == 1


def test_list_campaign_summaries_closes_database_connection(monkeypatch, tmp_path):
    import cubos.data.exports as exports

    db_path = tmp_path / "panda_data.db"
    _seed_store(db_path)
    real_connect = exports._connect
    closed = []

    class ClosingProbe:
        def __init__(self, conn):
            self._conn = conn

        def close(self):
            closed.append(True)
            self._conn.close()

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(
        exports,
        "_connect",
        lambda path: ClosingProbe(real_connect(path)),
    )

    assert list_campaign_summaries(db_path)
    assert closed == [True]


def _zip_csv(archive, name):
    return list(csv.DictReader(io.StringIO(archive.read(name).decode())))


def test_download_data_zip_layout(tmp_path):
    db_path = tmp_path / "panda_data.db"
    campaign_id = _seed_all_instruments_store(db_path)

    with zipfile.ZipFile(io.BytesIO(export_campaign_data_zip(db_path, campaign_id))) as archive:
        assert set(archive.namelist()) == {
            "README.txt",
            "metadata.json",
            "samples.csv",
            "uvvis/measurements.csv",
            "uvvis/curves.csv",
            "uvvis/spectra_wide.csv",
            "asmi/measurements.csv",
            "asmi/curves.csv",
            "asmi/raw/metadata.csv",
            f"asmi/raw/{_zip_csv(archive, 'asmi/raw/metadata.csv')[0]['File']}",
            "filmetrics/measurements.csv",
            "uv_curing/measurements.csv",
            "potentiostat/measurements.csv",
            "potentiostat/curves.csv",
        }
        samples = _zip_csv(archive, "samples.csv")
        assert [row["well_id"] for row in samples] == ["A1", "B2", "C3", "D4", "E5"]
        assert (samples[0]["uvvis_measurements"], samples[0]["asmi_measurements"]) == ("1", "0")
        assert (samples[1]["uvvis_measurements"], samples[1]["asmi_measurements"]) == ("0", "1")

        wide = _zip_csv(archive, "uvvis/spectra_wide.csv")
        assert list(wide[0])[0] == "wavelength_nm"
        assert list(wide[0])[1].startswith("A1 ")
        assert [row["wavelength_nm"] for row in wide] == ["400.0", "500.0"]

        uvvis = _zip_csv(archive, "uvvis/measurements.csv")[0]
        assert (uvvis["integration_time_s"], uvvis["points"]) == ("0.24", "2")

        potentiostat = _zip_csv(archive, "potentiostat/measurements.csv")[0]
        assert (potentiostat["technique"], potentiostat["step_potential_v"]) == ("ca", "0.5")
        curves = _zip_csv(archive, "potentiostat/curves.csv")
        assert [(row["time_s"], row["current_a"]) for row in curves] == [("0.0", "1e-06"), ("0.1", "2e-06")]

        asmi = _zip_csv(archive, "asmi/curves.csv")
        assert list(asmi[0]) == [
            "measurement_id", "well_id", "sample_index", "timestamp_s",
            "z_position_mm", "raw_force_n", "corrected_force_n", "direction",
        ]
        for name in archive.namelist():
            if name.endswith(".csv"):
                header = archive.read(name).decode().splitlines()[0].lower()
                assert not any(word in header for word in ("hertz", "spring", "contact", "peak", "charge"))

        cure = _zip_csv(archive, "uv_curing/measurements.csv")[0]
        assert cure["exposure_time_s"] == "1.5"

        readme = archive.read("README.txt").decode()
        for heading in ("UV-VIS", "ASMI", "POTENTIOSTAT", "UV CURING", "FILM THICKNESS"):
            assert heading in readme
        metadata = json.loads(archive.read("metadata.json"))
        assert metadata["format"] == "cubos.download-data.v1"
        assert metadata["measurement_counts"]["asmi"] == 1


def test_download_data_keeps_legacy_asmi_raw_files(tmp_path):
    db_path = tmp_path / "panda_data.db"
    campaign_id = _seed_store(db_path)

    with zipfile.ZipFile(io.BytesIO(export_campaign_data_zip(db_path, campaign_id))) as archive:
        assert archive.read("asmi/raw/metadata.csv").decode().splitlines()[0] == (
            "File,Measurement_ID,Test_Time,Well,Target_Z(mm),Step_Size(mm),"
            "Force_Limit(N),Baseline_Force(N),Baseline_Std(N),"
            "Force_Exceeded,Data_Points"
        )
        assert archive.read("asmi/raw/well_B2_20260611_100200.csv").decode().splitlines() == [
            "Timestamp(s),Z_Position(mm),Raw_Force(N),Corrected_Force(N),Direction",
            "1.000,-74.000,0.500,0.100,down",
            "1.100,-74.100,0.600,0.200,up",
        ]
        uvvis, asmi = _zip_csv(archive, "uvvis/measurements.csv")[0], _zip_csv(archive, "asmi/measurements.csv")[0]
        assert uvvis["elapsed_min"] == "0.0"
        assert asmi["elapsed_min"] == "1.0"
        assert asmi["measured_at"] == "2026-06-11T10:02:00Z"


def test_download_data_rejects_campaign_without_measurements(tmp_path):
    db_path = tmp_path / "panda_data.db"
    store = DataStore(db_path=db_path)
    campaign_id = store.create_campaign(
        description="Empty", deck_config="d", gantry_config="g", protocol_config="p",
    )
    store.close()

    with pytest.raises(MeasurementExportNotFoundError):
        export_campaign_data_zip(db_path, campaign_id)


def test_json_array_reports_malformed_asmi_rows():
    with pytest.raises(MeasurementDataError, match="must be a JSON array"):
        _json_array("{}", "raw_forces")


def test_export_campaign_results_csvs_writes_sane_flat_files(tmp_path):
    db_path = tmp_path / "panda_data.db"
    campaign_id = _seed_all_instruments_store(db_path)

    written = export_campaign_results_csvs(
        db_path,
        campaign_id,
        output_dir=tmp_path / "results",
    )

    run_dir = tmp_path / "results" / "campaign_1_20260612_110000"
    assert {path.name for path in written} == {
        "campaign.csv",
        "experiments.csv",
        "manifest.csv",
        "uvvis.csv",
        "asmi.csv",
        "filmetrics.csv",
        "uv_curing.csv",
        "potentiostat.csv",
    }
    assert run_dir.is_dir()

    uvvis_rows = list(csv.DictReader((run_dir / "uvvis.csv").open()))
    assert uvvis_rows == [
        {
            "measurement_id": "1",
            "experiment_id": "1",
            "labware_key": "plate",
            "labware_name": "plate",
            "well_id": "A1",
            "measurement_timestamp": uvvis_rows[0]["measurement_timestamp"],
            "experiment_created_at": uvvis_rows[0]["experiment_created_at"],
            "sample_index": "0",
            "wavelength_nm": "400.0",
            "intensity_au": "0.1",
            "integration_time_s": "0.24",
        },
        {
            "measurement_id": "1",
            "experiment_id": "1",
            "labware_key": "plate",
            "labware_name": "plate",
            "well_id": "A1",
            "measurement_timestamp": uvvis_rows[1]["measurement_timestamp"],
            "experiment_created_at": uvvis_rows[1]["experiment_created_at"],
            "sample_index": "1",
            "wavelength_nm": "500.0",
            "intensity_au": "0.2",
            "integration_time_s": "0.24",
        },
    ]

    asmi_rows = list(csv.DictReader((run_dir / "asmi.csv").open()))
    assert [row["z_position_mm"] for row in asmi_rows] == ["-74.0", "-74.1"]
    assert [row["corrected_force_n"] for row in asmi_rows] == ["0.1", "0.2"]

    potentiostat_rows = list(csv.DictReader((run_dir / "potentiostat.csv").open()))
    assert [row["time_s"] for row in potentiostat_rows] == ["0.0", "0.1"]
    assert [row["current_a"] for row in potentiostat_rows] == ["1e-06", "2e-06"]

    filmetrics_rows = list(csv.DictReader((run_dir / "filmetrics.csv").open()))
    assert filmetrics_rows[0]["thickness_nm"] == "151.2"

    manifest = (run_dir / "manifest.csv").read_text()
    assert "uvvis,2,uvvis.csv" in manifest
    assert "asmi,2,asmi.csv" in manifest
    assert "potentiostat,2,potentiostat.csv" in manifest


def test_export_campaign_results_csvs_writes_via_atomic_replace(monkeypatch, tmp_path):
    import cubos.data.exports as exports

    db_path = tmp_path / "panda_data.db"
    campaign_id = _seed_store(db_path)
    replaced = []
    real_replace = exports.os.replace

    def tracking_replace(src, dst):
        replaced.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(exports.os, "replace", tracking_replace)

    written = export_campaign_results_csvs(
        db_path,
        campaign_id,
        output_dir=tmp_path / "results",
    )

    assert written
    assert len(replaced) == len(written)
    assert all(str(src).endswith(".tmp") for src, _dst in replaced)


def test_results_directory_is_gitignored():
    gitignore = Path(__file__).resolve().parents[4] / ".gitignore"
    assert "data/results/" in gitignore.read_text(encoding="utf-8")


def test_export_timestamp_converts_space_to_t_and_adds_z():
    assert (
        _export_timestamp("2026-08-20 18:10:20")
        == "2026-08-20T18:10:20Z"
    )


def test_export_timestamp_preserves_existing_z():
    assert (
        _export_timestamp("2026-08-20T18:10:20Z")
        == "2026-08-20T18:10:20Z"
    )


def test_export_timestamp_none_returns_empty_string():
    assert _export_timestamp(None) == ""

def test_download_data_bundles_camera_images(tmp_path):
    db_path = tmp_path / "panda_data.db"
    store = DataStore(db_path=db_path)
    campaign_id = store.create_campaign(
        description="Camera export test",
        deck_config="deck.yaml",
        gantry_config="gantry.yaml",
        protocol_config="protocol.yaml",
    )
    present_image = tmp_path / "a1_20260830-120000.tiff"
    present_image.write_bytes(b"fake-tiff-bytes")
    store.log_experiment_measurement(
        campaign_id=campaign_id, labware_key="plate", labware_name="plate",
        well_id="A1", contents_json=None, result=str(present_image),
    )
    store.log_experiment_measurement(
        campaign_id=campaign_id, labware_key="plate", labware_name="plate",
        well_id="A2", contents_json=None,
        result=str(tmp_path / "gone_20260830-120000.tiff"),
    )

    with zipfile.ZipFile(io.BytesIO(export_campaign_data_zip(db_path, campaign_id))) as archive:
        present_arcname = f"camera/images/camera_1_{present_image.name}"
        assert archive.read(present_arcname) == b"fake-tiff-bytes"
        included, missing = _zip_csv(archive, "camera/measurements.csv")
        assert (included["image_status"], included["image_file"]) == ("included", present_arcname)
        assert (missing["image_status"], missing["image_file"]) == ("missing on server", "")


def test_download_data_samples_group_repeat_measurements_by_well(tmp_path):
    db_path = tmp_path / "panda_data.db"
    store = DataStore(db_path=db_path)
    campaign_id = store.create_campaign(
        description="Repeat reads", deck_config="d", gantry_config="g", protocol_config="p",
    )
    for well in ("A1", "A2", "A1"):
        store.log_experiment_measurement(
            campaign_id=campaign_id, labware_key="plate", labware_name="plate", well_id=well,
            contents_json='[{"source": "dye_red", "volume_ul": 20}]',
            result=CureResult(intensity_percent=10.0, exposure_time_s=2.0, timestamp=1.0),
        )
    store.close()

    with zipfile.ZipFile(io.BytesIO(export_campaign_data_zip(db_path, campaign_id))) as archive:
        samples = _zip_csv(archive, "samples.csv")
    assert [(row["well_id"], row["uv_curing_measurements"]) for row in samples] == [("A1", "2"), ("A2", "1")]
    assert samples[0]["contents"] == "dye_red 20 uL"
    assert samples[0]["experiment_ids"] == "1;3"

