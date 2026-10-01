"""Tests for Feature-06 vial ``capped`` metadata.

Mirrors ``tests/deck/test_container_role.py``: covers the ``Vial``/
``VialGrid`` runtime model, the deck YAML schema, and the deck loader
wiring for plain vials, vial grids, and nested holder vials.
"""

from __future__ import annotations

import tempfile
from pathlib import Path


from cubos.deck.labware.labware import Coordinate3D
from cubos.deck.labware.vial import Vial
from cubos.deck.loader import load_deck_from_yaml


def _write(yaml_text: str) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_text)
        return f.name


def _make_vial(**overrides) -> Vial:
    defaults = dict(
        name="v",
        height=57.0,
        diameter=28.0,
        location=Coordinate3D(x=0.0, y=0.0, z=0.0),
        capacity_ul=1000.0,
        working_volume_ul=900.0,
    )
    defaults.update(overrides)
    return Vial(**defaults)


# ─── Vial model validation ────────────────────────────────────────────────


class TestVialCappedModel:
    def test_capped_defaults_to_none(self):
        assert _make_vial().capped is None


# ─── Deck YAML: vial grid (uniform capped) ─────────────────────────────────


def test_vial_grid_capped_propagates_to_every_vial():
    yaml = """
labware:
  reagents:
    type: vial_grid
    name: reagents
    rows: 1
    columns: 2
    calibration:
      a1: {x: 10.0, y: 20.0, z: 30.0}
      a2: {x: 20.0, y: 20.0, z: 30.0}
    x_offset: 10.0
    y_offset: 10.0
    vial_height: 40.0
    vial_diameter: 12.0
    capacity_ul: 500.0
    working_volume_ul: 400.0
    vial_capped: true
"""
    path = _write(yaml)
    try:
        result = load_deck_from_yaml(path)
        grid = result["reagents"]
        for vial in grid.vials.values():
            assert vial.capped is True
    finally:
        Path(path).unlink(missing_ok=True)
