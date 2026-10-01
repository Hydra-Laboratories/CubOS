"""Tests for Feature-05 container role/solution/allowed_solutions metadata.

Covers the ``Vial``/``VialGrid`` runtime model, the deck YAML schema, and
the deck loader wiring for plain vials, vial grids, and nested holder vials.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from cubos.deck.labware.container_role import KNOWN_CONTAINER_ROLES
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


class TestVialRoleModel:


    @pytest.mark.parametrize("role", sorted(KNOWN_CONTAINER_ROLES))
    def test_every_known_role_accepted(self, role):
        assert _make_vial(role=role).role == role

    def test_unknown_role_rejected(self):
        with pytest.raises(Exception, match="role"):
            _make_vial(role="not_a_real_role")


    def test_blank_solution_rejected(self):
        with pytest.raises(Exception, match="solution"):
            _make_vial(solution="   ")


    def test_empty_allowed_solutions_list_rejected(self):
        with pytest.raises(Exception, match="allowed_solutions"):
            _make_vial(role="waste", allowed_solutions=[])

    def test_blank_allowed_solutions_entry_rejected(self):
        with pytest.raises(Exception, match="allowed_solutions"):
            _make_vial(role="waste", allowed_solutions=["water", "  "])


# ─── Deck YAML: plain vial ────────────────────────────────────────────────


def test_vial_unknown_role_rejected_at_load():
    yaml = """
labware:
  bad:
    type: vial
    name: bad
    role: not_a_role
    height: 57.0
    diameter: 28.0
    location: {x: 30.0, y: 40.0, z: 30.0}
    capacity_ul: 1500.0
    working_volume_ul: 1200.0
"""
    path = _write(yaml)
    try:
        with pytest.raises(Exception, match="role"):
            load_deck_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


# ─── Deck YAML: vial grid (uniform role/solution) ─────────────────────────


def test_vial_grid_role_and_solution_propagate_to_every_vial():
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
    vial_role: stock
    vial_solution: acetonitrile
"""
    path = _write(yaml)
    try:
        result = load_deck_from_yaml(path)
        grid = result["reagents"]
        for vial in grid.vials.values():
            assert vial.role == "stock"
            assert vial.solution == "acetonitrile"
    finally:
        Path(path).unlink(missing_ok=True)


def test_vial_grid_allowed_solutions_propagate_to_every_vial():
    yaml = """
labware:
  waste_grid:
    type: vial_grid
    name: waste_grid
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
    vial_role: waste
    vial_allowed_solutions: [water]
"""
    path = _write(yaml)
    try:
        result = load_deck_from_yaml(path)
        grid = result["waste_grid"]
        for vial in grid.vials.values():
            assert vial.role == "waste"
            assert vial.allowed_solutions == ["water"]
    finally:
        Path(path).unlink(missing_ok=True)
