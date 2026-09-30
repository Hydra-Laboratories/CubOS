"""Regression: no XY travel below the working-volume ceiling across tools.

Runs the real ``pick_up_tip -> decap -> aspirate -> cap`` commands against
the real ``InstrumentedGantry`` with a controller double that emits the
exact G-code ``Mill`` would (its move builders are called unmodified).
The pipette with a tip hangs lower than the capper, so any XY G-code
issued while the carriage sits below ``z_max`` is a bench crash.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from cubos.deck.loader import load_deck_from_yaml_safe
from cubos.gantry.coordinates import Coordinates
from cubos.gantry.gantry_driver.driver import DEFAULT_FEED_RATE, Mill
from cubos.gantry.instrument_mount import InstrumentedGantry
from cubos.instruments.capper.vendors.mock import MockCapper
from cubos.instruments.pipette.vendors.opentrons import OpentronsPipette
from cubos.protocol_engine.commands.capper import cap, decap
from cubos.protocol_engine.commands.pipette import aspirate, pick_up_tip
from cubos.protocol_engine.runtime import ProtocolContext

SAFE_Z = 55.0
Z_MAX = 250.0
VIAL_RIM_Z = 40.0
CAPPER_DEPTH = 45.0
PIPETTE_DEPTH = 40.0
TIP_LENGTH = 25.0

DECK_YAML = f"""\
labware:
  reagent:
    type: vial
    name: reagent
    height: 40.0
    diameter: 15.0
    location: {{x: 100.0, y: 100.0, z: {VIAL_RIM_Z}}}
    capacity_ul: 500.0
    working_volume_ul: 400.0
    capped: true
  tip_rack:
    type: tip_rack
    name: tip_rack
    rows: 2
    columns: 12
    tip_length: {TIP_LENGTH}
    pickup_z: 30.0
    drop_z: 30.0
    x_offset: 9.0
    y_offset: 9.0
    calibration:
      a1: {{x: 200.0, y: 200.0, z: 30.0}}
      a2: {{x: 200.0, y: 191.0}}
"""


class GCodeController:
    """Gantry double: applies Mill's real move builders and records G-code."""

    def __init__(self):
        self.config = {"working_volume": {"z_min": 0.0, "z_max": Z_MAX}}
        self.cur = Coordinates(0.0, 0.0, Z_MAX)
        self.gcode: list[tuple[str, float, float, float]] = []
        mill = SimpleNamespace(
            default_feed_rate=DEFAULT_FEED_RATE, logger=logging.getLogger("mill"),
        )
        mill._validate_target_coordinates = (
            lambda t: Mill._validate_target_coordinates(mill, t)
        )
        mill._validate_finite_coordinate = Mill._validate_finite_coordinate
        self._mill = mill

    def get_coordinates(self):
        return {"x": self.cur.x, "y": self.cur.y, "z": self.cur.z}

    def move_to(self, x, y, z, travel_z=None):
        target = Coordinates(x, y, z)
        if travel_z is None:
            cmds = Mill._build_direct_move(self._mill, self.cur, target)
        else:
            cmds = Mill._build_transit_move(self._mill, self.cur, target, travel_z)
        for cmd in cmds:
            word = cmd.split()[1]
            setattr(self.cur, word[0].lower(), float(word[1:]))
            self.gcode.append((word[0], self.cur.x, self.cur.y, self.cur.z))


@pytest.fixture
def rig(tmp_path):
    deck_path = tmp_path / "deck.yaml"
    deck_path.write_text(DECK_YAML, encoding="utf-8")
    deck = load_deck_from_yaml_safe(deck_path)
    pipette = OpentronsPipette(
        pipette_model="p300_single_gen2", offline=True, name="pipette",
        offset_x=115.9, offset_y=6.1, depth=PIPETTE_DEPTH,
    )
    capper = MockCapper(
        engage_depth_mm=-10.0, name="capper",
        offset_x=62.9, offset_y=5.1, depth=CAPPER_DEPTH,
    )
    controller = GCodeController()
    gantry = InstrumentedGantry(
        controller, {"pipette": pipette, "capper": capper}, safe_z=SAFE_Z,
    )
    ctx = ProtocolContext(gantry=gantry, deck=deck, logger=logging.getLogger("test"))
    return ctx, controller, pipette, capper


def _run_sequence(ctx):
    pick_up_tip(ctx, "tip_rack.A1")
    decap(ctx, "capper", "reagent")
    aspirate(ctx, "reagent", 50.0)
    cap(ctx, "capper", "reagent")


def test_every_xy_gcode_is_issued_at_the_ceiling(rig):
    ctx, controller, _, _ = rig
    _run_sequence(ctx)
    lateral = [g for g in controller.gcode if g[0] in ("X", "Y")]
    assert lateral, "sequence must include XY travel"
    low = [g for g in lateral if g[3] < Z_MAX]
    assert low == []


def test_pipette_tip_never_below_rim_while_moving_xy(rig):
    ctx, controller, pipette, _ = rig
    _run_sequence(ctx)
    lowest_tip = min(
        z - pipette.effective_depth for axis, _, _, z in controller.gcode if axis in ("X", "Y")
    )
    assert lowest_tip > VIAL_RIM_Z


def test_capper_ends_above_the_vial_not_at_a_park_position(rig):
    ctx, controller, _, capper = rig
    pick_up_tip(ctx, "tip_rack.A1")
    decap(ctx, "capper", "reagent")
    axis, x, y, z = controller.gcode[-1]
    assert axis == "Z"
    assert (x + capper.offset_x, y + capper.offset_y) == pytest.approx((100.0, 100.0))
    assert z - capper.effective_depth == pytest.approx(SAFE_Z)


def test_engage_and_retract_stay_z_only(rig):
    ctx, controller, _, _ = rig
    pick_up_tip(ctx, "tip_rack.A1")
    controller.gcode.clear()
    decap(ctx, "capper", "reagent")
    # lift, X, Y, descend to safe_z, engage, retract: exactly one XY leg.
    axes = [g[0] for g in controller.gcode]
    assert axes == ["Z", "X", "Y", "Z", "Z", "Z"]
