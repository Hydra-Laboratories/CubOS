"""Tests for the move protocol command — unit and end-to-end."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cubos.deck.labware.labware import Coordinate3D
from cubos.protocol_engine.loader import load_protocol_from_yaml
from cubos.protocol_engine.runtime import ProtocolContext


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _mock_context(
    resolve_return: Coordinate3D | None = None,
    positions: dict | None = None,
) -> ProtocolContext:
    """Create a ProtocolContext with mock board and deck."""
    coord = resolve_return or Coordinate3D(x=10.0, y=10.0, z=15.0)

    board = MagicMock()
    deck = MagicMock()
    deck.resolve_coordinate.return_value = coord

    return ProtocolContext(
        gantry=board,
        deck=deck,
        positions=positions or {},
        logger=logging.getLogger("test_move"),
    )


# ─── Unit tests: routing by position type ─────────────────────────────────────


class TestMoveCommandRouting:


    def test_named_position_forwards_travel_z(self):
        from cubos.protocol_engine.commands.move import move

        ctx = _mock_context(positions={"safe": [50.0, 50.0, 70.0]})
        move(ctx, instrument="pipette", position="safe", travel_z=80.0)

        ctx.gantry.move.assert_called_once_with(
            "pipette", (50.0, 50.0, 70.0), travel_z=80.0,
        )
        ctx.gantry.move_to_labware.assert_not_called()
        ctx.deck.resolve_coordinate.assert_not_called()


    def test_invalid_deck_target_propagates_error(self):
        """A dotted position (looks like a deck target) that fails to
        resolve propagates the underlying error unwrapped."""
        from cubos.protocol_engine.commands.move import move

        ctx = _mock_context()
        ctx.deck.resolve_coordinate.side_effect = KeyError("No labware 'bad' on deck.")

        with pytest.raises(KeyError, match="bad"):
            move(ctx, instrument="pipette", position="bad.A1")

    def test_bare_string_typo_mentions_both_namespaces(self):
        """A non-dotted string that's neither a named position nor a
        deck labware key gets a clear error listing both namespaces —
        catches typos like 'home_postion' vs 'home_position'."""
        from cubos.protocol_engine.commands.move import move
        from cubos.protocol_engine.errors import ProtocolExecutionError

        ctx = _mock_context(positions={"home_position": [0, 0, 80]})
        ctx.deck.resolve_coordinate.side_effect = KeyError("Not found")

        with pytest.raises(ProtocolExecutionError, match="home_postion"):
            move(ctx, instrument="pipette", position="home_postion")

    def test_deck_target_rejects_travel_z_override(self):
        from cubos.protocol_engine.commands.move import move
        from cubos.protocol_engine.errors import ProtocolExecutionError

        ctx = _mock_context()

        with pytest.raises(ProtocolExecutionError, match="travel_z is only supported"):
            move(ctx, instrument="pipette", position="plate_1.A1", travel_z=80.0)


# ─── End-to-end: YAML → load → run ──────────────────────────────────────────


class TestMoveEndToEnd:


    def test_yaml_literal_coords_use_raw_move(self):
        yaml_content = """
protocol:
  - move:
      instrument: pipette
      position: [100.0, 50.0, 30.0]
"""
        ctx = _mock_context()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            path = f.name
        try:
            protocol = load_protocol_from_yaml(path)
            protocol.execute(ctx)

            ctx.gantry.move.assert_called_once_with("pipette", (100.0, 50.0, 30.0))
            ctx.gantry.move_to_labware.assert_not_called()
        finally:
            Path(path).unlink(missing_ok=True)

    def test_yaml_named_position_forwards_travel_z(self):
        yaml_content = """
positions:
  safe_z: [0.0, 0.0, 20.0]
protocol:
  - move:
      instrument: pipette
      position: safe_z
      travel_z: 20.0
"""
        ctx = _mock_context(positions={"safe_z": [0.0, 0.0, 20.0]})

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            path = f.name
        try:
            protocol = load_protocol_from_yaml(path)
            protocol.execute(ctx)

            ctx.gantry.move.assert_called_once_with(
                "pipette", (0.0, 0.0, 20.0), travel_z=20.0,
            )
            ctx.gantry.move_to_labware.assert_not_called()
        finally:
            Path(path).unlink(missing_ok=True)
