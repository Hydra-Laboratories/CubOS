"""Tests for the generic decap/cap protocol commands.

Mirrors ``tests/protocol_engine/test_pipette_commands.py``'s style. A
``FakeGantry`` test double (not MagicMock) traces every ``move``/
``move_to_labware`` call into a shared, ordered list; ``MockCapper``'s own
``actuation_log`` is redirected into that same list, so a single trace
proves the exact approach -> engage -> capture/release -> retract
order across both the motion and the actuation/sensor layers.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest

from cubos.data.data_store import DataStore
from cubos.deck.labware.labware import Coordinate3D
from cubos.deck.loader import load_deck_from_yaml_safe
from cubos.instruments.capper.exceptions import CapperCommandError, CapperTimeoutError
from cubos.instruments.capper.vendors.mock import MockCapper
from cubos.protocol_engine.commands.capper import cap, decap
from cubos.protocol_engine.errors import ProtocolExecutionError
from cubos.protocol_engine.commands.pipette import transfer
from cubos.protocol_engine.runtime import ProtocolContext

SAFE_Z = 100.0


class FakeGantry:
    """Minimal InstrumentedGantry double that traces motion calls in order."""

    def __init__(self, capper, safe_z: float = SAFE_Z):
        self.instruments = {"capper": capper}
        self.safe_z = safe_z
        self.trace: list = []
        self.move_to_labware_raises: BaseException | None = None
        # Raise on the Nth call to move() (1-indexed), or never if None.
        self.move_raises_on_call: int | None = None
        self._move_call_count = 0
        capper.actuation_log = self.trace  # share one ordered trace

    def move_to_labware(self, instrument, position):
        self.trace.append(("approach", position))
        if self.move_to_labware_raises is not None:
            raise self.move_to_labware_raises

    def move(self, instrument, position):
        self._move_call_count += 1
        self.trace.append(("move", position))
        if self.move_raises_on_call == self._move_call_count:
            raise RuntimeError(f"injected mid-motion failure on move #{self._move_call_count}")


def _write_deck(tmp_path, capped: bool = True):
    yaml_text = f"""\
labware:
  reagent:
    type: vial
    name: reagent
    height: 40.0
    diameter: 15.0
    location: {{x: 5.0, y: 5.0, z: 20.0}}
    capacity_ul: 500.0
    working_volume_ul: 400.0
    capped: {"true" if capped else "false"}

  waste:
    type: vial
    name: waste
    height: 40.0
    diameter: 15.0
    location: {{x: 20.0, y: 5.0, z: 20.0}}
    capacity_ul: 500.0
    working_volume_ul: 400.0
    capped: false

  untracked:
    type: vial
    name: untracked
    height: 40.0
    diameter: 15.0
    location: {{x: 35.0, y: 5.0, z: 20.0}}
    capacity_ul: 500.0
    working_volume_ul: 400.0
"""
    path = tmp_path / "deck.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return path, load_deck_from_yaml_safe(path)


def _make_capper(**overrides):
    kwargs = dict(engage_depth_mm=-8.0, capture_settle_s=0.0)
    kwargs.update(overrides)
    return MockCapper(**kwargs)


def _untracked_context(capper=None, deck=None) -> tuple[ProtocolContext, FakeGantry]:
    capper = capper or _make_capper()
    gantry = FakeGantry(capper)
    if deck is None:
        deck = _SimpleDeck()
    return (
        ProtocolContext(gantry=gantry, deck=deck, logger=logging.getLogger("test_capper")),
        gantry,
    )


class _SimpleDeck:
    """Deck double for untracked-mode tests: only resolve_coordinate is used."""

    def resolve_coordinate(self, target):
        return Coordinate3D(x=10.0, y=20.0, z=30.0)


# ─── Happy-path sequencing ──────────────────────────────────────────────────


class TestDecapSequencing:
    def test_exact_action_order(self):
        ctx, gantry = _untracked_context()
        decap(ctx, "capper", "vial_1")
        capper = gantry.instruments["capper"]
        engage_z = 30.0 + capper.engage_depth_mm
        assert gantry.trace == [
            ("approach", Coordinate3D(x=10.0, y=20.0, z=30.0)),
            ("move", (10.0, 20.0, engage_z)),
            "capture_cap",
            ("move", (10.0, 20.0, SAFE_Z)),
            "read_cap_present",
        ]

    def test_cap_present_after_decap(self):
        ctx, gantry = _untracked_context()
        decap(ctx, "capper", "vial_1")
        assert gantry.instruments["capper"].read_cap_present() is True


class TestCapSequencing:
    def test_exact_action_order(self):
        capper = _make_capper()
        capper.capture_cap()  # start "capped" so release has something to confirm
        ctx, gantry = _untracked_context(capper=capper)
        gantry.trace.clear()  # drop the priming capture_cap call above
        cap(ctx, "capper", "vial_1")
        engage_z = 30.0 + capper.engage_depth_mm
        assert gantry.trace == [
            ("approach", Coordinate3D(x=10.0, y=20.0, z=30.0)),
            ("move", (10.0, 20.0, engage_z)),
            "release_cap",
            ("move", (10.0, 20.0, SAFE_Z)),
            "read_cap_present",
        ]

    def test_cap_absent_after_cap(self):
        capper = _make_capper()
        capper.capture_cap()
        ctx, gantry = _untracked_context(capper=capper)
        cap(ctx, "capper", "vial_1")
        assert gantry.instruments["capper"].read_cap_present() is False

    def test_release_confirm_reads_after_retract_with_beam_realistic_sensor(self):
        """Regression: the real line-break sensor reads cap-present at engage
        depth even after a successful release -- the released cap sits on the
        vial inside the beam until the tool lifts clear. Confirming before the
        retract made every hardware `cap` fail its sensor check."""
        capper = _make_capper(capture_retries=0)
        capper.capture_cap()  # start holding a cap
        engage_z = 30.0 + capper.engage_depth_mm
        current_z = {"z": SAFE_Z}

        class ZTrackingGantry(FakeGantry):
            def move(self, instrument, position):
                current_z["z"] = position[2]
                super().move(instrument, position)

        def beam_read():
            capper.actuation_log.append("read_cap_present")
            if current_z["z"] == engage_z:
                return True  # cap in the beam: held, or sitting on the vial
            return capper._cap_present

        capper.read_cap_present = beam_read
        gantry = ZTrackingGantry(capper)
        ctx = ProtocolContext(
            gantry=gantry, deck=_SimpleDeck(), logger=logging.getLogger("test_capper"),
        )

        cap(ctx, "capper", "vial_1")  # must not raise

        retract_index = gantry.trace.index(("move", (10.0, 20.0, SAFE_Z)))
        read_index = gantry.trace.index("read_cap_present")
        assert read_index > retract_index


# ─── Retry / fail-closed on sensor faults ──────────────────────────────────


class TestSensorConfirmationRetries:
    def test_recovers_after_transient_sensor_read_failure(self):
        capper = _make_capper(capture_retries=2)
        call_count = {"n": 0}
        original_read = capper.read_cap_present

        def flaky_read():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise CapperTimeoutError("no response")
            return original_read()

        capper.read_cap_present = flaky_read
        ctx, gantry = _untracked_context(capper=capper)
        decap(ctx, "capper", "vial_1")  # should not raise
        assert call_count["n"] == 2

    def test_exhausts_retries_then_fails_closed(self):
        capper = _make_capper(capture_retries=1)
        capper.read_cap_present = lambda: (_ for _ in ()).throw(
            CapperTimeoutError("still no response")
        )
        ctx, gantry = _untracked_context(capper=capper)
        with pytest.raises(ProtocolExecutionError, match="reconciliation"):
            decap(ctx, "capper", "vial_1")

    def test_contradictory_sensor_reading_fails_closed(self):
        # Actuate capture but the sensor insists no cap is present -- a
        # contradictory reading, not a communication error.
        capper = _make_capper(capture_retries=0)
        capper.read_cap_present = lambda: False
        ctx, gantry = _untracked_context(capper=capper)
        with pytest.raises(ProtocolExecutionError, match="reconciliation"):
            decap(ctx, "capper", "vial_1")

    def test_serial_timeout_triggers_safe_retract(self):
        capper = _make_capper(capture_retries=0)
        capper.read_cap_present = lambda: (_ for _ in ()).throw(
            CapperTimeoutError("timeout")
        )
        ctx, gantry = _untracked_context(capper=capper)
        with pytest.raises(ProtocolExecutionError):
            decap(ctx, "capper", "vial_1")
        # Last recorded motion must be a retract to safe_z at the vial XY.
        last_move = [entry for entry in gantry.trace if isinstance(entry, tuple)][-1]
        assert last_move == ("move", (10.0, 20.0, SAFE_Z))

    def test_command_error_during_actuation_fails_closed(self):
        capper = _make_capper(capture_retries=0)
        capper.capture_cap = lambda: (_ for _ in ()).throw(
            CapperCommandError("jam")
        )
        ctx, gantry = _untracked_context(capper=capper)
        with pytest.raises(ProtocolExecutionError, match="reconciliation"):
            decap(ctx, "capper", "vial_1")


# ─── Mid-motion failures: safe retract on every stage ──────────────────────


class TestSafeRetractOnMidMotionFailure:
    def test_approach_failure_still_attempts_retract(self):
        capper = _make_capper()
        ctx, gantry = _untracked_context(capper=capper)
        gantry.move_to_labware_raises = RuntimeError("approach comm failure")
        with pytest.raises(ProtocolExecutionError):
            decap(ctx, "capper", "vial_1")
        assert any(
            entry == ("move", (10.0, 20.0, SAFE_Z)) for entry in gantry.trace
        )

    def test_engage_failure_still_attempts_retract(self):
        capper = _make_capper()
        ctx, gantry = _untracked_context(capper=capper)
        gantry.move_raises_on_call = 1  # the engage move
        with pytest.raises(ProtocolExecutionError):
            decap(ctx, "capper", "vial_1")
        # capture_cap/read_cap_present never ran; a safe-retract move was
        # still attempted afterward.
        assert "capture_cap" not in gantry.trace
        assert gantry.trace[-1] == ("move", (10.0, 20.0, SAFE_Z))

    def test_retract_failure_before_confirm_fails_closed(self):
        # The retract precedes the sensor confirm, so a retract failure
        # means the operation was never confirmed -- generic fail-closed path.
        capper = _make_capper()
        ctx, gantry = _untracked_context(capper=capper)
        gantry.move_raises_on_call = 2  # the retract move, after actuation
        with pytest.raises(ProtocolExecutionError, match="decap failed"):
            decap(ctx, "capper", "vial_1")
        assert "capture_cap" in gantry.trace
        assert "read_cap_present" not in gantry.trace

    def test_no_lateral_move_after_confirmed_retract(self):
        ctx, gantry = _untracked_context()
        decap(ctx, "capper", "vial_1")
        moves = [pos for kind, pos in (t for t in gantry.trace if isinstance(t, tuple)) if kind == "move"]
        assert all(pos[:2] == (10.0, 20.0) for pos in moves)
        assert moves[-1] == (10.0, 20.0, SAFE_Z)

    def test_safe_retract_itself_failing_does_not_mask_original_error(self):
        capper = _make_capper()
        ctx, gantry = _untracked_context(capper=capper)
        gantry.move_raises_on_call = 1  # engage fails
        original_move = gantry.move

        def always_raise(instrument, position):
            gantry._move_call_count += 1
            gantry.trace.append(("move", position))
            raise RuntimeError("gantry unresponsive")

        gantry.move = always_raise
        with pytest.raises(ProtocolExecutionError, match="engage comm|gantry unresponsive|failed"):
            decap(ctx, "capper", "vial_1")


class TestUnknownInstrument:
    def test_missing_instrument_raises(self):
        ctx, _gantry = _untracked_context()
        with pytest.raises(ProtocolExecutionError, match="No instrument"):
            decap(ctx, "nonexistent", "vial_1")

    def test_wrong_instrument_type_raises(self):
        gantry = FakeGantry(_make_capper())
        gantry.instruments["not_a_capper"] = object()
        ctx = ProtocolContext(gantry=gantry, deck=_SimpleDeck(), logger=logging.getLogger("t"))
        with pytest.raises(ProtocolExecutionError, match="CapperInstrument"):
            decap(ctx, "not_a_capper", "vial_1")


# ─── Durable tracking: journaling, idempotency, reconciliation ─────────────


def _tracked_context(tmp_path, capped: bool = True):
    deck_path, deck = _write_deck(tmp_path, capped=capped)
    store = DataStore(":memory:")
    fluid_state_id = store.create_fluid_state(deck_path, deck, label="capper test")
    campaign_id = store.create_campaign("capper test", fluid_state_id=fluid_state_id)
    capper = _make_capper()
    gantry = FakeGantry(capper)
    ctx = ProtocolContext(
        gantry=gantry,
        deck=deck,
        logger=logging.getLogger("test_capper_tracked"),
        data_store=store,
        campaign_id=campaign_id,
        fluid_state_id=fluid_state_id,
    )
    return ctx, gantry, store, fluid_state_id


class TestTrackedDecap:
    def test_decap_flips_durable_state_to_uncapped(self, tmp_path):
        ctx, gantry, store, fluid_state_id = _tracked_context(tmp_path, capped=True)
        decap(ctx, "capper", "reagent")
        assert store.get_cap_state(fluid_state_id, "reagent", "") == "uncapped"

    def test_decap_rejects_already_uncapped_vial(self, tmp_path):
        ctx, gantry, store, fluid_state_id = _tracked_context(tmp_path, capped=False)
        with pytest.raises(ProtocolExecutionError):
            decap(ctx, "capper", "reagent")

    def test_replaying_step_index_is_idempotent(self, tmp_path):
        ctx, gantry, store, fluid_state_id = _tracked_context(tmp_path, capped=True)
        ctx.active_step_index = 0
        ctx.active_step_command = "decap"
        decap(ctx, "capper", "reagent")
        call_count_after_first = len(gantry.trace)
        decap(ctx, "capper", "reagent")  # same step index -> same operation_key
        assert len(gantry.trace) == call_count_after_first  # no second physical action
        assert store.get_cap_state(fluid_state_id, "reagent", "") == "uncapped"

    def test_sensor_fault_marks_reconciliation_required(self, tmp_path):
        ctx, gantry, store, fluid_state_id = _tracked_context(tmp_path, capped=True)
        capper = gantry.instruments["capper"]
        capper.read_cap_present = lambda: (_ for _ in ()).throw(
            CapperTimeoutError("comm failure")
        )
        with pytest.raises(ProtocolExecutionError):
            decap(ctx, "capper", "reagent")
        assert store.get_cap_state(fluid_state_id, "reagent", "") == "reconciliation_required"

    def test_decap_vial_with_no_capped_declaration_raises_clear_error(self, tmp_path):
        ctx, gantry, store, fluid_state_id = _tracked_context(tmp_path, capped=True)
        with pytest.raises(ProtocolExecutionError, match="no durable cap state"):
            decap(ctx, "capper", "untracked")


# ─── require_uncapped preflight on transfer ────────────────────────────────


class TestRequireUncappedPreflight:
    def test_transfer_blocked_when_source_is_capped(self, tmp_path):
        ctx, gantry, store, fluid_state_id = _tracked_context(tmp_path, capped=True)
        store.seed_fluid(fluid_state_id, "reagent", 300.0)
        pipette = _FakePipette()
        ctx.gantry.instruments["pipette"] = pipette
        with pytest.raises(ProtocolExecutionError, match="uncapped"):
            transfer(
                ctx, source="reagent", destination="waste", volume_ul=10.0,
                require_uncapped=["reagent"],
            )
        # No aspiration may have started.
        assert pipette.aspirate_calls == []

    def test_transfer_allowed_after_decap(self, tmp_path):
        ctx, gantry, store, fluid_state_id = _tracked_context(tmp_path, capped=True)
        store.seed_fluid(fluid_state_id, "reagent", 300.0)
        decap(ctx, "capper", "reagent")
        pipette = _FakePipette()
        ctx.gantry.instruments["pipette"] = pipette
        transfer(
            ctx, source="reagent", destination="waste", volume_ul=10.0,
            require_uncapped=["reagent"],
        )
        assert pipette.aspirate_calls == [10.0]

    def test_untracked_vial_is_not_constrained(self, tmp_path):
        ctx, gantry, store, fluid_state_id = _tracked_context(tmp_path, capped=True)
        store.seed_fluid(fluid_state_id, "waste", 0.0)
        store.seed_fluid(fluid_state_id, "reagent", 300.0)
        decap(ctx, "capper", "reagent")
        pipette = _FakePipette()
        ctx.gantry.instruments["pipette"] = pipette
        # 'waste' has no capper tracking requirement being asserted here --
        # require_uncapped names 'reagent' only.
        transfer(
            ctx, source="reagent", destination="waste", volume_ul=10.0,
            require_uncapped=["reagent"],
        )
        assert pipette.aspirate_calls == [10.0]

    def test_omitted_require_uncapped_does_not_check_anything(self, tmp_path):
        ctx, gantry, store, fluid_state_id = _tracked_context(tmp_path, capped=True)
        store.seed_fluid(fluid_state_id, "reagent", 300.0)
        pipette = _FakePipette()
        ctx.gantry.instruments["pipette"] = pipette
        # reagent is still capped, but require_uncapped was not asked for.
        transfer(ctx, source="reagent", destination="waste", volume_ul=10.0)
        assert pipette.aspirate_calls == [10.0]


class _FakePipette:
    """Minimal pipette double for transfer() preflight tests."""

    def __init__(self):
        self.aspirate_calls: list[float] = []
        self.effective_depth = 0.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.depth = 0.0
        self.name = "pipette"

    def aspirate(self, volume_ul, speed=50.0):
        self.aspirate_calls.append(volume_ul)
        return type("R", (), {"success": True, "volume_ul": volume_ul})()

    def dispense(self, volume_ul, speed=50.0):
        return type("R", (), {"success": True, "volume_ul": volume_ul})()

    def blowout(self, speed=50.0):
        return type("R", (), {"success": True})()
