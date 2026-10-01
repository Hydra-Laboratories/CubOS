"""Tests for the ``measure`` protocol command."""

import json
import logging
from unittest.mock import MagicMock

import pytest

from cubos.data.data_store import DataStore
from cubos.deck.deck import Deck
from cubos.deck.labware.labware import Coordinate3D
from cubos.deck.labware.well_plate import WellPlate
from cubos.deck.loader import load_deck_from_yaml_safe
from cubos.instruments.base_instrument import BaseInstrument
from cubos.instruments.filmetrics.models import MeasurementResult
from cubos.instruments.uv_curing.models import CureResult
from cubos.protocol_engine.commands.measure import measure
from cubos.protocol_engine.errors import ProtocolExecutionError
from cubos.protocol_engine.runtime import ProtocolContext


HEIGHT_MM = 14.10


VIAL_GRID_DECK_YAML = """\
labware:
  reagents:
    type: vial_grid
    name: reagents
    model_name: reagent_grid
    rows: 1
    columns: 2
    calibration:
      a1: {x: 10.0, y: 20.0, z: 30.0}
      a2: {x: 20.0, y: 20.0, z: 30.0}
    x_offset: 10.0
    y_offset: 10.0
    vial_model_name: reagent_vial
    vial_height: 40.0
    vial_diameter: 12.0
    capacity_ul: 500.0
    working_volume_ul: 400.0
    aliases:
      buffer: A1
      product: A2
"""


def _mock_instr():
    instr = MagicMock(spec=BaseInstrument)
    instr.name = "uvvis"
    instr.offset_x = 0.0
    instr.offset_y = 0.0
    instr.depth = 0.0
    instr.measure = MagicMock(return_value="spectrum")
    return instr


def _ctx(instr, well_coord=None, height=HEIGHT_MM):
    well_coord = well_coord or Coordinate3D(x=10.0, y=20.0, z=height or 0.0)
    board = MagicMock()
    board.instruments = {"uvvis": instr}
    deck = MagicMock()
    deck.resolve_coordinate = MagicMock(return_value=well_coord)
    labware = MagicMock(height=height)
    deck.__getitem__ = MagicMock(return_value=labware)
    return ProtocolContext(gantry=board, deck=deck)


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


class _FakeASMI:
    def indentation(
        self,
        *,
        gantry,
        well_z,
        measurement_height,
        indentation_limit_height,
        step_size=0.1,
        force_limit=10.0,
    ):
        del gantry, well_z, measurement_height, indentation_limit_height
        return {
            "measurements": [
                {
                    "timestamp": 1.0,
                    "z_mm": 13.0,
                    "raw_force_n": 0.2,
                    "corrected_force_n": 0.1,
                    "direction": "down",
                }
            ],
            "baseline_avg": 0.1,
            "baseline_std": 0.01,
            "force_exceeded": False,
            "data_points": 1,
            "measure_with_return": False,
            "step_size_mm": step_size,
            "z_target_mm": 13.0,
            "force_limit_n": force_limit,
        }


class _FakeFilmetrics:
    def measure(self):
        return MeasurementResult(thickness_nm=151.2, goodness_of_fit=0.96)


class _FakeUVCuring:
    def cure(self):
        return CureResult(
            intensity_percent=60.0,
            exposure_time_s=1.5,
            timestamp=123.4,
        )

    def health_check(self):
        return True


def test_measure_travels_at_safe_z_then_descends():
    """measure: move_to_labware (XY at safe_z), then descend to
    height + measurement_height, then call the method."""
    instr = _mock_instr()
    coord = Coordinate3D(x=10.0, y=20.0, z=HEIGHT_MM)
    ctx = _ctx(instr, well_coord=coord)

    result = measure(
        ctx, instrument="uvvis", position="plate_1.A1",
        measurement_height=2.0,
    )

    ctx.gantry.move_to_labware.assert_called_once_with("uvvis", coord)
    ctx.gantry.move.assert_called_once_with("uvvis", (10.0, 20.0, HEIGHT_MM + 2.0))
    instr.measure.assert_called_once()
    assert result == "spectrum"


def test_measure_with_negative_offset_descends_below_surface():
    """Negative measurement_height = below the labware surface."""
    instr = _mock_instr()
    coord = Coordinate3D(x=10.0, y=20.0, z=HEIGHT_MM)
    ctx = _ctx(instr, well_coord=coord)

    measure(
        ctx, instrument="uvvis", position="plate_1.A1",
        measurement_height=-1.0,
    )

    ctx.gantry.move.assert_called_once_with("uvvis", (10.0, 20.0, HEIGHT_MM - 1.0))


def test_measure_persists_single_asmi_indentation_when_campaign_is_present():
    plate = _plate()
    board = MagicMock()
    board.controller = object()
    board.instruments = {"asmi": _FakeASMI()}
    deck = Deck({"plate_1": plate})
    store = DataStore(db_path=":memory:")
    campaign_id = store.create_campaign(description="measure")
    store.register_labware(campaign_id, "plate_1", plate)
    ctx = ProtocolContext(
        gantry=board,
        deck=deck,
        data_store=store,
        campaign_id=campaign_id,
    )

    measure(
        ctx,
        instrument="asmi",
        position="plate_1.A1",
        measurement_height=0.0,
        method="indentation",
        indentation_limit_height=-1.0,
    )

    row = store._conn.execute(
        """
        SELECT e.labware_name, e.well_id, m.sample_timestamps, m.directions,
               e.labware_key, m.step_size_mm, m.z_target_mm, m.force_limit_n
        FROM experiments e
        JOIN asmi_measurements m ON m.experiment_id = e.id
        WHERE e.campaign_id = ?
        """,
        (campaign_id,),
    ).fetchone()
    assert row[0] == "test_plate"
    assert row[1] == "A1"
    assert row[2] == "[1.0]"
    assert row[3] == '["down"]'
    assert row[4] == "plate_1"
    assert row[5] == pytest.approx(0.1)
    assert row[6] == pytest.approx(13.0)
    assert row[7] == pytest.approx(10.0)
    store.close()


def test_measure_uses_current_tracked_composition_for_vial_grid_alias(tmp_path):
    deck_path = tmp_path / "deck.yaml"
    deck_path.write_text(VIAL_GRID_DECK_YAML, encoding="utf-8")
    deck = load_deck_from_yaml_safe(deck_path)
    store = DataStore(db_path=":memory:")
    state_id = store.create_fluid_state(
        deck_path,
        deck,
        initial_fluids={
            "reagents.buffer": {
                "volume_ul": 100.0,
                "composition": {"water": 100.0},
            },
        },
    )
    campaign_id = store.create_campaign(
        description="tracked vial measurement",
        fluid_state_id=state_id,
    )
    source = deck.resolve_labware_target("reagents.buffer")
    destination = deck.resolve_labware_target("reagents.product")
    assert store.begin_fluid_transfer(
        state_id,
        "tracked-vial-transfer",
        source,
        destination,
        25.0,
        campaign_id=campaign_id,
    ) is True
    store.complete_fluid_transfer("tracked-vial-transfer")
    store.get_contents = MagicMock(
        side_effect=AssertionError("tracked measurement used legacy contents"),
    )

    board = MagicMock()
    board.instruments = {"filmetrics": _FakeFilmetrics()}
    ctx = ProtocolContext(
        gantry=board,
        deck=deck,
        data_store=store,
        campaign_id=campaign_id,
        fluid_state_id=state_id,
    )

    measure(
        ctx,
        instrument="filmetrics",
        position="reagents.product",
        measurement_height=0.0,
    )

    row = store._conn.execute(
        "SELECT labware_key, well_id, contents FROM experiments"
    ).fetchone()
    assert row[0] == "reagents"
    assert row[1] == "A2"
    assert json.loads(row[2]) == [{"source": "water", "volume_ul": 25.0}]
    store.get_contents.assert_not_called()
    store.close()


def test_measure_active_state_missing_target_fails_before_motion():
    plate = _plate()
    instr = _mock_instr()
    board = MagicMock()
    board.instruments = {"uvvis": instr}
    store = MagicMock()
    store.get_campaign_fluid_state_id.return_value = 7
    store.get_fluid_snapshot.return_value = {"containers": []}
    ctx = ProtocolContext(
        gantry=board,
        deck=Deck({"plate_1": plate}),
        data_store=store,
        campaign_id=12,
        fluid_state_id=7,
    )

    with pytest.raises(ProtocolExecutionError, match="has no container"):
        measure(
            ctx,
            instrument="uvvis",
            position="plate_1.A1",
            measurement_height=0.0,
        )

    board.move_to_labware.assert_not_called()
    board.move.assert_not_called()
    instr.measure.assert_not_called()


def test_measure_uv_health_check_does_not_persist_or_fail(caplog):
    plate = _plate()
    board = MagicMock()
    board.controller = object()
    board.instruments = {"uv_curing": _FakeUVCuring()}
    deck = Deck({"plate_1": plate})
    store = DataStore(db_path=":memory:")
    campaign_id = store.create_campaign(description="measure")
    store.register_labware(campaign_id, "plate_1", plate)
    ctx = ProtocolContext(
        gantry=board,
        deck=deck,
        data_store=store,
        campaign_id=campaign_id,
    )

    with caplog.at_level(logging.WARNING):
        result = measure(
            ctx,
            instrument="uv_curing",
            position="plate_1.A1",
            measurement_height=0.0,
            method="health_check",
        )

    assert result is True
    assert "not persistable" in caplog.text
    assert store._conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM uv_curing_measurements"
    ).fetchone()[0] == 0
    store.close()


def test_measure_persistence_failure_returns_result_and_warns(caplog):
    plate = _plate()
    board = MagicMock()
    board.controller = object()
    board.instruments = {"asmi": _FakeASMI()}
    deck = Deck({"plate_1": plate})
    store = DataStore(db_path=":memory:")
    campaign_id = store.create_campaign(description="measure")
    store.register_labware(campaign_id, "plate_1", plate)
    store._conn.execute(
        "UPDATE labware SET contents = ? WHERE labware_key = ? AND well_id = ?",
        ("{bad json", "plate_1", "A1"),
    )
    store._conn.commit()
    ctx = ProtocolContext(
        gantry=board,
        deck=deck,
        data_store=store,
        campaign_id=campaign_id,
    )

    with caplog.at_level(logging.WARNING):
        result = measure(
            ctx,
            instrument="asmi",
            position="plate_1.A1",
            measurement_height=0.0,
            method="indentation",
            indentation_limit_height=-1.0,
        )

    assert result["data_points"] == 1
    assert "Failed to log measurement for position plate_1.A1" in caplog.text
    store.close()


def test_measure_unknown_instrument_raises():
    instr = _mock_instr()
    ctx = _ctx(instr)
    with pytest.raises(ProtocolExecutionError, match="Unknown instrument"):
        measure(
            ctx, instrument="not_a_thing", position="plate_1.A1",
            measurement_height=0.0,
        )


def test_measure_unknown_method_raises():
    instr = MagicMock(spec=BaseInstrument)
    instr.name = "uvvis"
    instr.offset_x = instr.offset_y = instr.depth = 0.0
    ctx = _ctx(instr)
    with pytest.raises(ProtocolExecutionError, match="has no method"):
        measure(
            ctx, instrument="uvvis", position="plate_1.A1",
            measurement_height=0.0, method="nope",
        )


@pytest.mark.parametrize("bad", ["", "1.0", float("nan"), True])
def test_measure_rejects_non_finite_measurement_height(bad):
    instr = _mock_instr()
    ctx = _ctx(instr)
    with pytest.raises(ProtocolExecutionError, match="finite number"):
        measure(
            ctx, instrument="uvvis", position="plate_1.A1",
            measurement_height=bad,
        )


def test_measure_rejects_legacy_indentation_limit_in_method_kwargs():
    """Measure routes its ``method_kwargs`` through the same
    legacy-rename hint table scan uses, so a user porting an old config
    that nested ``indentation_limit`` inside ``method_kwargs`` gets the
    rename hint instead of a generic Python TypeError or silent
    overwrite."""
    instr = _mock_instr()
    ctx = _ctx(instr)
    with pytest.raises(ProtocolExecutionError, match="indentation_limit_height"):
        measure(
            ctx, instrument="uvvis", position="plate_1.A1",
            measurement_height=0.0,
            method_kwargs={"indentation_limit": 5.0},
        )


def test_measure_rejects_safe_approach_height_in_method_kwargs():
    instr = _mock_instr()
    ctx = _ctx(instr)
    with pytest.raises(ProtocolExecutionError, match="interwell_scan_height"):
        measure(
            ctx, instrument="uvvis", position="plate_1.A1",
            measurement_height=0.0,
            method_kwargs={"safe_approach_height": 10.0},
        )


def test_measure_rejects_measurement_height_in_method_kwargs():
    """``measurement_height`` is a top-level command field; nesting it in
    ``method_kwargs`` would let the engine silently overwrite the user's
    value during dispatch injection."""
    instr = _mock_instr()
    ctx = _ctx(instr)
    with pytest.raises(ProtocolExecutionError, match="measurement_height"):
        measure(
            ctx, instrument="uvvis", position="plate_1.A1",
            measurement_height=0.0,
            method_kwargs={"measurement_height": 5.0},
        )


def test_measure_rejects_inverted_indentation_height_before_motion():
    instr = _mock_instr()
    ctx = _ctx(instr)

    with pytest.raises(ProtocolExecutionError, match="indentation_limit_height"):
        measure(
            ctx,
            instrument="uvvis",
            position="plate_1.A1",
            measurement_height=0.0,
            indentation_limit_height=1.0,
        )

    ctx.gantry.move_to_labware.assert_not_called()
    ctx.gantry.move.assert_not_called()
    instr.measure.assert_not_called()


def test_measure_detect_surface_skips_frame_mismatched_height_check():
    """With detect_surface the limit is surface-relative: a limit above
    measurement_height must be accepted (different frames)."""
    calls = []

    class _DetectASMI:
        def indentation(
            self,
            *,
            gantry,
            well_z,
            measurement_height,
            indentation_limit_height,
            detect_surface=False,
        ):
            del gantry, well_z
            calls.append((
                measurement_height, indentation_limit_height, detect_surface,
            ))
            return {"measurements": []}

    instr = _DetectASMI()
    ctx = _ctx(MagicMock())
    ctx.gantry.instruments = {"uvvis": instr}

    measure(
        ctx,
        instrument="uvvis",
        position="plate_1.A1",
        method="indentation",
        measurement_height=-2.0,
        indentation_limit_height=-1.0,
        method_kwargs={"detect_surface": True},
    )

    assert calls == [(-2.0, -1.0, True)]


def test_measure_detect_surface_rejects_positive_limit_before_motion():
    instr = _mock_instr()
    ctx = _ctx(instr)

    with pytest.raises(ProtocolExecutionError, match="detect_surface"):
        measure(
            ctx,
            instrument="uvvis",
            position="plate_1.A1",
            measurement_height=0.0,
            indentation_limit_height=0.5,
            method_kwargs={"detect_surface": True},
        )

    ctx.gantry.move_to_labware.assert_not_called()
    ctx.gantry.move.assert_not_called()
    instr.measure.assert_not_called()
