from __future__ import annotations

import pytest
from pydantic import ValidationError

from cubos.deck.loader import _build_deck_from_raw
from cubos.deck.yaml_schema import DeckYamlSchema
from cubos.gantry.loader import load_gantry_from_yaml
from cubos.protocol_engine.routing import _world_edge


def _tip_rack(*, motion: dict) -> dict:
    return {
        "type": "tip_rack",
        "name": "tips",
        "rows": 1,
        "columns": 2,
        "pickup_z": 98.5,
        "tip_length": 70,
        "calibration": {
            "a1": {"x": 146.456, "y": 75.035, "z": 98.5},
            "a2": {"x": 156.456, "y": 75.035},
        },
        "x_offset": 10,
        "y_offset": 10,
        "location": {"x": 150, "y": 20, "z": 0},
        "motion": motion,
    }


def test_a1_motion_box_ignores_stale_holder_location() -> None:
    motion = {
        "box": {
            "anchor": "A1",
            "offset": {"x": -7, "y": -78, "z": -70},
            "size": {"x": 120, "y": 84, "z": 63},
        },
        "occupied_tip_radius_mm": 2.5,
        "access": {
            "pick_up_tip": {
                "strategy": "side_exit",
                "lift_mm": 30,
                "exit_edge": "x_min",
                "clearance_mm": 8,
            }
        },
    }
    deck = _build_deck_from_raw(
        {"motion_planning": {"clearance_mm": 2}, "labware": {"tips": _tip_rack(motion=motion)}}
    )

    assert deck.planning_enabled
    assert deck["tips"].location.x == 150
    assert deck["tips"].motion["resolved_box"] == {
        "min": {"x": pytest.approx(139.456), "y": pytest.approx(-2.965), "z": pytest.approx(28.5)},
        "max": {"x": pytest.approx(259.456), "y": pytest.approx(81.035), "z": pytest.approx(91.5)},
    }


def test_scalar_location_anchor_uses_identity_orientation() -> None:
    raw = {
        "motion_planning": {},
        "labware": {
            "waste": {
                "type": "vial",
                "name": "waste",
                "height": 20,
                "diameter": 10,
                "location": {"x": 30, "y": 40, "z": 50},
                "capacity_ul": 1000,
                "working_volume_ul": 900,
                "motion": {
                    "box": {
                        "anchor": "location",
                        "offset": {"x": -5, "y": -5, "z": -20},
                        "size": {"x": 10, "y": 10, "z": 20},
                    }
                },
            }
        },
    }

    deck = _build_deck_from_raw(raw)
    assert deck["waste"].motion["resolved_box"] == {
        "min": {"x": 25.0, "y": 35.0, "z": 30.0},
        "max": {"x": 35.0, "y": 45.0, "z": 50.0},
    }


def test_motion_anchor_must_exist_on_labware() -> None:
    raw = {
        "motion_planning": {},
        "labware": {
            "waste": {
                "type": "vial",
                "name": "waste",
                "height": 20,
                "diameter": 10,
                "location": {"x": 30, "y": 40, "z": 50},
                "capacity_ul": 1000,
                "working_volume_ul": 900,
                "motion": {
                    "box": {
                        "anchor": "A1",
                        "offset": {"x": 0, "y": 0, "z": 0},
                        "size": {"x": 10, "y": 10, "z": 20},
                    }
                },
            }
        },
    }
    with pytest.raises(ValueError, match="uses motion anchor A1"):
        _build_deck_from_raw(raw)


@pytest.mark.parametrize("operation", ["scan", "aspirate", "camera_capture"])
def test_access_rejects_unsupported_operation_keys(operation: str) -> None:
    motion = {
        "box": {
            "offset": {"x": 0, "y": 0, "z": -10},
            "size": {"x": 20, "y": 20, "z": 10},
        },
        "access": {operation: {"strategy": "vertical"}},
    }
    with pytest.raises(ValidationError):
        DeckYamlSchema.model_validate({"labware": {"tips": _tip_rack(motion=motion)}})


def test_side_exit_rejected_for_non_pickup_operation() -> None:
    motion = {
        "box": {
            "offset": {"x": 0, "y": 0, "z": -10},
            "size": {"x": 20, "y": 20, "z": 10},
        },
        "access": {
            "transfer": {
                "strategy": "side_exit",
                "lift_mm": 30,
                "exit_edge": "x_min",
            }
        },
    }
    with pytest.raises(ValidationError, match="only for pick_up_tip"):
        DeckYamlSchema.model_validate({"labware": {"tips": _tip_rack(motion=motion)}})


def test_gantry_loader_extracts_tcp_relative_motion_envelopes(tmp_path) -> None:
    path = tmp_path / "gantry.yaml"
    path.write_text(
        """
serial_port: offline
gantry_type: cub
cnc:
  factory_z_travel_mm: 100
working_volume:
  x_min: 0
  x_max: 100
  y_min: 0
  y_max: 100
  z_min: 0
  z_max: 100
instruments:
  pipette:
    type: pipette
    vendor: opentrons
    depth: -70
    motion_envelope:
      box:
        offset: {x: -5, y: -5, z: -10}
        size: {x: 10, y: 10, z: 80}
      attached_tip_radius_mm: 2.5
"""
    )

    config = load_gantry_from_yaml(path)
    assert "motion_envelope" not in config.instruments["pipette"]
    assert config.motion_envelopes["pipette"]["attached_tip_radius_mm"] == 2.5


@pytest.mark.parametrize(
    ("a2", "expected"),
    [
        ({"x": 90, "y": 100}, "x_max"),
        ({"x": 100, "y": 110}, "y_min"),
    ],
)
def test_local_exit_edge_rotates_with_a1_a2_frame(a2, expected) -> None:
    motion = {
        "box": {
            "anchor": "A1",
            "offset": {"x": -5, "y": -5, "z": -10},
            "size": {"x": 20, "y": 20, "z": 10},
        },
        "occupied_tip_radius_mm": 2.5,
        "access": {
            "pick_up_tip": {
                "strategy": "side_exit",
                "lift_mm": 30,
                "exit_edge": "x_min",
            }
        },
    }
    rack = _tip_rack(motion=motion)
    rack["calibration"] = {
        "a1": {"x": 100, "y": 100, "z": 50},
        "a2": a2,
    }
    deck = _build_deck_from_raw(
        {"motion_planning": {}, "labware": {"tips": rack}}
    )

    assert _world_edge(deck["tips"].motion, "x_min") == expected
