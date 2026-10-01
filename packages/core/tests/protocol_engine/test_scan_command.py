"""Tests for the scan protocol command.

The scan command's per-well motion under the labware-relative model is:

* **First well of the plate**: ``move_to_labware`` (travels XY at gantry
  ``safe_z``) → raw move to ``height + interwell_scan_height`` →
  raw move to ``height + measurement_height`` → call instrument method.
* **Subsequent wells**: raw move to ``height + interwell_scan_height``
  with ``travel_z`` set so XY travel happens at that height → raw move to
  the action plane → call instrument method.
* **End of scan**: raw move that lifts back to ``height + interwell_scan_height``.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest

from cubos.data.data_store import DataStore
from cubos.deck.deck import Deck
from cubos.deck.labware.labware import Coordinate3D
from cubos.deck.labware.well_plate import WellPlate
from cubos.deck.labware.well_plate_holder import WellPlateHolder
from cubos.deck.loader import load_deck_from_yaml_safe
from cubos.instruments.base_instrument import BaseInstrument
from cubos.instruments.filmetrics.models import MeasurementResult
from cubos.instruments.uv_curing.models import CureResult
from cubos.protocol_engine.errors import ProtocolExecutionError
from cubos.protocol_engine.runtime import ProtocolContext


HEIGHT_MM = 14.10
SAFE_APPROACH = 10.0
MEASUREMENT = 1.0
APPROACH_ABS = HEIGHT_MM + SAFE_APPROACH
ACTION_ABS = HEIGHT_MM + MEASUREMENT


TRACKED_SCAN_DECK_YAML = """\
labware:
  source:
    type: vial
    name: source
    model_name: source_vial
    height: 50.0
    diameter: 20.0
    location: {x: 5.0, y: 5.0, z: 20.0}
    capacity_ul: 500.0
    working_volume_ul: 400.0

  plate:
    type: well_plate
    name: plate_1
    model_name: test_2x2
    length: 30.0
    width: 20.0
    height: 14.1
    well_depth: 10.0
    rows: 2
    columns: 2
    calibration:
      a1: {x: 20.0, y: 20.0, z: 14.1}
      a2: {x: 30.0, y: 20.0, z: 14.1}
    x_offset: 10.0
    y_offset: 8.0
    capacity_ul: 200.0
    working_volume_ul: 150.0
"""


def _make_2x2_plate() -> WellPlate:
    return WellPlate(
        name="plate_1",
        model_name="test_96",
        length=127.71,
        width=85.43,
        height=HEIGHT_MM,
        rows=2,
        columns=2,
        wells={
            "A1": Coordinate3D(x=0.0, y=0.0, z=HEIGHT_MM),
            "A2": Coordinate3D(x=10.0, y=0.0, z=HEIGHT_MM),
            "B1": Coordinate3D(x=0.0, y=8.0, z=HEIGHT_MM),
            "B2": Coordinate3D(x=10.0, y=8.0, z=HEIGHT_MM),
        },
        capacity_ul=200.0,
        working_volume_ul=150.0,
    )


def _make_2x3_plate() -> WellPlate:
    return WellPlate(
        name="plate_2",
        model_name="test_model",
        length=127.71,
        width=85.43,
        height=HEIGHT_MM,
        rows=2,
        columns=3,
        wells={
            "A1": Coordinate3D(x=0.0, y=0.0, z=HEIGHT_MM),
            "A2": Coordinate3D(x=10.0, y=0.0, z=HEIGHT_MM),
            "A3": Coordinate3D(x=20.0, y=0.0, z=HEIGHT_MM),
            "B1": Coordinate3D(x=0.0, y=8.0, z=HEIGHT_MM),
            "B2": Coordinate3D(x=10.0, y=8.0, z=HEIGHT_MM),
            "B3": Coordinate3D(x=20.0, y=8.0, z=HEIGHT_MM),
        },
        capacity_ul=200.0,
        working_volume_ul=150.0,
    )


class _FakeSensor(BaseInstrument):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.call_count = 0
        self._return_value = True

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def health_check(self) -> bool:
        return True

    def measure(self) -> bool:
        self.call_count += 1
        return self._return_value

    def indentation(
        self,
        *,
        measurement_height: float,
        indentation_limit_height: float,
        well_z: float,
        gantry=None,
    ) -> dict:
        self.call_count += 1
        return {
            "measurement_height": measurement_height,
            "indentation_limit_height": indentation_limit_height,
            "well_z": well_z,
            "gantry": gantry,
        }


class _FakeFilmetrics(BaseInstrument):
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def health_check(self) -> bool:
        return True

    def measure(self) -> MeasurementResult:
        return MeasurementResult(thickness_nm=151.2, goodness_of_fit=0.96)


class _FakeUVCuring(BaseInstrument):
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def health_check(self) -> bool:
        return True

    def cure(self) -> CureResult:
        return CureResult(
            intensity_percent=60.0,
            exposure_time_s=1.5,
            timestamp=123.4,
        )


def _make_sensor(**kwargs) -> _FakeSensor:
    defaults = dict(name="sensor", offset_x=0.0, offset_y=0.0, depth=0.0)
    defaults.update(kwargs)
    return _FakeSensor(**defaults)


def _mock_context(
    plate: WellPlate | None = None,
    sensor: _FakeSensor | None = None,
) -> ProtocolContext:
    plate = plate or _make_2x2_plate()
    sensor = sensor or _make_sensor()

    board = MagicMock()
    board.instruments = {"uvvis": sensor}
    deck = Deck({plate.name: plate})

    return ProtocolContext(
        gantry=board,
        deck=deck,
        logger=logging.getLogger("test_scan_command"),
    )


def _scan_args(**overrides):
    args = {
        "plate": "plate_1",
        "instrument": "uvvis",
        "method": "measure",
        "measurement_height": MEASUREMENT,
        "interwell_scan_height": SAFE_APPROACH,
    }
    args.update(overrides)
    return args


class TestScanCommand:


    def test_visits_wells_in_row_major_order(self):
        from cubos.protocol_engine.commands.scan import scan

        plate = _make_2x3_plate()
        ctx = _mock_context(plate=plate)

        scan(ctx, plate="plate_2", instrument="uvvis", method="measure",
             measurement_height=MEASUREMENT, interwell_scan_height=SAFE_APPROACH)

        action_zs = [
            c for c in ctx.gantry.move.call_args_list
            if c.args[1][2] == ACTION_ABS
        ]
        xs = [c.args[1][0] for c in action_zs]
        assert xs == [0.0, 10.0, 20.0, 0.0, 10.0, 20.0]

    def test_per_well_motion_uses_relative_offsets(self):
        """First well: move_to_labware → approach Z → action Z.
        Subsequent wells: approach (with travel_z) → action Z.
        Plus a final retract."""
        from cubos.protocol_engine.commands.scan import scan

        ctx = _mock_context()

        scan(ctx, **_scan_args())

        zs = [c.args[1][2] for c in ctx.gantry.move.call_args_list]
        assert zs == [
            APPROACH_ABS, ACTION_ABS,    # A1
            APPROACH_ABS, ACTION_ABS,    # A2
            APPROACH_ABS, ACTION_ABS,    # B1
            APPROACH_ABS, ACTION_ABS,    # B2
            APPROACH_ABS,                # final retract
        ]

    def test_first_well_descend_does_not_pass_travel_z(self):
        """Action descents must NOT pass travel_z (would re-introduce
        the lift-then-drop detour)."""
        from cubos.protocol_engine.commands.scan import scan

        ctx = _mock_context()

        scan(ctx, **_scan_args())

        action_calls = [
            c for c in ctx.gantry.move.call_args_list
            if c.args[1][2] == ACTION_ABS
        ]
        for call in action_calls:
            assert call.kwargs.get("travel_z") is None

    def test_subsequent_wells_use_travel_z_for_xy_at_approach(self):
        from cubos.protocol_engine.commands.scan import scan

        ctx = _mock_context()

        scan(ctx, **_scan_args())

        approach_calls = [
            c for c in ctx.gantry.move.call_args_list
            if c.args[1][2] == APPROACH_ABS and c.kwargs.get("travel_z") is not None
        ]
        assert len(approach_calls) == 4
        for call in approach_calls:
            assert call.kwargs["travel_z"] == APPROACH_ABS

    def test_final_retract_returns_to_safe_approach(self):
        from cubos.protocol_engine.commands.scan import scan

        ctx = _mock_context()

        scan(ctx, **_scan_args())

        last_move = ctx.gantry.move.call_args_list[-1]
        instr_name, position = last_move.args
        assert instr_name == "uvvis"
        assert position == (10.0, 8.0, APPROACH_ABS)
        assert last_move.kwargs.get("travel_z") == APPROACH_ABS

    def test_measurement_height_from_command_arg_drives_action_z(self):
        """Action plane is `plate.height + scan.measurement_height`."""
        from cubos.protocol_engine.commands.scan import scan

        ctx = _mock_context()

        scan(ctx, **_scan_args(measurement_height=2.0))

        action_zs = [
            c.args[1][2] for c in ctx.gantry.move.call_args_list
            if c.args[1][2] == HEIGHT_MM + 2.0
        ]
        assert len(action_zs) == 4

    def test_missing_measurement_height_rejected(self):
        from cubos.protocol_engine.commands.scan import scan

        ctx = _mock_context()

        args = _scan_args()
        args.pop("measurement_height")
        with pytest.raises(TypeError, match="measurement_height"):
            scan(ctx, **args)

    def test_missing_interwell_scan_height_rejected(self):
        from cubos.protocol_engine.commands.scan import scan

        ctx = _mock_context()

        args = _scan_args()
        args.pop("interwell_scan_height")
        with pytest.raises(TypeError, match="interwell_scan_height"):
            scan(ctx, **args)

    def test_safe_approach_below_measurement_rejected(self):
        from cubos.protocol_engine.commands.scan import scan

        ctx = _mock_context()

        with pytest.raises(ProtocolExecutionError, match="Approach must be at or above"):
            scan(ctx, **_scan_args(measurement_height=5.0, interwell_scan_height=2.0))

    def test_relative_offsets_and_well_z_passed_to_method_when_supported(self):
        """When the method signature has ``measurement_height`` /
        ``indentation_limit_height`` / ``well_z``, the engine forwards
        the user's relative offsets verbatim and injects ``well_z`` from
        the plate's calibrated well coordinate. The instrument resolves
        absolute Z values internally."""
        from cubos.protocol_engine.commands.scan import scan

        ctx = _mock_context()
        ctx.gantry.controller = object()

        results = scan(
            ctx, plate="plate_1", instrument="uvvis", method="indentation",
            measurement_height=2.0,
            interwell_scan_height=SAFE_APPROACH,
            indentation_limit_height=-5.0,   # 5 mm into the well at deepest
        )

        for r in results.values():
            assert r["measurement_height"] == 2.0
            assert r["indentation_limit_height"] == -5.0
            assert r["well_z"] == HEIGHT_MM

    def test_detect_surface_limit_not_compared_to_measurement_height(self):
        """With detect_surface the limit is anchored to the detected
        surface, so a limit above measurement_height (different frames)
        must not be rejected."""
        from cubos.protocol_engine.commands.scan import scan

        class _DetectSensor(_FakeSensor):
            def indentation(
                self,
                *,
                measurement_height: float,
                indentation_limit_height: float,
                well_z: float,
                gantry=None,
                detect_surface: bool = False,
            ) -> dict:
                self.call_count += 1
                return {
                    "measurement_height": measurement_height,
                    "indentation_limit_height": indentation_limit_height,
                    "well_z": well_z,
                    "detect_surface": detect_surface,
                }

        sensor = _DetectSensor(
            name="sensor", offset_x=0.0, offset_y=0.0, depth=0.0,
        )
        ctx = _mock_context(sensor=sensor)
        ctx.gantry.controller = object()

        results = scan(
            ctx, plate="plate_1", instrument="uvvis", method="indentation",
            measurement_height=-2.0,
            interwell_scan_height=SAFE_APPROACH,
            indentation_limit_height=-1.0,
            method_kwargs={"detect_surface": True},
        )

        for r in results.values():
            assert r["detect_surface"] is True
            assert r["indentation_limit_height"] == -1.0

    def test_detect_surface_rejects_positive_limit(self):
        from cubos.protocol_engine.commands.scan import scan

        ctx = _mock_context()

        with pytest.raises(ProtocolExecutionError, match="detect_surface"):
            scan(
                ctx, plate="plate_1", instrument="uvvis", method="indentation",
                measurement_height=MEASUREMENT,
                interwell_scan_height=SAFE_APPROACH,
                indentation_limit_height=0.5,
                method_kwargs={"detect_surface": True},
            )

    def test_legacy_z_limit_rejected_at_runtime(self):
        from cubos.protocol_engine.commands.scan import scan

        ctx = _mock_context()

        with pytest.raises(ProtocolExecutionError, match="z_limit"):
            scan(
                ctx, plate="plate_1", instrument="uvvis", method="indentation",
                measurement_height=MEASUREMENT,
                interwell_scan_height=SAFE_APPROACH,
                method_kwargs={"z_limit": 5.0},
            )

    def test_legacy_interwell_travel_height_rejected(self):
        from cubos.protocol_engine.commands.scan import scan

        ctx = _mock_context()

        with pytest.raises(ProtocolExecutionError, match="interwell_travel_height"):
            scan(
                ctx, plate="plate_1", instrument="uvvis", method="measure",
                measurement_height=MEASUREMENT,
                interwell_scan_height=SAFE_APPROACH,
                method_kwargs={"interwell_travel_height": 5.0},
            )

    def test_returns_dict_of_results_per_well(self):
        from cubos.protocol_engine.commands.scan import scan

        ctx = _mock_context()

        result = scan(ctx, **_scan_args())

        assert set(result.keys()) == {"A1", "A2", "B1", "B2"}
        assert all(v is True for v in result.values())


    def test_calls_method_once_per_well(self):
        from cubos.protocol_engine.commands.scan import scan

        sensor = _make_sensor()
        ctx = _mock_context(sensor=sensor)

        scan(ctx, **_scan_args())

        assert sensor.call_count == 4


    def test_sharc_nested_plate_persists_under_canonical_labware_key(self):
        from cubos.protocol_engine.commands.scan import scan

        plate = _make_2x2_plate()
        holder = WellPlateHolder(
            name="plate_holder",
            location=Coordinate3D(x=0.0, y=0.0, z=0.0),
            contained_labware={"plate": plate},
        )
        deck = Deck(
            {"plate_holder": holder},
            volume_labware={"plate_holder__plate": plate},
            target_aliases={"plate_holder.plate": "plate_holder__plate"},
        )
        filmetrics = _FakeFilmetrics(
            name="filmetrics", offset_x=0.0, offset_y=0.0, depth=0.0,
        )
        board = MagicMock()
        board.instruments = {"filmetrics": filmetrics}
        store = DataStore(db_path=":memory:")
        campaign_id = store.create_campaign(description="SHARC nested scan")
        store.register_labware(
            campaign_id, "plate_holder__plate", plate,
        )
        ctx = ProtocolContext(
            gantry=board,
            deck=deck,
            data_store=store,
            campaign_id=campaign_id,
        )

        scan(
            ctx,
            plate="plate_holder.plate",
            instrument="filmetrics",
            method="measure",
            measurement_height=MEASUREMENT,
            interwell_scan_height=SAFE_APPROACH,
        )

        rows = store._conn.execute(
            """
            SELECT labware_key, well_id
            FROM experiments
            WHERE campaign_id = ?
            ORDER BY id
            """,
            (campaign_id,),
        ).fetchall()
        assert [row[0] for row in rows] == ["plate_holder__plate"] * 4
        assert [row[1] for row in rows] == ["A1", "A2", "B1", "B2"]
        store.close()

    def test_validates_plate_is_wellplate(self):
        from cubos.protocol_engine.commands.scan import scan

        ctx = ProtocolContext(
            gantry=MagicMock(instruments={"uvvis": _make_sensor()}),
            deck=Deck({"vial_1": MagicMock(spec=[])}),
            logger=logging.getLogger("test_scan_command"),
        )

        with pytest.raises(ProtocolExecutionError, match="WellPlate"):
            scan(ctx, plate="vial_1", instrument="uvvis", method="measure",
                 measurement_height=MEASUREMENT, interwell_scan_height=SAFE_APPROACH)

    def test_unknown_instrument_raises(self):
        from cubos.protocol_engine.commands.scan import scan

        ctx = _mock_context()

        with pytest.raises(ProtocolExecutionError, match="instrument"):
            scan(ctx, plate="plate_1", instrument="nonexistent", method="measure",
                 measurement_height=MEASUREMENT, interwell_scan_height=SAFE_APPROACH)

    def test_unknown_method_raises(self):
        from cubos.protocol_engine.commands.scan import scan

        ctx = _mock_context()

        with pytest.raises(ProtocolExecutionError, match="method"):
            scan(ctx, plate="plate_1", instrument="uvvis", method="nonexistent",
                 measurement_height=MEASUREMENT, interwell_scan_height=SAFE_APPROACH)


    def test_tracked_transfer_scan_uses_current_fluid_composition(self, tmp_path):
        from cubos.protocol_engine.commands.pipette import transfer
        from cubos.protocol_engine.commands.scan import scan

        deck_path = tmp_path / "deck.yaml"
        deck_path.write_text(TRACKED_SCAN_DECK_YAML, encoding="utf-8")
        deck = load_deck_from_yaml_safe(deck_path)
        store = DataStore(db_path=":memory:")
        state_id = store.create_fluid_state(
            deck_path,
            deck,
            initial_fluids={
                "source": {
                    "volume_ul": 100.0,
                    "composition": {"water": 60.0, "ethanol": 40.0},
                },
            },
        )
        campaign_id = store.create_campaign(
            description="tracked scan",
            fluid_state_id=state_id,
        )
        board = MagicMock()
        board.instruments = {
            "pipette": MagicMock(),
            "filmetrics": _FakeFilmetrics(
                name="filmetrics", offset_x=0.0, offset_y=0.0, depth=0.0,
            ),
        }
        ctx = ProtocolContext(
            gantry=board,
            deck=deck,
            data_store=store,
            campaign_id=campaign_id,
            fluid_state_id=state_id,
        )

        transfer(
            ctx,
            source="source",
            destination="plate.A1",
            volume_ul=50.0,
        )
        store.get_contents = MagicMock(
            side_effect=AssertionError("tracked scan used legacy contents"),
        )
        scan(
            ctx,
            plate="plate",
            instrument="filmetrics",
            method="measure",
            measurement_height=MEASUREMENT,
            interwell_scan_height=SAFE_APPROACH,
        )

        rows = store._conn.execute(
            "SELECT well_id, contents FROM experiments ORDER BY id"
        ).fetchall()
        assert [row[0] for row in rows] == ["A1", "A2", "B1", "B2"]
        assert json.loads(rows[0][1]) == [
            {"source": "ethanol", "volume_ul": 20.0},
            {"source": "water", "volume_ul": 30.0},
        ]
        assert all(json.loads(row[1]) == [] for row in rows[1:])
        store.get_contents.assert_not_called()
        store.close()

    def test_tracked_scan_missing_container_fails_before_first_move(self):
        from cubos.protocol_engine.commands.scan import scan

        ctx = _mock_context()
        ctx.data_store = MagicMock()
        ctx.data_store.get_campaign_fluid_state_id.return_value = 9
        ctx.data_store.get_fluid_snapshot.return_value = {
            "containers": [
                {
                    "labware_key": "plate_1",
                    "location_id": "A1",
                    "current_volume_ul": 0.0,
                    "composition": {},
                }
            ]
        }
        ctx.campaign_id = 13
        ctx.fluid_state_id = 9

        with pytest.raises(ProtocolExecutionError, match="has no container"):
            scan(ctx, **_scan_args())

        ctx.gantry.move_to_labware.assert_not_called()
        ctx.gantry.move.assert_not_called()
        assert ctx.gantry.instruments["uvvis"].call_count == 0


    def test_malformed_asmi_measurement_warns_and_returns_results(self, caplog):
        from cubos.protocol_engine.commands.scan import scan

        sensor = _make_sensor()
        sensor._return_value = {
            "measurements": [
                {
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
        }
        ctx = _mock_context(sensor=sensor)
        ctx.data_store = DataStore(db_path=":memory:")
        ctx.campaign_id = ctx.data_store.create_campaign(description="scan")
        ctx.data_store.register_labware(
            ctx.campaign_id,
            "plate_1",
            ctx.deck.resolve_labware("plate_1"),
        )

        with caplog.at_level(logging.WARNING):
            results = scan(ctx, **_scan_args())

        assert set(results) == {"A1", "A2", "B1", "B2"}
        assert "Failed to log measurement for well" in caplog.text
        assert ctx.data_store._conn.execute(
            "SELECT COUNT(*) FROM experiments"
        ).fetchone()[0] == 0
        ctx.data_store.close()


    def test_uv_health_check_does_not_persist_or_fail(self, caplog):
        from cubos.protocol_engine.commands.scan import scan

        plate = _make_2x2_plate()
        uv_curing = _FakeUVCuring(
            name="uv_curing", offset_x=0.0, offset_y=0.0, depth=0.0,
        )
        board = MagicMock()
        board.instruments = {"uv_curing": uv_curing}
        store = DataStore(db_path=":memory:")
        campaign_id = store.create_campaign(description="uv curing scan")
        store.register_labware(campaign_id, "plate_1", plate)
        ctx = ProtocolContext(
            gantry=board,
            deck=Deck({"plate_1": plate}),
            data_store=store,
            campaign_id=campaign_id,
        )

        with caplog.at_level(logging.WARNING):
            results = scan(
                ctx,
                **_scan_args(instrument="uv_curing", method="health_check"),
            )

        assert results == {"A1": True, "A2": True, "B1": True, "B2": True}
        assert "not persistable" in caplog.text
        assert store._conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 0
        assert store._conn.execute(
            "SELECT COUNT(*) FROM uv_curing_measurements"
        ).fetchone()[0] == 0
        store.close()
