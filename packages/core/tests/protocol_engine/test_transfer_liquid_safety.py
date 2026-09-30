"""End-to-end Feature-04 tests for the `transfer` protocol command.

Exercises the real stack: offline ``OpentronsPipette`` (real ``PipetteConfig``
driver-boundary validation), real ``Deck`` loaded from YAML, real
``DataStore``/fluid-state journal -- only the gantry is mocked (no motion
hardware offline). Covers stroke splitting, preflight-before-motion guards,
per-stroke durable recovery, state-derived aspiration height, and
liquid-class volume correction semantics.
"""

from __future__ import annotations

import logging
import math
from unittest.mock import MagicMock

import pytest

from cubos.data.data_store import DataStore
from cubos.deck.loader import load_deck_from_yaml_safe
from cubos.instruments.pipette.models import PIPETTE_MODELS
from cubos.instruments.pipette.vendors.opentrons import OpentronsPipette
from cubos.protocol_engine.commands.pipette import transfer
from cubos.protocol_engine.errors import ProtocolExecutionError
from cubos.protocol_engine.runtime import ProtocolContext

P300 = PIPETTE_MODELS["p300_single_gen2"]  # min 20.0 uL, max 200.0 uL

SOURCE_RIM_Z = 50.0
SOURCE_HEIGHT_MM = 100.0
SOURCE_DIAMETER_MM = 20.0
SOURCE_DEAD_VOLUME_UL = 100.0
SOURCE_AREA_MM2 = math.pi * (SOURCE_DIAMETER_MM / 2.0) ** 2

DECK_YAML = f"""\
labware:
  source:
    type: vial
    name: source
    model_name: source_vial
    height: {SOURCE_HEIGHT_MM}
    diameter: {SOURCE_DIAMETER_MM}
    location: {{x: 5.0, y: 5.0, z: {SOURCE_RIM_Z}}}
    capacity_ul: 5000.0
    working_volume_ul: 4500.0
    dead_volume_ul: {SOURCE_DEAD_VOLUME_UL}

  dest:
    type: vial
    name: dest
    model_name: dest_vial
    height: 80.0
    diameter: 25.0
    location: {{x: 40.0, y: 5.0, z: 45.0}}
    capacity_ul: 2000.0
    working_volume_ul: 1500.0

  plate:
    type: well_plate
    name: destination_plate
    model_name: two_well_plate
    length: 30.0
    width: 20.0
    height: 14.0
    well_depth: 10.0
    rows: 1
    columns: 2
    calibration:
      a1: {{x: 70.0, y: 20.0, z: 15.0}}
      a2: {{x: 79.0, y: 20.0, z: 15.0}}
    x_offset: 9.0
    y_offset: 9.0
    capacity_ul: 200.0
    working_volume_ul: 150.0
"""


class RecordingPipette(OpentronsPipette):
    """Offline P300 that records every driver-boundary volume it is commanded.

    ``fail_on_dispense_call`` (1-based) makes the N-th dispense raise, to
    simulate a physical failure mid-way through a multi-stroke transfer.
    """

    def __init__(self, *, fail_on_dispense_call: int | None = None, **kwargs):
        kwargs.setdefault("pipette_model", "p300_single_gen2")
        super().__init__(offline=True, **kwargs)
        self.aspirate_volumes: list[float] = []
        self.dispense_volumes: list[float] = []
        self._fail_on_dispense_call = fail_on_dispense_call
        self._dispense_calls = 0
        self.blowout_calls: list[float] = []
        self.call_order: list[str] = []

    def aspirate(self, volume_ul: float, speed: float = 50.0):
        self.aspirate_volumes.append(volume_ul)
        self.call_order.append("aspirate")
        return super().aspirate(volume_ul, speed)

    def dispense(self, volume_ul: float, speed: float = 50.0):
        self._dispense_calls += 1
        if self._dispense_calls == self._fail_on_dispense_call:
            raise RuntimeError("simulated physical dispense failure")
        self.dispense_volumes.append(volume_ul)
        self.call_order.append("dispense")
        return super().dispense(volume_ul, speed)

    def blowout(self, speed: float = 50.0):
        self.blowout_calls.append(speed)
        self.call_order.append("blowout")
        return super().blowout(speed)


@pytest.fixture()
def tracked_env(tmp_path):
    """Real deck + DataStore + fluid state; only the gantry is mocked."""
    deck_path = tmp_path / "deck.yaml"
    deck_path.write_text(DECK_YAML, encoding="utf-8")
    deck = load_deck_from_yaml_safe(deck_path)
    store = DataStore(db_path=":memory:")
    state_id = store.create_fluid_state(
        deck_path,
        deck,
        initial_fluids={"source": {"volume_ul": 4000.0}},
    )
    campaign_id = store.create_campaign("feature-04", fluid_state_id=state_id)
    yield deck, store, state_id, campaign_id
    store.close()


def _make_context(
    deck,
    store,
    state_id,
    campaign_id,
    pipette,
    *,
    step_index: int = 1,
) -> ProtocolContext:
    board = MagicMock()
    board.instruments = {"pipette": pipette}
    context = ProtocolContext(
        gantry=board,
        deck=deck,
        data_store=store,
        campaign_id=campaign_id,
        fluid_state_id=state_id,
        logger=logging.getLogger("test_transfer_liquid_safety"),
    )
    # Deterministic step-scoped operation keys, as Protocol.execute sets them,
    # so a rerun of the "same step" reuses the same per-stroke keys.
    context.active_step_index = step_index
    context.active_step_command = "transfer"
    return context


def _volumes(store, state_id) -> dict[str, float]:
    snapshot = store.get_fluid_snapshot(state_id)
    return {
        (
            f"{container['labware_key']}.{container['location_id']}"
            if container["location_id"] else container["labware_key"]
        ): container["current_volume_ul"]
        for container in snapshot["containers"]
    }


def _operations(store, state_id):
    return store.get_fluid_snapshot(state_id)["operations"]


# ─── Stroke splitting + one consistent final state ───────────────────────────


class TestStrokeSplitting:

    def test_600ul_on_p300_emits_legal_strokes_and_one_final_state(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        transfer(context, source="source", destination="dest", volume_ul=600.0)

        # >= 2 legal strokes, none above model capacity at the driver boundary.
        assert len(pipette.aspirate_volumes) >= 2
        assert pipette.aspirate_volumes == pipette.dispense_volumes
        for volume in pipette.aspirate_volumes:
            assert P300.min_volume <= volume <= P300.max_volume
        assert sum(pipette.aspirate_volumes) == pytest.approx(600.0)

        # ONE consistent final state update: exactly -600/+600 overall.
        volumes = _volumes(store, state_id)
        assert volumes["source"] == pytest.approx(3400.0)
        assert volumes["dest"] == pytest.approx(600.0)

        operations = _operations(store, state_id)
        assert [operation["status"] for operation in operations] == (
            ["applied"] * len(pipette.aspirate_volumes)
        )
        # Per-stroke keys are distinct but share the logical step scope.
        keys = [operation["operation_key"] for operation in operations]
        assert len(set(keys)) == len(keys)
        assert all("step:1:transfer" in key for key in keys)
        assert all("stroke" in key for key in keys)

    def test_single_stroke_transfer_keeps_unsuffixed_operation_key(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        transfer(context, source="source", destination="dest", volume_ul=50.0)

        operations = _operations(store, state_id)
        assert len(operations) == 1
        assert "stroke" not in operations[0]["operation_key"]
        assert operations[0]["status"] == "applied"

    def test_no_driver_call_ever_exceeds_capacity_even_for_huge_volume(
        self, tracked_env,
    ):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        transfer(context, source="source", destination="dest", volume_ul=1400.0)

        for volume in pipette.aspirate_volumes + pipette.dispense_volumes:
            assert volume <= P300.max_volume + 1e-9
        assert sum(pipette.aspirate_volumes) == pytest.approx(1400.0)


# ─── Blow-out after the final stroke (always runs, not opt-in) ──────────────


class TestBlowOutOnFinalStroke:

    def test_single_stroke_transfer_blows_out_once_after_dispense(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        transfer(
            context, source="source", destination="dest", volume_ul=50.0,
            speed=30.0,
        )

        assert pipette.blowout_calls == [30.0]
        assert pipette.call_order == ["aspirate", "dispense", "blowout"]

    def test_multi_stroke_transfer_blows_out_only_after_the_final_stroke(
        self, tracked_env,
    ):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        transfer(context, source="source", destination="dest", volume_ul=600.0)

        assert len(pipette.aspirate_volumes) >= 2
        assert pipette.blowout_calls == [50.0]
        assert pipette.call_order == (
            ["aspirate", "dispense"] * (len(pipette.aspirate_volumes) - 1)
            + ["aspirate", "dispense", "blowout"]
        )


# ─── Preflight rejects before motion ─────────────────────────────────────────


class TestPreflightBeforeMotion:

    @pytest.mark.parametrize(
        ("volume", "match"),
        [
            (0.0, "> 0"),
            (-25.0, "> 0"),
            (P300.min_volume - 1.0, "minimum"),
        ],
        ids=["zero", "negative", "below-model-min"],
    )
    def test_invalid_volumes_rejected_before_motion(
        self, tracked_env, volume, match,
    ):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        with pytest.raises(ProtocolExecutionError, match=match):
            transfer(context, source="source", destination="dest", volume_ul=volume)

        context.gantry.move_to_labware.assert_not_called()
        assert pipette.aspirate_volumes == []
        assert _operations(store, state_id) == []

    def test_exactly_model_max_and_min_pass_volume_preflight(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        transfer(
            context, source="source", destination="dest",
            volume_ul=P300.max_volume,
        )
        assert pipette.aspirate_volumes == [pytest.approx(P300.max_volume)]

        context2 = _make_context(
            deck, store, state_id, campaign_id, pipette, step_index=2,
        )
        transfer(
            context2, source="source", destination="dest",
            volume_ul=P300.min_volume,
        )
        assert pipette.aspirate_volumes[-1] == pytest.approx(P300.min_volume)

    def test_capacity_plus_one_splits_rather_than_failing(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        transfer(
            context, source="source", destination="dest",
            volume_ul=P300.max_volume + 1.0,
        )
        assert len(pipette.aspirate_volumes) == 2
        assert sum(pipette.aspirate_volumes) == pytest.approx(P300.max_volume + 1.0)

    def test_source_below_dead_volume_rejected_before_motion(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        store.seed_fluid(state_id, "source", 150.0)
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        # 150 current - 100 dead volume = 50 available; ask for 60.
        with pytest.raises(ProtocolExecutionError, match="dead-volume"):
            transfer(context, source="source", destination="dest", volume_ul=60.0)

        context.gantry.move_to_labware.assert_not_called()
        assert pipette.aspirate_volumes == []
        assert _operations(store, state_id) == []
        assert _volumes(store, state_id)["source"] == pytest.approx(150.0)

    def test_destination_overflow_above_working_volume_rejected_before_motion(
        self, tracked_env,
    ):
        deck, store, state_id, campaign_id = tracked_env
        store.seed_fluid(state_id, "dest", 1400.0)
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        # 1400 + 200 = 1600 > working volume 1500 (capacity 2000 would allow it).
        with pytest.raises(ProtocolExecutionError, match="working volume"):
            transfer(context, source="source", destination="dest", volume_ul=200.0)

        context.gantry.move_to_labware.assert_not_called()
        assert pipette.aspirate_volumes == []
        assert _operations(store, state_id) == []

    def test_well_plate_destination_overflow_also_rejected(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        store.seed_fluid(state_id, "plate.A1", 140.0)
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        # 140 + 20 = 160 > plate working volume 150.
        with pytest.raises(ProtocolExecutionError, match="working volume"):
            transfer(context, source="source", destination="plate.A1", volume_ul=20.0)

        context.gantry.move_to_labware.assert_not_called()


# ─── Per-stroke durable recovery ─────────────────────────────────────────────


class TestPerStrokeRecovery:

    def test_failure_after_stroke_marks_exactly_which_strokes_applied(
        self, tracked_env,
    ):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette(fail_on_dispense_call=2)
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        with pytest.raises(RuntimeError, match="simulated physical dispense"):
            transfer(context, source="source", destination="dest", volume_ul=600.0)

        operations = _operations(store, state_id)
        assert len(operations) == 2  # stroke 3 never began
        assert operations[0]["status"] == "applied"
        assert operations[1]["status"] == "reconciliation_required"
        # Per-stroke detail identifies exactly which stroke is uncertain.
        assert "stroke 2/3" in operations[1]["detail"]

        # Only stroke 1's liquid was committed to durable state.
        volumes = _volumes(store, state_id)
        assert volumes["source"] == pytest.approx(3800.0)
        assert volumes["dest"] == pytest.approx(200.0)

    def test_replay_after_failure_cannot_silently_reapply_liquid(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette(fail_on_dispense_call=2)
        context = _make_context(deck, store, state_id, campaign_id, pipette)
        with pytest.raises(RuntimeError):
            transfer(context, source="source", destination="dest", volume_ul=600.0)

        # Rerun the same step: the uncertain stroke blocks all liquid handling
        # (reconciliation required), and stroke 1 is not re-executed.
        replay_pipette = RecordingPipette()
        replay_context = _make_context(
            deck, store, state_id, campaign_id, replay_pipette,
        )
        with pytest.raises(ProtocolExecutionError, match="reconcil"):
            transfer(
                replay_context, source="source", destination="dest",
                volume_ul=600.0,
            )
        assert replay_pipette.aspirate_volumes == []
        assert _volumes(store, state_id)["source"] == pytest.approx(3800.0)

    def test_replay_after_resolution_skips_applied_strokes_and_finishes(
        self, tracked_env,
    ):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette(fail_on_dispense_call=2)
        context = _make_context(deck, store, state_id, campaign_id, pipette)
        with pytest.raises(RuntimeError):
            transfer(context, source="source", destination="dest", volume_ul=600.0)

        # Operator inspects hardware, confirms stroke 2's liquid did move.
        uncertain_key = _operations(store, state_id)[1]["operation_key"]
        store.resolve_fluid_operation(
            uncertain_key, "applied", detail="operator: stroke 2 completed",
        )
        assert _volumes(store, state_id)["source"] == pytest.approx(3600.0)

        # Replay of the same step executes ONLY the remaining stroke 3.
        replay_pipette = RecordingPipette()
        replay_context = _make_context(
            deck, store, state_id, campaign_id, replay_pipette,
        )
        transfer(
            replay_context, source="source", destination="dest", volume_ul=600.0,
        )

        assert replay_pipette.aspirate_volumes == [pytest.approx(200.0)]
        volumes = _volumes(store, state_id)
        assert volumes["source"] == pytest.approx(3400.0)
        assert volumes["dest"] == pytest.approx(600.0)
        statuses = [op["status"] for op in _operations(store, state_id)]
        assert statuses == ["applied", "applied", "applied"]

    def test_full_replay_of_completed_transfer_is_a_no_op(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)
        transfer(context, source="source", destination="dest", volume_ul=600.0)

        replay_pipette = RecordingPipette()
        replay_context = _make_context(
            deck, store, state_id, campaign_id, replay_pipette,
        )
        transfer(
            replay_context, source="source", destination="dest", volume_ul=600.0,
        )

        assert replay_pipette.aspirate_volumes == []
        assert _volumes(store, state_id)["source"] == pytest.approx(3400.0)
        assert _volumes(store, state_id)["dest"] == pytest.approx(600.0)


# ─── State-derived aspiration height ─────────────────────────────────────────


def _aspirate_engage_z(context) -> float:
    """Z of the first descend-to-action move (the source engage)."""
    first_move = context.gantry.move.call_args_list[0]
    _instrument, (x, y, z) = first_move.args
    return z


def _expected_source_offset(volume_ul: float) -> float:
    surface = volume_ul / SOURCE_AREA_MM2 - SOURCE_HEIGHT_MM
    dead_floor = SOURCE_DEAD_VOLUME_UL / SOURCE_AREA_MM2 - SOURCE_HEIGHT_MM
    clearance_floor = 2.0 - SOURCE_HEIGHT_MM
    return min(0.0, max(surface, dead_floor, clearance_floor))


class TestStateDerivedAspirationHeight:

    def test_derived_z_tracks_liquid_surface_when_height_omitted(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        transfer(context, source="source", destination="dest", volume_ul=100.0)

        expected_z = SOURCE_RIM_Z + _expected_source_offset(4000.0)
        assert _aspirate_engage_z(context) == pytest.approx(expected_z)

    def test_derived_z_descends_as_source_volume_drops_but_never_below_floor(
        self, tracked_env,
    ):
        deck, store, state_id, campaign_id = tracked_env
        engage_zs = []
        for step, seed_volume in enumerate((4000.0, 1000.0, 130.0), start=1):
            store.seed_fluid(state_id, "source", seed_volume)
            pipette = RecordingPipette()
            context = _make_context(
                deck, store, state_id, campaign_id, pipette, step_index=step,
            )
            transfer(
                context, source="source", destination="dest", volume_ul=25.0,
            )
            engage_z = _aspirate_engage_z(context)
            engage_zs.append(engage_z)
            # Always inside the vessel: below the rim, above the bottom.
            assert SOURCE_RIM_Z - SOURCE_HEIGHT_MM <= engage_z <= SOURCE_RIM_Z
            # Never below the dead-volume / bottom-clearance floor.
            floor_z = SOURCE_RIM_Z + max(
                SOURCE_DEAD_VOLUME_UL / SOURCE_AREA_MM2 - SOURCE_HEIGHT_MM,
                2.0 - SOURCE_HEIGHT_MM,
            )
            assert engage_z >= floor_z - 1e-9

        # Tip follows the liquid down monotonically across decreasing volume.
        assert engage_zs[0] > engage_zs[1] >= engage_zs[2]

    def test_explicit_source_height_bypasses_state_derivation(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        transfer(
            context, source="source", destination="dest", volume_ul=100.0,
            source_height=-3.0,
        )
        assert _aspirate_engage_z(context) == pytest.approx(SOURCE_RIM_Z - 3.0)

    def test_explicit_zero_source_height_engages_at_labware_reference(
        self, tracked_env,
    ):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        transfer(
            context, source="source", destination="dest", volume_ul=100.0,
            source_height=0.0,
        )
        assert _aspirate_engage_z(context) == pytest.approx(SOURCE_RIM_Z)

    def test_well_plate_source_falls_back_to_legacy_default(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        store.seed_fluid(state_id, "plate.A1", 150.0)
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        transfer(context, source="plate.A1", destination="dest", volume_ul=50.0)

        # No vial geometry -> engage at the well reference Z (legacy 0.0).
        assert _aspirate_engage_z(context) == pytest.approx(15.0)


# ─── Liquid-class volume correction ──────────────────────────────────────────


class TestLiquidClassCorrectionSemantics:

    # Semantics under test (documented in transfer()'s docstring): the
    # correction adjusts only the DRIVER-COMMANDED volume; durable fluid
    # state always moves the REQUESTED volume. Correction parameters follow
    # PANDA-BEAR's linear form (panda_lib/utilities.py::correction_factor,
    # 0.91 cP bucket: y = 1.01x + 6.23).
    LIQUID_CLASSES = {"aqueous": {"multiplier": 1.01, "offset_ul": 6.23}}

    def test_commanded_volume_reflects_correction_state_reflects_request(
        self, tracked_env,
    ):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette(liquid_classes=self.LIQUID_CLASSES)
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        transfer(
            context, source="source", destination="dest", volume_ul=100.0,
            liquid_class="aqueous",
        )

        assert pipette.aspirate_volumes == [pytest.approx(1.01 * 100.0 + 6.23)]
        assert pipette.dispense_volumes == pipette.aspirate_volumes
        volumes = _volumes(store, state_id)
        assert volumes["source"] == pytest.approx(3900.0)
        assert volumes["dest"] == pytest.approx(100.0)

    def test_correction_participates_in_split_and_never_exceeds_capacity(
        self, tracked_env,
    ):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette(liquid_classes=self.LIQUID_CLASSES)
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        transfer(
            context, source="source", destination="dest", volume_ul=600.0,
            liquid_class="aqueous",
        )

        assert len(pipette.aspirate_volumes) >= 2
        for commanded in pipette.aspirate_volumes:
            assert commanded <= P300.max_volume + 1e-9
        # Driver got corrected volumes (sum > requested)...
        assert sum(pipette.aspirate_volumes) > 600.0
        # ...while durable state moved exactly the requested total.
        volumes = _volumes(store, state_id)
        assert volumes["source"] == pytest.approx(3400.0)
        assert volumes["dest"] == pytest.approx(600.0)

    def test_no_correction_by_default(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette(liquid_classes=self.LIQUID_CLASSES)
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        transfer(context, source="source", destination="dest", volume_ul=100.0)

        assert pipette.aspirate_volumes == [pytest.approx(100.0)]

    def test_unknown_liquid_class_fails_before_motion(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette(liquid_classes=self.LIQUID_CLASSES)
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        with pytest.raises(ProtocolExecutionError, match="liquid_class"):
            transfer(
                context, source="source", destination="dest", volume_ul=100.0,
                liquid_class="glycerol",
            )
        context.gantry.move_to_labware.assert_not_called()
        assert _operations(store, state_id) == []


# ─── Untracked path keeps model-based safety ─────────────────────────────────


class TestUntrackedTransfer:

    def test_untracked_600ul_still_splits_and_respects_driver_capacity(self, tmp_path):
        deck_path = tmp_path / "deck.yaml"
        deck_path.write_text(DECK_YAML, encoding="utf-8")
        deck = load_deck_from_yaml_safe(deck_path)
        pipette = RecordingPipette()
        board = MagicMock()
        board.instruments = {"pipette": pipette}
        context = ProtocolContext(
            gantry=board,
            deck=deck,
            logger=logging.getLogger("test_transfer_liquid_safety"),
        )

        transfer(context, source="source", destination="dest", volume_ul=600.0)

        assert len(pipette.aspirate_volumes) == 3
        for volume in pipette.aspirate_volumes:
            assert volume <= P300.max_volume + 1e-9
        assert sum(pipette.aspirate_volumes) == pytest.approx(600.0)

    def test_untracked_engages_at_labware_reference_by_default(self, tmp_path):
        deck_path = tmp_path / "deck.yaml"
        deck_path.write_text(DECK_YAML, encoding="utf-8")
        deck = load_deck_from_yaml_safe(deck_path)
        pipette = RecordingPipette()
        board = MagicMock()
        board.instruments = {"pipette": pipette}
        context = ProtocolContext(
            gantry=board,
            deck=deck,
            logger=logging.getLogger("test_transfer_liquid_safety"),
        )

        transfer(context, source="source", destination="dest", volume_ul=50.0)

        # Without tracked volume state there is nothing to derive from:
        # legacy behavior (engage at the vial rim reference) is preserved.
        assert _aspirate_engage_z(context) == pytest.approx(SOURCE_RIM_Z)
