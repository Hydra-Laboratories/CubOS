"""Pure unit tests for Feature-04 stroke planning, preflight, and height math.

Covers ``cubos.protocol_engine.commands._liquid_transfer`` in isolation from
the ``transfer`` protocol command (integration coverage for the full command
lives in ``test_transfer_liquid_safety.py``).
"""

from __future__ import annotations

import math

import pytest

from cubos.deck.labware.labware import Coordinate3D
from cubos.deck.labware.vial import Vial
from cubos.instruments.pipette.models import PIPETTE_MODELS
from cubos.protocol_engine.commands._liquid_transfer import (
    LiquidTransferPreflightError,
    derive_liquid_relative_height,
    pipette_capacity,
    plan_strokes,
    validate_dead_volume,
    validate_destination_overflow,
)

P300 = PIPETTE_MODELS["p300_single_gen2"]  # min_volume=20.0, max_volume=200.0


def _vial(
    *,
    height: float = 100.0,
    diameter: float = 20.0,
    capacity_ul: float = 5000.0,
    working_volume_ul: float = 4500.0,
    dead_volume_ul: float = 0.0,
) -> Vial:
    return Vial(
        name="v",
        height=height,
        diameter=diameter,
        location=Coordinate3D(x=0.0, y=0.0, z=50.0),
        capacity_ul=capacity_ul,
        working_volume_ul=working_volume_ul,
        dead_volume_ul=dead_volume_ul,
    )


# ─── pipette_capacity ────────────────────────────────────────────────────────


class TestPipetteCapacity:

    def test_real_config_returned(self):
        class FakePipette:
            config = P300

        assert pipette_capacity(FakePipette()) is P300

    def test_missing_config_returns_none(self):
        assert pipette_capacity(object()) is None

    def test_non_pipetteconfig_config_returns_none(self):
        class FakePipette:
            config = {"max_volume": 200}

        assert pipette_capacity(FakePipette()) is None


# ─── plan_strokes: boundary volumes ──────────────────────────────────────────


class TestPlanStrokesBoundaries:

    def test_exactly_model_max_is_single_stroke(self):
        assert plan_strokes(200.0, P300) == [200.0]

    def test_exactly_model_min_is_single_stroke(self):
        assert plan_strokes(20.0, P300) == [20.0]

    def test_below_model_min_rejected(self):
        with pytest.raises(LiquidTransferPreflightError, match="minimum"):
            plan_strokes(19.0, P300)

    def test_capacity_plus_one_splits_into_two_legal_strokes(self):
        strokes = plan_strokes(201.0, P300)
        assert len(strokes) == 2
        assert sum(strokes) == pytest.approx(201.0)
        for stroke in strokes:
            assert P300.min_volume - 1e-6 <= stroke <= P300.max_volume + 1e-6

    def test_zero_rejected(self):
        with pytest.raises(LiquidTransferPreflightError, match="> 0"):
            plan_strokes(0.0, P300)

    def test_negative_rejected(self):
        with pytest.raises(LiquidTransferPreflightError, match="> 0"):
            plan_strokes(-5.0, P300)

    def test_non_finite_rejected(self):
        with pytest.raises(LiquidTransferPreflightError):
            plan_strokes(float("nan"), P300)

    def test_no_capacity_metadata_single_unsplit_stroke(self):
        # Bare test doubles without PipetteConfig get one unsplit stroke,
        # matching pre-Feature-04 behavior exactly.
        assert plan_strokes(5000.0, None) == [5000.0]

    def test_no_capacity_metadata_still_rejects_non_positive(self):
        with pytest.raises(LiquidTransferPreflightError):
            plan_strokes(0.0, None)


class TestPlanStrokesSplitting:

    def test_600ul_on_p300_splits_into_at_least_two_legal_strokes(self):
        strokes = plan_strokes(600.0, P300)
        assert len(strokes) >= 2
        assert sum(strokes) == pytest.approx(600.0)
        for stroke in strokes:
            assert P300.min_volume - 1e-6 <= stroke <= P300.max_volume + 1e-6

    def test_600ul_on_p300_is_exactly_three_even_strokes(self):
        # ceil(600 / 200) == 3, evenly divisible -> deterministic 200/200/200.
        assert plan_strokes(600.0, P300) == pytest.approx([200.0, 200.0, 200.0])

    def test_split_is_deterministic_across_calls(self):
        assert plan_strokes(610.0, P300) == plan_strokes(610.0, P300)

    def test_uneven_split_remainder_absorbed_by_last_stroke(self):
        strokes = plan_strokes(410.0, P300)
        # ceil(410/200) = 3 -> base = 410/3, last stroke absorbs the remainder.
        assert len(strokes) == 3
        assert strokes[0] == pytest.approx(strokes[1])
        assert sum(strokes) == pytest.approx(410.0)

    def test_huge_volume_splits_into_many_bounded_strokes(self):
        strokes = plan_strokes(3800.0, P300)
        assert len(strokes) == math.ceil(3800.0 / 200.0)
        assert sum(strokes) == pytest.approx(3800.0)
        for stroke in strokes:
            assert stroke <= P300.max_volume + 1e-6

    def test_pathological_model_where_split_cannot_satisfy_min_raises(self):
        from cubos.instruments.pipette.models import PipetteConfig, PipetteFamily

        # Stroke splitting reads capability only, so a bare PipetteConfig is
        # enough -- no plunger geometry needed.
        narrow = PipetteConfig(
            name="narrow", family=PipetteFamily.OT2, channels=1,
            max_volume=100.0, min_volume=90.0,
        )
        # 150 uL needs 2 strokes of 75 each, but min_volume is 90.
        with pytest.raises(LiquidTransferPreflightError, match="Cannot split"):
            plan_strokes(150.0, narrow)


# ─── validate_dead_volume / validate_destination_overflow ───────────────────


class TestDeadVolumeGuard:

    def test_rejects_when_source_would_cross_dead_volume_floor(self):
        with pytest.raises(LiquidTransferPreflightError, match="dead-volume"):
            validate_dead_volume(
                source_current_volume_ul=100.0,
                dead_volume_ul=50.0,
                requested_volume_ul=60.0,
                source_label="source",
            )

    def test_allows_exact_available_volume(self):
        validate_dead_volume(
            source_current_volume_ul=100.0,
            dead_volume_ul=50.0,
            requested_volume_ul=50.0,
            source_label="source",
        )

    def test_zero_dead_volume_allows_full_draw(self):
        validate_dead_volume(
            source_current_volume_ul=100.0,
            dead_volume_ul=0.0,
            requested_volume_ul=100.0,
            source_label="source",
        )


class TestDestinationOverflowGuard:

    def test_rejects_projected_volume_above_working_volume(self):
        with pytest.raises(LiquidTransferPreflightError, match="working volume"):
            validate_destination_overflow(
                destination_current_volume_ul=80.0,
                working_volume_ul=100.0,
                requested_volume_ul=30.0,
                destination_label="dest",
            )

    def test_allows_exact_working_volume(self):
        validate_destination_overflow(
            destination_current_volume_ul=80.0,
            working_volume_ul=100.0,
            requested_volume_ul=20.0,
            destination_label="dest",
        )


# ─── derive_liquid_relative_height ───────────────────────────────────────────


class TestDeriveLiquidRelativeHeight:

    def test_missing_geometry_returns_none(self):
        vial = _vial(height=None, diameter=None)
        assert derive_liquid_relative_height(vial, 100.0) is None

    def test_offset_never_positive(self):
        vial = _vial(height=100.0, diameter=20.0)
        offset = derive_liquid_relative_height(vial, 4500.0)
        assert offset <= 0.0

    def test_offset_within_vessel_bounds_at_high_volume(self):
        vial = _vial(height=100.0, diameter=20.0, working_volume_ul=4500.0)
        offset = derive_liquid_relative_height(vial, 4500.0)
        assert -vial.height <= offset <= 0.0

    def test_offset_within_vessel_bounds_at_low_volume(self):
        vial = _vial(height=100.0, diameter=20.0, dead_volume_ul=50.0)
        offset = derive_liquid_relative_height(vial, 60.0)
        assert -vial.height <= offset <= 0.0

    def test_tip_follows_liquid_down_as_volume_drops(self):
        vial = _vial(height=100.0, diameter=20.0)
        high = derive_liquid_relative_height(vial, 3000.0)
        low = derive_liquid_relative_height(vial, 500.0)
        # Offsets are <= 0 (below rim); lower volume -> more negative (deeper).
        assert low < high

    def test_clamped_at_dead_volume_floor(self):
        vial = _vial(height=100.0, diameter=20.0, dead_volume_ul=200.0)
        at_dead_volume = derive_liquid_relative_height(vial, 200.0)
        below_dead_volume = derive_liquid_relative_height(vial, 50.0)
        # Once volume is at/under the dead-volume floor, the offset clamps
        # rather than continuing to track the (physically unreachable) level.
        assert below_dead_volume == pytest.approx(at_dead_volume)

    def test_clamped_at_bottom_clearance_when_dead_volume_is_zero(self):
        vial = _vial(height=100.0, diameter=20.0, dead_volume_ul=0.0)
        offset = derive_liquid_relative_height(
            vial, 1.0, bottom_clearance_mm=5.0,
        )
        area = math.pi * (vial.diameter / 2.0) ** 2
        clearance_floor = 5.0 - vial.height
        # Bottom clearance wins over the (deeper) near-empty liquid surface.
        assert offset == pytest.approx(clearance_floor)
        assert offset > (1.0 / area) - vial.height

    def test_higher_of_dead_volume_and_clearance_floor_wins(self):
        # A large dead_volume_ul should produce a floor above the fixed
        # clearance floor.
        vial = _vial(height=100.0, diameter=20.0, dead_volume_ul=500.0)
        offset = derive_liquid_relative_height(
            vial, 10.0, bottom_clearance_mm=2.0,
        )
        area = math.pi * (vial.diameter / 2.0) ** 2
        dead_floor = (500.0 / area) - vial.height
        clearance_floor = 2.0 - vial.height
        assert offset == pytest.approx(max(dead_floor, clearance_floor))

    def test_non_positive_geometry_returns_none(self):
        # Vial itself rejects height/diameter <= 0 at construction time; this
        # exercises the function's own defensive guard via a duck-typed
        # stand-in the same way a looser future labware type might.
        from types import SimpleNamespace

        fake_vial = SimpleNamespace(height=0.0, diameter=20.0, dead_volume_ul=0.0)
        assert derive_liquid_relative_height(fake_vial, 100.0) is None
