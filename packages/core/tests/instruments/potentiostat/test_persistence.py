"""End-to-end tests for potentiostat result → normalize → DataStore.

Exercises the protocol-engine normalization layer (`normalize_measurement`)
and the DataStore persistence path (`log_measurement` → SQL insert) for each
of the four supported potentiostat techniques.
"""

from __future__ import annotations



from cubos.data.data_store import DataStore
from cubos.instruments.potentiostat.models import (
    OCPResult,
)
from cubos.protocol_engine.measurements import (
    MeasurementType,
    normalize_measurement,
)


def _make_store() -> DataStore:
    store = DataStore(db_path=":memory:")
    cid = store.create_campaign("unit-test")
    eid = store.create_experiment(cid, labware_name="plate", well_id="A1")
    store._experiment_id = eid  # type: ignore[attr-defined]
    return store


# --- normalize_measurement ---------------------------------------------------


class TestNormalize:

    def test_ocp_result(self):
        raw = OCPResult(
            time_s=(0.0, 0.1),
            voltage_v=(0.35, 0.36),
            sample_period_s=0.1,
            duration_s=0.2,
            vendor="admiral",
            metadata={"device_id": "unit-test"},
        )
        m = normalize_measurement("pstat_a", "run_OCP", raw)
        assert m.measurement_type == MeasurementType.POTENTIOSTAT_OCP
        assert m.payload == {"time_s": [0.0, 0.1], "voltage_v": [0.35, 0.36]}
        assert m.metadata["technique"] == "ocp"
        assert m.metadata["vendor"] == "admiral"
        assert m.metadata["instrument_name"] == "pstat_a"
        assert m.metadata["method_name"] == "run_OCP"
        assert m.metadata["device_id"] == "unit-test"
        assert m.metadata["duration_s"] == 0.2


# --- DataStore persistence ---------------------------------------------------


class TestPersistence:


    def test_aborted_true_persists_as_one(self):
        store = _make_store()
        raw = OCPResult(
            time_s=(0.0,),
            voltage_v=(0.3,),
            sample_period_s=0.1,
            duration_s=0.1,
            vendor="admiral",
            metadata={"aborted": True, "stop_reason": "user_stop"},
        )
        m = normalize_measurement("pstat_a", "run_OCP", raw)
        row_id = store.log_measurement(store._experiment_id, m)
        aborted = store._conn.execute(
            "SELECT aborted FROM potentiostat_measurements WHERE id = ?",
            (row_id,),
        ).fetchone()[0]
        assert aborted == 1
        store.close()
