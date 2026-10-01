"""Tests for the labware SQLite table and DataStore labware tracking methods."""

from __future__ import annotations

import json

import pytest

from cubos.data.data_store import DataStore
from cubos.deck.labware.labware import Coordinate3D
from cubos.deck.labware.vial import Vial
from cubos.deck.labware.well_plate import WellPlate


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _store() -> DataStore:
    return DataStore(db_path=":memory:")


def _make_vial(name: str = "vial_1") -> Vial:
    return Vial(
        name=name,
        model_name="test_vial",
        height=50.0,
        diameter=20.0,
        location=Coordinate3D(x=0.0, y=0.0, z=0.0),
        capacity_ul=5000.0,
        working_volume_ul=4000.0,
    )


def _make_2x2_plate(name: str = "plate_1") -> WellPlate:
    return WellPlate(
        name=name,
        model_name="test_96",
        length=127.71,
        width=85.43,
        height=14.10,
        rows=2,
        columns=2,
        wells={
            "A1": Coordinate3D(x=0.0, y=0.0, z=-5.0),
            "A2": Coordinate3D(x=10.0, y=0.0, z=-5.0),
            "B1": Coordinate3D(x=0.0, y=-8.0, z=-5.0),
            "B2": Coordinate3D(x=10.0, y=-8.0, z=-5.0),
        },
        capacity_ul=200.0,
        working_volume_ul=150.0,
    )


# ─── Table existence ─────────────────────────────────────────────────────────


class TestLabwareTableExists:


    def test_foreign_key_to_campaigns(self):
        store = _store()
        with pytest.raises(Exception):
            store._conn.execute(
                "INSERT INTO labware (campaign_id, labware_key, labware_type, "
                "total_volume_ul, working_volume_ul) "
                "VALUES (9999, 'plate_1', 'well_plate', 200, 150)"
            )
            store._conn.commit()
        store.close()


# ─── register_labware ────────────────────────────────────────────────────────


class TestRegisterLabware:


    def test_register_vial_stores_volumes(self):
        store = _store()
        cid = store.create_campaign(description="test")
        store.register_labware(cid, "vial_1", _make_vial())

        row = store._conn.execute(
            "SELECT labware_type, well_id, total_volume_ul, working_volume_ul, "
            "current_volume_ul, contents "
            "FROM labware WHERE campaign_id = ? AND labware_key = ?",
            (cid, "vial_1"),
        ).fetchone()
        assert row[0] == "vial"
        assert row[1] is None  # no well_id for vials
        assert row[2] == 5000.0
        assert row[3] == 4000.0
        assert row[4] == 0.0
        assert row[5] is None
        store.close()

    def test_register_well_plate_creates_row_per_well(self):
        store = _store()
        cid = store.create_campaign(description="test")
        store.register_labware(cid, "plate_1", _make_2x2_plate())

        rows = store._conn.execute(
            "SELECT well_id FROM labware WHERE campaign_id = ? AND labware_key = ?",
            (cid, "plate_1"),
        ).fetchall()
        well_ids = {r[0] for r in rows}
        assert well_ids == {"A1", "A2", "B1", "B2"}
        store.close()

    def test_register_well_plate_stores_per_well_volumes(self):
        store = _store()
        cid = store.create_campaign(description="test")
        store.register_labware(cid, "plate_1", _make_2x2_plate())

        row = store._conn.execute(
            "SELECT labware_type, total_volume_ul, working_volume_ul, current_volume_ul "
            "FROM labware WHERE campaign_id = ? AND labware_key = ? AND well_id = 'A1'",
            (cid, "plate_1"),
        ).fetchone()
        assert row[0] == "well_plate"
        assert row[1] == 200.0
        assert row[2] == 150.0
        assert row[3] == 0.0
        store.close()

    def test_duplicate_registration_raises(self):
        store = _store()
        cid = store.create_campaign(description="test")
        store.register_labware(cid, "vial_1", _make_vial())
        with pytest.raises(ValueError, match="already registered"):
            store.register_labware(cid, "vial_1", _make_vial())
        store.close()


# ─── record_dispense ─────────────────────────────────────────────────────────


class TestRecordDispense:


    def test_tracks_contents_json(self):
        store = _store()
        cid = store.create_campaign(description="test")
        store.register_labware(cid, "plate_1", _make_2x2_plate())

        store.record_dispense(cid, "plate_1", "A1", "vial_1", 50.0)
        store.record_dispense(cid, "plate_1", "A1", "vial_2", 25.0)

        row = store._conn.execute(
            "SELECT contents FROM labware "
            "WHERE campaign_id = ? AND labware_key = ? AND well_id = ?",
            (cid, "plate_1", "A1"),
        ).fetchone()
        parsed = json.loads(row[0])
        assert len(parsed) == 2
        assert parsed[0] == {"source": "vial_1", "volume_ul": 50.0}
        assert parsed[1] == {"source": "vial_2", "volume_ul": 25.0}
        store.close()


    def test_dispense_unknown_labware_raises(self):
        store = _store()
        cid = store.create_campaign(description="test")

        with pytest.raises(ValueError, match="not registered"):
            store.record_dispense(cid, "unknown", "A1", "vial_1", 50.0)
        store.close()


# ─── get_contents ─────────────────────────────────────────────────────────────


class TestGetContents:


    def test_returns_contents_after_dispense(self):
        store = _store()
        cid = store.create_campaign(description="test")
        store.register_labware(cid, "plate_1", _make_2x2_plate())
        store.record_dispense(cid, "plate_1", "A1", "vial_1", 50.0)

        result = store.get_contents(cid, "plate_1", "A1")
        assert result[0]["source"] == "vial_1"
        store.close()
