"""End-to-end Feature-05 tests for the compound liquid protocol commands.

Mirrors ``test_transfer_liquid_safety.py``'s harness: real ``Deck``, real
``DataStore``/fluid-state journal, only the gantry/pipette driver boundary
is a recording double. Exercises ``rinse_well``, ``flush_pipette``,
``purge_pipette``, and ``clear_well`` -- automatic vs. explicit container
selection, the durable substep-key scheme, crash-and-resume idempotency,
and composition/dilution math consistency with ``fluid_state``.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from cubos.data.data_store import DataStore
from cubos.deck.loader import load_deck_from_yaml_safe
from cubos.instruments.pipette.models import PIPETTE_MODELS
from cubos.instruments.pipette.vendors.opentrons import OpentronsPipette
from cubos.protocol_engine.commands.pipette import (
    clear_well,
    flush_pipette,
    purge_pipette,
    rinse_well,
)
from cubos.protocol_engine.errors import ProtocolExecutionError
from cubos.protocol_engine.runtime import ProtocolContext

P300 = PIPETTE_MODELS["p300_single_gen2"]  # min 20.0 uL, max 200.0 uL

DECK_YAML = """\
labware:
  stock:
    type: vial
    name: stock
    model_name: stock_vial
    role: stock
    solution: water
    height: 100.0
    diameter: 20.0
    location: {x: 5.0, y: 5.0, z: 50.0}
    capacity_ul: 5000.0
    working_volume_ul: 4500.0
    dead_volume_ul: 100.0

  waste:
    type: vial
    name: waste
    model_name: waste_vial
    role: waste
    height: 80.0
    diameter: 25.0
    location: {x: 40.0, y: 5.0, z: 45.0}
    capacity_ul: 5000.0
    working_volume_ul: 4500.0

  restricted_waste:
    type: vial
    name: restricted_waste
    model_name: waste_vial
    role: waste
    allowed_solutions: [ethanol]
    height: 80.0
    diameter: 25.0
    location: {x: 60.0, y: 5.0, z: 45.0}
    capacity_ul: 5000.0
    working_volume_ul: 4500.0

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
      a1: {x: 70.0, y: 20.0, z: 15.0}
      a2: {x: 79.0, y: 20.0, z: 15.0}
    x_offset: 9.0
    y_offset: 9.0
    capacity_ul: 200.0
    working_volume_ul: 150.0
"""


class RecordingPipette(OpentronsPipette):
    """Offline P300 that records every driver-boundary volume it is commanded.

    ``fail_on_call`` (1-based, counting *every* aspirate+dispense call in
    call order) makes that N-th call raise, to simulate a physical failure
    at an arbitrary point in a multi-substep compound command.
    """

    def __init__(self, *, fail_on_call: int | None = None, **kwargs):
        kwargs.setdefault("pipette_model", "p300_single_gen2")
        super().__init__(offline=True, **kwargs)
        self.aspirate_volumes: list[float] = []
        self.dispense_volumes: list[float] = []
        self.calls: list[tuple[str, float]] = []
        self._fail_on_call = fail_on_call
        self._call_count = 0

    def _maybe_fail(self, kind: str) -> None:
        self._call_count += 1
        if self._call_count == self._fail_on_call:
            raise RuntimeError(f"simulated physical {kind} failure (call {self._call_count})")

    def aspirate(self, volume_ul: float, speed: float = 50.0):
        self._maybe_fail("aspirate")
        self.aspirate_volumes.append(volume_ul)
        self.calls.append(("aspirate", volume_ul))
        return super().aspirate(volume_ul, speed)

    def dispense(self, volume_ul: float, speed: float = 50.0):
        self._maybe_fail("dispense")
        self.dispense_volumes.append(volume_ul)
        self.calls.append(("dispense", volume_ul))
        return super().dispense(volume_ul, speed)


@pytest.fixture()
def tracked_env(tmp_path):
    deck_path = tmp_path / "deck.yaml"
    deck_path.write_text(DECK_YAML, encoding="utf-8")
    deck = load_deck_from_yaml_safe(deck_path)
    store = DataStore(db_path=":memory:")
    state_id = store.create_fluid_state(
        deck_path, deck,
        initial_fluids={"stock": {"volume_ul": 4000.0, "composition": {"water": 4000.0}}},
    )
    campaign_id = store.create_campaign("feature-05", fluid_state_id=state_id)
    yield deck, store, state_id, campaign_id
    store.close()


def _make_context(deck, store, state_id, campaign_id, pipette, *, step_index=1, command="rinse_well"):
    board = MagicMock()
    board.instruments = {"pipette": pipette}
    context = ProtocolContext(
        gantry=board, deck=deck, data_store=store, campaign_id=campaign_id,
        fluid_state_id=state_id, logger=logging.getLogger("test_compound_liquid_commands"),
    )
    context.active_step_index = step_index
    context.active_step_command = command
    return context


def _volumes(store, state_id) -> dict[str, float]:
    snapshot = store.get_fluid_snapshot(state_id)
    return {
        (
            f"{c['labware_key']}.{c['location_id']}" if c["location_id"] else c["labware_key"]
        ): c["current_volume_ul"]
        for c in snapshot["containers"]
    }


def _composition(store, state_id, key: str) -> dict[str, float]:
    snapshot = store.get_fluid_snapshot(state_id)
    for c in snapshot["containers"]:
        target = f"{c['labware_key']}.{c['location_id']}" if c["location_id"] else c["labware_key"]
        if target == key:
            return c["composition"]
    raise KeyError(key)


def _operations(store, state_id):
    return store.get_fluid_snapshot(state_id)["operations"]


# ─── Representative 3-rinse + 3-flush trace ────────────────────────────────


class TestRinseAndFlushTrace:

    def test_exact_sequence_and_final_state(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        rinse_well(
            context, well="plate.A1", volume_ul=50.0, cycles=3, solution="water",
        )
        flush_pipette(context, volume_ul=50.0, cycles=3, solution="water")

        # 3 cycles x (fill + remove) + 3 flush transfers = 9 aspirate/dispense pairs.
        assert pipette.aspirate_volumes == [pytest.approx(50.0)] * 9
        assert pipette.dispense_volumes == [pytest.approx(50.0)] * 9

        operations = _operations(store, state_id)
        assert len(operations) == 9
        assert [op["status"] for op in operations] == ["applied"] * 9

        expected_keys = (
            "rinse:cycle0:fill", "rinse:cycle0:remove",
            "rinse:cycle1:fill", "rinse:cycle1:remove",
            "rinse:cycle2:fill", "rinse:cycle2:remove",
            "flush:cycle0", "flush:cycle1", "flush:cycle2",
        )
        for op, expected_key in zip(operations, expected_keys):
            assert expected_key in op["operation_key"], (expected_key, op["operation_key"])

        expected_sources_destinations = (
            ("stock", "plate.A1"), ("plate.A1", "waste"),
            ("stock", "plate.A1"), ("plate.A1", "waste"),
            ("stock", "plate.A1"), ("plate.A1", "waste"),
            ("stock", "waste"), ("stock", "waste"), ("stock", "waste"),
        )
        for op, (source, destination) in zip(operations, expected_sources_destinations):
            assert op["source"] == source
            assert op["destination"] == destination

        volumes = _volumes(store, state_id)
        assert volumes["stock"] == pytest.approx(4000.0 - 6 * 50.0)
        assert volumes["waste"] == pytest.approx(6 * 50.0)
        assert volumes["plate.A1"] == pytest.approx(0.0)

        assert _composition(store, state_id, "waste") == {"water": pytest.approx(300.0)}

    def test_explicit_containers_bypass_selection(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        rinse_well(
            context, well="plate.A1", volume_ul=25.0, cycles=1,
            source="stock", waste="waste",
        )
        volumes = _volumes(store, state_id)
        assert volumes["waste"] == pytest.approx(25.0)


# ─── Restart mid-workflow: idempotent resume ───────────────────────────────


class TestResumeMidWorkflow:

    def test_failure_mid_rinse_marks_exactly_the_failed_substep(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        # fail on the 4th aspirate/dispense call: cycle0 fill (2 calls) +
        # cycle0 remove aspirate (call 3) succeed, cycle0 remove dispense
        # (call 4) fails.
        pipette = RecordingPipette(fail_on_call=4)
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        with pytest.raises(RuntimeError, match="simulated physical dispense"):
            rinse_well(context, well="plate.A1", volume_ul=50.0, cycles=3, solution="water")

        operations = _operations(store, state_id)
        assert len(operations) == 2  # cycle0 fill applied, cycle0 remove reconciliation_required
        assert operations[0]["status"] == "applied"
        assert "rinse:cycle0:fill" in operations[0]["operation_key"]
        assert operations[1]["status"] == "reconciliation_required"
        assert "rinse:cycle0:remove" in operations[1]["operation_key"]

    def test_replay_skips_applied_substeps_and_finishes(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette(fail_on_call=4)
        context = _make_context(deck, store, state_id, campaign_id, pipette)
        with pytest.raises(RuntimeError):
            rinse_well(context, well="plate.A1", volume_ul=50.0, cycles=3, solution="water")

        # Operator confirms the uncertain remove did complete physically.
        uncertain_key = _operations(store, state_id)[1]["operation_key"]
        store.resolve_fluid_operation(uncertain_key, "applied", detail="operator confirmed")

        replay_pipette = RecordingPipette()
        replay_context = _make_context(
            deck, store, state_id, campaign_id, replay_pipette, step_index=1,
        )
        # Same step index/command -> same deterministic operation keys ->
        # cycle0 fill/remove are skipped (no-op), only cycles 1-2 execute.
        rinse_well(
            replay_context, well="plate.A1", volume_ul=50.0, cycles=3, solution="water",
        )

        assert len(replay_pipette.aspirate_volumes) == 4  # cycles 1 & 2: fill+remove each
        operations = _operations(store, state_id)
        assert len(operations) == 6
        assert [op["status"] for op in operations] == ["applied"] * 6

        volumes = _volumes(store, state_id)
        assert volumes["stock"] == pytest.approx(4000.0 - 3 * 50.0)
        assert volumes["waste"] == pytest.approx(3 * 50.0)
        assert volumes["plate.A1"] == pytest.approx(0.0)

    def test_replay_without_resolution_blocks_all_further_liquid_handling(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette(fail_on_call=4)
        context = _make_context(deck, store, state_id, campaign_id, pipette)
        with pytest.raises(RuntimeError):
            rinse_well(context, well="plate.A1", volume_ul=50.0, cycles=3, solution="water")

        replay_pipette = RecordingPipette()
        replay_context = _make_context(
            deck, store, state_id, campaign_id, replay_pipette, step_index=1,
        )
        with pytest.raises(ProtocolExecutionError, match="reconcil"):
            rinse_well(
                replay_context, well="plate.A1", volume_ul=50.0, cycles=3, solution="water",
            )
        assert replay_pipette.aspirate_volumes == []

    def test_full_replay_of_completed_rinse_is_a_no_op(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)
        rinse_well(context, well="plate.A1", volume_ul=25.0, cycles=2, solution="water")

        replay_pipette = RecordingPipette()
        replay_context = _make_context(
            deck, store, state_id, campaign_id, replay_pipette, step_index=1,
        )
        rinse_well(
            replay_context, well="plate.A1", volume_ul=25.0, cycles=2, solution="water",
        )
        assert replay_pipette.aspirate_volumes == []
        assert _volumes(store, state_id)["waste"] == pytest.approx(50.0)

    @pytest.mark.parametrize("fail_on_call", [1, 2, 3, 4], ids=[
        "fill-aspirate", "fill-dispense", "remove-aspirate", "remove-dispense",
    ])
    def test_failure_at_every_substep_boundary_yields_correct_journal_states(
        self, tracked_env, fail_on_call,
    ):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette(fail_on_call=fail_on_call)
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        with pytest.raises(RuntimeError):
            rinse_well(context, well="plate.A1", volume_ul=50.0, cycles=1, solution="water")

        operations = _operations(store, state_id)
        # Exactly one operation is left non-terminal (started -> marked
        # reconciliation_required); everything before it applied cleanly.
        statuses = [op["status"] for op in operations]
        assert statuses.count("reconciliation_required") == 1
        assert statuses[:-1] == ["applied"] * (len(statuses) - 1)
        assert statuses[-1] == "reconciliation_required"


# ─── Mix substep ────────────────────────────────────────────────────────────


class TestRinseWithMix:

    def test_mix_substep_runs_between_fill_and_remove(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        rinse_well(
            context, well="plate.A1", volume_ul=50.0, cycles=1, solution="water",
            mix_repetitions=2,
        )
        # fill + two mix cycles (two aspirates each) + remove.
        assert len(pipette.aspirate_volumes) == 6

        operations = _operations(store, state_id)
        mix_ops = [op for op in operations if op["operation_type"] == "mix"]
        assert len(mix_ops) == 1
        assert "rinse:cycle0:mix" in mix_ops[0]["operation_key"]
        assert mix_ops[0]["parameters"]["repetitions"] == 2

    def test_no_mix_operation_journaled_when_repetitions_zero(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        rinse_well(context, well="plate.A1", volume_ul=50.0, cycles=1, solution="water")
        operations = _operations(store, state_id)
        assert not any(op["operation_type"] == "mix" for op in operations)


# ─── Composition / dilution math consistency ───────────────────────────────


class TestDilutionMath:

    def test_rinse_dilutes_a_residual_composition_consistently_with_fluid_state(
        self, tracked_env,
    ):
        deck, store, state_id, campaign_id = tracked_env
        residual_ul = 20.0
        store.seed_fluid(
            state_id, "plate.A1", residual_ul, composition={"ethanol": residual_ul},
        )
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        volume_ul = 50.0
        rinse_well(context, well="plate.A1", volume_ul=volume_ul, cycles=1, solution="water")

        # Fill: well holds residual_ul ethanol + volume_ul water = R+V uL.
        # Remove takes volume_ul uL back out proportionally (fluid_state's
        # `_proportional_composition`), leaving exactly residual_ul uL behind
        # with composition diluted by the wash: ethanol = R^2/(R+V),
        # water = R*V/(R+V).
        total = residual_ul + volume_ul
        expected_ethanol = residual_ul * residual_ul / total
        expected_water = residual_ul * volume_ul / total

        volumes = _volumes(store, state_id)
        assert volumes["plate.A1"] == pytest.approx(residual_ul)
        composition = _composition(store, state_id, "plate.A1")
        assert composition["ethanol"] == pytest.approx(expected_ethanol, rel=1e-6)
        assert composition["water"] == pytest.approx(expected_water, rel=1e-6)

    def test_repeated_rinse_cycles_progressively_wash_out_residual(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        residual_ul = 20.0
        store.seed_fluid(
            state_id, "plate.A1", residual_ul, composition={"ethanol": residual_ul},
        )
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        rinse_well(context, well="plate.A1", volume_ul=50.0, cycles=3, solution="water")

        composition = _composition(store, state_id, "plate.A1")
        # After 3 wash cycles, residual ethanol should be nearly gone.
        assert composition.get("ethanol", 0.0) < 1.0
        assert _volumes(store, state_id)["plate.A1"] == pytest.approx(residual_ul)


# ─── purge_pipette ──────────────────────────────────────────────────────────


class TestPurgePipette:

    def test_purge_is_a_single_source_to_waste_transfer(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette, command="purge_pipette")

        purge_pipette(context, volume_ul=75.0, source="stock", waste="waste")

        assert pipette.aspirate_volumes == [pytest.approx(75.0)]
        operations = _operations(store, state_id)
        assert len(operations) == 1
        assert "purge" in operations[0]["operation_key"]
        assert operations[0]["source"] == "stock"
        assert operations[0]["destination"] == "waste"

    def test_purge_with_automatic_selection(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette, command="purge_pipette")

        purge_pipette(context, volume_ul=75.0, solution="water")
        assert _volumes(store, state_id)["waste"] == pytest.approx(75.0)


# ─── clear_well ─────────────────────────────────────────────────────────────


class TestClearWell:

    def test_clears_full_tracked_volume_to_waste(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        store.seed_fluid(state_id, "plate.A1", 60.0, composition={"water": 60.0})
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette, command="clear_well")

        clear_well(context, well="plate.A1", waste="waste")

        assert pipette.aspirate_volumes == [pytest.approx(60.0)]
        assert _volumes(store, state_id)["plate.A1"] == pytest.approx(0.0)
        assert _volumes(store, state_id)["waste"] == pytest.approx(60.0)

    def test_clears_down_to_a_target_volume(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        store.seed_fluid(state_id, "plate.A1", 60.0, composition={"water": 60.0})
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette, command="clear_well")

        clear_well(context, well="plate.A1", target_volume_ul=10.0, waste="waste")

        assert pipette.aspirate_volumes == [pytest.approx(50.0)]
        assert _volumes(store, state_id)["plate.A1"] == pytest.approx(10.0)

    def test_no_op_when_already_at_or_below_target(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette, command="clear_well")

        clear_well(context, well="plate.A1", waste="waste")  # already 0 uL
        assert pipette.aspirate_volumes == []
        assert _operations(store, state_id) == []

    def test_explicit_volume_ul_overrides_tracked_state(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        store.seed_fluid(state_id, "plate.A1", 60.0, composition={"water": 60.0})
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette, command="clear_well")

        clear_well(context, well="plate.A1", volume_ul=30.0, waste="waste")
        assert pipette.aspirate_volumes == [pytest.approx(30.0)]
        assert _volumes(store, state_id)["plate.A1"] == pytest.approx(30.0)

    def test_requires_explicit_volume_when_untracked(self, tmp_path):
        deck_path = tmp_path / "deck.yaml"
        deck_path.write_text(DECK_YAML, encoding="utf-8")
        deck = load_deck_from_yaml_safe(deck_path)
        pipette = RecordingPipette()
        board = MagicMock()
        board.instruments = {"pipette": pipette}
        context = ProtocolContext(
            gantry=board, deck=deck, logger=logging.getLogger("test_compound_liquid_commands"),
        )
        with pytest.raises(ProtocolExecutionError, match="explicit"):
            clear_well(context, well="plate.A1", waste="waste")


# ─── Selection failure surfaces as ProtocolExecutionError ──────────────────


class TestSelectionFailuresSurfaceCleanly:

    def test_no_matching_stock_solution_raises_before_motion(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        with pytest.raises(ProtocolExecutionError, match="stock selection failed"):
            rinse_well(context, well="plate.A1", volume_ul=50.0, cycles=1, solution="acetone")
        assert pipette.aspirate_volumes == []

    def test_waste_compatibility_policy_excludes_incompatible_waste(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        # "waste" (accept-all) still exists on the deck, so selection
        # succeeds and lands in the generic waste bin rather than the
        # ethanol-only restricted_waste vial.
        rinse_well(context, well="plate.A1", volume_ul=25.0, cycles=1, solution="water")
        volumes = _volumes(store, state_id)
        assert volumes["waste"] == pytest.approx(25.0)
        assert volumes["restricted_waste"] == pytest.approx(0.0)

    def test_source_and_solution_mutual_exclusivity(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        with pytest.raises(ProtocolExecutionError, match="exactly one"):
            rinse_well(
                context, well="plate.A1", volume_ul=25.0, cycles=1,
                source="stock", solution="water",
            )
        with pytest.raises(ProtocolExecutionError, match="exactly one"):
            rinse_well(context, well="plate.A1", volume_ul=25.0, cycles=1)

    def test_dead_volume_reserve_blocks_selection(self, tracked_env):
        deck, store, state_id, campaign_id = tracked_env
        store.seed_fluid(state_id, "stock", 150.0, composition={"water": 150.0})
        pipette = RecordingPipette()
        context = _make_context(deck, store, state_id, campaign_id, pipette)

        # 150 current - 100 dead volume = 50 available; ask for 60.
        with pytest.raises(ProtocolExecutionError, match="stock selection failed"):
            rinse_well(context, well="plate.A1", volume_ul=60.0, cycles=1, solution="water")
        assert pipette.aspirate_volumes == []
