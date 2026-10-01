"""Tests for measurement logging and content tracking via DataStore in protocol commands."""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock


from cubos.data.data_store import DataStore
from cubos.deck.deck import Deck
from cubos.deck.labware.labware import Coordinate3D
from cubos.deck.labware.vial import Vial
from cubos.deck.labware.well_plate import WellPlate
from cubos.instruments.base_instrument import BaseInstrument
from cubos.instruments.uvvis_ccs.models import UVVisSpectrum
from cubos.protocol_engine.runtime import ProtocolContext


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_2x2_plate() -> WellPlate:
    return WellPlate(
        name="plate_1",
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


def _make_vial(name: str = "vial_1") -> Vial:
    return Vial(
        name=name,
        model_name="test_vial",
        height=50.0,
        diameter=20.0,
        location=Coordinate3D(x=50.0, y=0.0, z=0.0),
        capacity_ul=5000.0,
        working_volume_ul=4000.0,
    )


class _FakeSensor(BaseInstrument):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._spectrum = UVVisSpectrum(
            wavelengths=(500.0, 501.0, 502.0),
            intensities=(0.1, 0.2, 0.3),
            integration_time_s=0.24,
        )

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def health_check(self) -> bool:
        return True

    def measure(self) -> UVVisSpectrum:
        return self._spectrum


def _make_store_with_labware(plate=None, vial=None):
    """Create a DataStore, campaign, and register labware. Returns (store, campaign_id)."""
    store = DataStore(db_path=":memory:")
    cid = store.create_campaign(description="test")
    if plate:
        store.register_labware(cid, "plate_1", plate)
    if vial:
        store.register_labware(cid, "vial_1", vial)
    return store, cid


def _mock_context(
    plate=None, vial=None, sensor=None, store=None, campaign_id=None,
) -> ProtocolContext:
    plate = plate or _make_2x2_plate()
    vial = vial or _make_vial()
    sensor = sensor or _FakeSensor(
        name="uvvis", offset_x=0.0, offset_y=0.0, depth=0.0,
    )

    labware_map = {"plate_1": plate, "vial_1": vial}

    board = MagicMock()
    board.instruments = {"uvvis": sensor, "pipette": MagicMock()}
    board.instruments["pipette"].aspirate = MagicMock(return_value=None)
    board.instruments["pipette"].dispense = MagicMock(return_value=None)

    deck = MagicMock()
    deck.__getitem__ = MagicMock(side_effect=lambda k: labware_map[k])
    deck.resolve_coordinate = MagicMock(return_value=(0.0, 0.0, 0.0))
    deck.resolve_labware = MagicMock(side_effect=lambda k: labware_map[k])
    deck.resolve_labware_target = MagicMock(
        side_effect=Deck(labware_map).resolve_labware_target,
    )

    return ProtocolContext(
        gantry=board,
        deck=deck,
        logger=logging.getLogger("test_command_logging"),
        data_store=store,
        campaign_id=campaign_id,
    )


# ─── Scan command logging tests ──────────────────────────────────────────────


class TestScanCommandLogging:

    def test_logs_per_well_measurement(self):
        from cubos.protocol_engine.commands.scan import scan

        plate = _make_2x2_plate()
        store, cid = _make_store_with_labware(plate=plate)
        ctx = _mock_context(plate=plate, store=store, campaign_id=cid)

        scan(ctx, plate="plate_1", instrument="uvvis", method="measure", measurement_height=0.0, interwell_scan_height=10.0)

        uvvis_count = store._conn.execute(
            "SELECT COUNT(*) FROM uvvis_measurements"
        ).fetchone()[0]
        assert uvvis_count == 4

    def test_captures_contents_from_db(self):
        from cubos.protocol_engine.commands.scan import scan

        plate = _make_2x2_plate()
        store, cid = _make_store_with_labware(plate=plate, vial=_make_vial())
        store.record_dispense(cid, "plate_1", "A1", "vial_1", 50.0)
        ctx = _mock_context(plate=plate, store=store, campaign_id=cid)

        scan(ctx, plate="plate_1", instrument="uvvis", method="measure", measurement_height=0.0, interwell_scan_height=10.0)

        row = store._conn.execute(
            "SELECT contents FROM experiments WHERE well_id = 'A1'"
        ).fetchone()
        parsed = json.loads(row[0])
        assert parsed[0]["source"] == "vial_1"


# ─── Pipette command DB tracking tests ────────────────────────────────────────


class TestPipetteDbTracking:

    def test_transfer_records_source_in_labware_table(self):
        from cubos.protocol_engine.commands.pipette import transfer

        plate = _make_2x2_plate()
        vial = _make_vial()
        store, cid = _make_store_with_labware(plate=plate, vial=vial)

        labware_map = {"plate_1": plate, "vial_1": vial}
        board = MagicMock()
        pipette = MagicMock()
        board.instruments = {"pipette": pipette}

        deck = Deck(labware_map)

        ctx = ProtocolContext(
            gantry=board, deck=deck,
            logger=logging.getLogger("test"),
            data_store=store, campaign_id=cid,
        )

        transfer(ctx, source="vial_1", destination="plate_1.B2", volume_ul=75.0)

        contents = store.get_contents(cid, "plate_1", "B2")
        assert contents[0]["source"] == "vial_1"
        assert contents[0]["volume_ul"] == 75.0
