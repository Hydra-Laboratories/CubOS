"""Tests for the ``cure`` protocol command."""

from unittest.mock import MagicMock

import pytest

from cubos.data.data_store import DataStore
from cubos.deck.deck import Deck
from cubos.deck.labware.labware import Coordinate3D
from cubos.deck.labware.well_plate import WellPlate
from cubos.instruments.camera.vendors.mount_only import MountOnlyCamera
from cubos.instruments.uv_curing.vendors.excelitas import ExcelitasUVCuring
from cubos.protocol_engine.commands.cure import cure
from cubos.protocol_engine.errors import ProtocolExecutionError
from cubos.protocol_engine.runtime import ProtocolContext


HEIGHT_MM = 14.10


def _uv():
    uv = ExcelitasUVCuring(offline=True, default_intensity=50.0,
                            default_exposure_time=1.0)
    uv.connect()
    return uv


def _plate() -> WellPlate:
    return WellPlate(
        name="test_plate",
        model_name="test_2x1",
        rows=1,
        columns=1,
        wells={"A1": Coordinate3D(x=10.0, y=20.0, z=HEIGHT_MM)},
        capacity_ul=200.0,
        working_volume_ul=150.0,
    )


def _ctx(instr, data_store=None, campaign_id=None):
    board = MagicMock()
    board.controller = object()
    board.instruments = {"uv_curing": instr}
    deck = Deck({"plate_1": _plate()})
    return ProtocolContext(
        gantry=board, deck=deck, data_store=data_store, campaign_id=campaign_id,
    )


class TestCure:
    def test_cures_with_explicit_exposure_and_intensity(self):
        uv = _uv()
        result = cure(
            _ctx(uv), instrument="uv_curing", position="plate_1.A1",
            measurement_height=0.0, exposure_time=2.0, intensity=75.0,
        )
        assert result.exposure_time_s == pytest.approx(2.0)
        assert result.intensity_percent == pytest.approx(75.0)

    def test_intensity_defaults_to_instrument_default_when_omitted(self):
        uv = _uv()
        result = cure(
            _ctx(uv), instrument="uv_curing", position="plate_1.A1",
            measurement_height=0.0, exposure_time=2.0,
        )
        assert result.intensity_percent == pytest.approx(50.0)

    def test_unknown_instrument(self):
        with pytest.raises(ProtocolExecutionError, match="Unknown instrument"):
            cure(
                _ctx(_uv(), None, None), instrument="missing",
                position="plate_1.A1", measurement_height=0.0,
                exposure_time=1.0,
            )

    def test_wrong_instrument_type(self):
        camera = MountOnlyCamera(offline=True)
        ctx = _ctx(camera)
        with pytest.raises(ProtocolExecutionError, match="not a"):
            cure(
                ctx, instrument="uv_curing", position="plate_1.A1",
                measurement_height=0.0, exposure_time=1.0,
            )

    def test_persists_cure_measurement_when_campaign_present(self):
        store = DataStore(db_path=":memory:")
        campaign_id = store.create_campaign(description="cure")
        store.register_labware(campaign_id, "plate_1", _plate())
        uv = _uv()
        cure(
            _ctx(uv, data_store=store, campaign_id=campaign_id),
            instrument="uv_curing", position="plate_1.A1",
            measurement_height=0.0, exposure_time=1.5, intensity=60.0,
        )

        row = store._conn.execute(
            """
            SELECT e.well_id, m.intensity_percent, m.exposure_time_s
            FROM experiments e
            JOIN uv_curing_measurements m ON m.experiment_id = e.id
            WHERE e.campaign_id = ?
            """,
            (campaign_id,),
        ).fetchone()
        assert row[0] == "A1"
        assert row[1] == pytest.approx(60.0)
        assert row[2] == pytest.approx(1.5)
        store.close()
