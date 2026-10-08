"""Native protocol demonstrations used by the Color Twin.

The YAML files are the protocol inputs.  Routing metadata lives beside them so
the simulator can describe the scene while the core planner owns route
selection. The adapter in :mod:`sim.runtime` displays serialized core plans
for planning-enabled runs; legacy runs display their native movement events.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .defaults import DECK, GANTRY, STOCKS, dump, protocol_for


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
SAVED_USER_DECK = EXAMPLES / "saved-user" / "deck.yaml"
SAVED_PICUS120 = EXAMPLES / "saved-picus120"


@dataclass(frozen=True)
class Demo:
    id: str
    name: str
    description: str
    mode: str
    gantry_yaml: str
    deck_yaml: str
    protocol_yaml: str
    stocks: tuple[dict[str, str], ...]
    assumptions: tuple[str, ...]
    routing: dict[str, Any]
    initial_position: tuple[float, float, float] | None = None

    def bundle(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "mode": self.mode,
            "gantry_yaml": self.gantry_yaml,
            "deck_yaml": self.deck_yaml,
            "protocol_yaml": self.protocol_yaml,
            "stocks": [dict(stock) for stock in self.stocks],
            "assumptions": list(self.assumptions),
            "routing": copy.deepcopy(self.routing),
            "initial_position": list(self.initial_position) if self.initial_position else None,
        }


def _ordinary() -> Demo:
    deck = copy.deepcopy(DECK)
    gantry = copy.deepcopy(GANTRY)
    gantry["instruments"]["pipette"]["motion_envelope"] = {"box": {"offset": {"x": -2, "y": -2, "z": 0}, "size": {"x": 4, "y": 4, "z": 30}}, "additional_boxes": [{"offset": {"x": -15, "y": -15, "z": 30}, "size": {"x": 30, "y": 30, "z": 110}}], "attached_tip_radius_mm": 2.5}
    gantry["instruments"]["camera"]["motion_envelope"] = {"box": {"offset": {"x": -8, "y": -8, "z": 0}, "size": {"x": 16, "y": 16, "z": 20}}, "additional_boxes": [{"offset": {"x": -15, "y": -15, "z": 20}, "size": {"x": 30, "y": 30, "z": 80}}]}
    return Demo(
        id="ordinary-transfer",
        name="Ordinary vertical transfer",
        description="A roomy rack and plate transfer with identical liquid outcomes in legacy and planned modes.",
        mode="comparison",
        gantry_yaml=dump(gantry),
        deck_yaml=dump(deck),
        protocol_yaml=_ordinary_protocol(),
        stocks=tuple(STOCKS),
        assumptions=(
            "Photo-derived reference geometry; not calibrated hardware.",
            "The ordinary case is intentionally clear at the default vertical access plane.",
            "Legacy and planner results are compared from the same native protocol and input fluids.",
        ),
        routing={
            "schema_version": "labware-routing/v1",
            "planner_enabled": False,
            "access_policy": {"default": "vertical"},
            "registered_geometry": {
                "tips": {"kind": "fixture", "source": "illustrative", "envelope": {"length": 128, "width": 86, "height": 45}},
                "stocks": {"kind": "fixture", "source": "illustrative"},
                "plate": {"kind": "fixture", "source": "illustrative"},
            },
        },
        initial_position=None,
    )


def _saved_deck() -> tuple[str, dict[str, Any]]:
    # Keep this source copy byte-for-byte identical to the supplied user deck.
    text = SAVED_USER_DECK.read_text()
    return text, yaml.safe_load(text)


def _saved_gantry() -> str:
    data = copy.deepcopy(GANTRY)
    data["cnc"].update(factory_z_travel_mm=56, safe_z=66.5)
    data["working_volume"].update(x_max=258.205, y_max=144.645, z_min=10.5, z_max=66.5)
    data["instruments"]["pipette"].update(pipette_model="simulation_1000ul", depth=-70)
    data["instruments"]["camera"].update(offset_x=0, offset_y=-46.5, depth=-114.964)
    data["instruments"]["pipette"]["motion_envelope"] = {
        "box": {"offset": {"x": -2, "y": -2, "z": 0}, "size": {"x": 4, "y": 4, "z": 30}},
        "additional_boxes": [{"offset": {"x": -15, "y": -15, "z": 30}, "size": {"x": 30, "y": 30, "z": 110}}],
        "attached_tip_radius_mm": 4,
    }
    data["instruments"]["camera"]["motion_envelope"] = {
        "box": {"offset": {"x": -8, "y": -8, "z": 0}, "size": {"x": 16, "y": 16, "z": 20}},
        "additional_boxes": [{"offset": {"x": -15, "y": -15, "z": 20}, "size": {"x": 30, "y": 30, "z": 80}}],
    }
    return dump(data)


def _saved_side_exit() -> Demo:
    deck_text, deck = _saved_deck()
    # The original calibrated deck is preserved above.  This derived profile
    # carries the same anchors/pitches but registers an exit beyond the CAD
    # body with margin: the nominal negative-X edge is 139.456 mm.
    derived = copy.deepcopy(deck)
    derived["labware"]["tips"]["side_exit"] = {"lift_mm": 30, "exit_x": 131.456}
    _attach_motion(derived, _saved_motion_metadata(side_exit=True))
    return Demo(
        id="saved-side-exit",
        name="Saved deck · A1 side exit",
        description="Pick up from exposed A1, lift 30 mm, exit inward along −X, then transfer three colors.",
        mode="side-exit",
        gantry_yaml=_saved_gantry(),
        deck_yaml=yaml.safe_dump(derived, sort_keys=False),
        protocol_yaml=_side_protocol(),
        stocks=tuple(STOCKS),
        assumptions=(
            "The deck.yaml source copy is preserved verbatim in examples/saved-user/deck.yaml.",
            "All saved plate, stock, tip, waste coordinates and pitches are retained.",
            "The original exit_x=140 mm is inside the nominal registered body edge; the profile requests 131.456 mm with an 8 mm margin and core derives the final exit from every mounted-tool envelope.",
            "70 mm tip extension and nominal CAD envelope require physical fit and clearance validation.",
        ),
        routing={
            "schema_version": "labware-routing/v1",
            "planner_enabled": True,
            "registered_geometry": {
                "tips": {"kind": "fixture", "source": "ColorMatching_TipHolder CAD", "anchor": "tips.A1", "offset": {"x": -7, "y": -78, "z": -70}, "envelope": {"length": 124, "width": 84, "height": 63}},
                "plate": {"kind": "fixture", "source": "saved user deck"},
                "stocks": {"kind": "fixture", "source": "saved user deck", "rows": 2, "columns": 6},
                "waste": {"kind": "fixture", "source": "saved user deck"},
            },
            "access_policy": {"default": "vertical", "pick_up_tip": {"strategy": "side-exit", "lift_mm": 30, "edge": "negative-x", "clearance_mm": 8.0}},
        },
        initial_position=(230, 90, 66.5),
    )


def _saved_detour() -> Demo:
    deck_text, deck = _saved_deck()
    # Keep the exact saved deck input, including calibrated values.  Routing
    # registration is a sidecar consumed by the planner adapter.
    return Demo(
        id="saved-detour-to-waste",
        name="Saved deck · Y-first waste detour",
        description="Transit from the plate toward waste around the tall rack; the planner selects Y before X.",
        mode="detour",
        gantry_yaml=_saved_gantry(),
        deck_yaml=yaml.safe_dump(_with_motion(deck, _saved_motion_metadata(side_exit=True)), sort_keys=False),
        protocol_yaml=_detour_protocol(),
        stocks=tuple(STOCKS),
        assumptions=(
            "The exact saved user deck is used as the scene input.",
            "The tip rack is registered as a 120 × 84 × 63 mm nominal obstacle with a conservative 124 mm X envelope.",
            "The planner owns detour selection; the simulator displays the shared plan and execution events.",
            "Nominal geometry is not physical collision certification.",
        ),
        routing={
            "schema_version": "labware-routing/v1",
            "planner_enabled": True,
            "registered_geometry": {
                "tips": {"kind": "fixture", "source": "ColorMatching_TipHolder CAD", "anchor": "tips.A1", "offset": {"x": -7, "y": -78, "z": -70}, "envelope": {"length": 124, "width": 84, "height": 63}},
                "plate": {"kind": "fixture", "source": "saved user deck"},
                "waste": {"kind": "fixture", "source": "saved user deck"},
                "stocks": {"kind": "fixture", "source": "saved user deck", "rows": 2, "columns": 6},
            },
            "access_policy": {"default": "vertical", "transit": {"allowed_corridors": ["front-of-rack", "back-of-rack"]}},
        },
        initial_position=(230, 90, 66.5),
    )


def _saved_picus120() -> Demo:
    # Verbatim station snapshot; no dump()/derivation, unlike the other saved demos.
    gantry_text = (SAVED_PICUS120 / "gantry.yaml").read_text()
    deck_text = (SAVED_PICUS120 / "deck.yaml").read_text()
    protocol_text = (SAVED_PICUS120 / "protocol.yaml").read_text()
    return Demo(
        id="saved-picus120",
        name="Saved deck · Picus 120 µL batch 1",
        description="Picus 2 120 µL pipette, 42 mm tips, standard vertical rack — replays batch 1 of the running color-matching campaign.",
        mode="planned",
        gantry_yaml=gantry_text,
        deck_yaml=deck_text,
        protocol_yaml=protocol_text,
        stocks=tuple(STOCKS),
        assumptions=(
            "The frame is tip-end referenced: pipette depth is −42 mm from a tip-attached calibration.",
            "Bare-nozzle pickup plane is 81.0 mm = carriage 39.0 mm + 42 mm tip length.",
            "Mix engages at −2 mm because the tip end bottoms out 2.4 mm below the plate rim.",
            "Transfers above 120 µL are split into multiple pipette strokes by core.",
            "The waste point is shifted by the frame delta from the prior simulation point; unconfirmed on hardware.",
            "Camera color measurement is omitted in the sim; only the camera move steps are executed.",
            "Nominal geometry from the deck's motion boxes is not collision certification.",
        ),
        routing={
            "schema_version": "labware-routing/v1",
            "planner_enabled": True,
            "registered_geometry": {
                "tips": {"kind": "fixture", "source": "saved station deck", "anchor": "tips.A1", "offset": {"x": -7, "y": -78, "z": -60}, "envelope": {"length": 124, "width": 84, "height": 60}},
                "plate": {"kind": "fixture", "source": "saved station deck"},
                "stocks": {"kind": "fixture", "source": "saved station deck", "rows": 2, "columns": 6},
                "waste": {"kind": "fixture", "source": "saved station deck"},
            },
            "access_policy": {"default": "vertical"},
        },
        initial_position=(244.589, 144.0, 94.601),
    )


def _side_protocol() -> str:
    # Native protocol: side-exit is declared by deck access metadata; there
    # are no handwritten lift/exit moves in the protocol itself.
    steps = []
    for index, stock in enumerate(STOCKS):
        tip = f"A{index + 1}"
        steps.extend([
            {"pick_up_tip": {"position": f"tips.{tip}"}},
            {"transfer": {"source": stock["target"], "destination": "plate.A1", "volume_ul": 100, "source_height": -8}},
        ])
        if index == 2:
            steps.append({"mix": {"position": "plate.A1", "volume_ul": 60, "cycles": 3, "height": -3}})
        steps.append({"drop_tip": {"position": "waste"}})
    return dump({"protocol": steps})


def _ordinary_protocol() -> str:
    raw = yaml.safe_load(protocol_for([100, 100, 100]))
    # Planning v1 covers movement and fluid operations. Keep this comparison
    # protocol identical in both modes while omitting the optional camera
    # observation, which is intentionally outside the planner command set.
    raw["protocol"] = [step for step in raw["protocol"] if "measure" not in step]
    return dump(raw)


def _detour_protocol() -> str:
    return dump({"protocol": [
        {"pick_up_tip": {"position": "tips.A1"}},
        {"transfer": {"source": "stocks.A1", "destination": "plate.A1", "volume_ul": 100, "source_height": -8}},
        {"drop_tip": {"position": "waste"}},
    ]})


def _ordinary_motion_metadata() -> dict[str, Any]:
    return {
        "clearance_mm": 2,
        "fixtures": {
            "plate": {"box": {"anchor": "A1", "offset": {"x": -14.38, "y": -74.24, "z": -15}, "size": {"x": 127.76, "y": 85.48, "z": 15}}, "access": {"transfer": {"strategy": "vertical"}, "mix": {"strategy": "vertical"}}},
            "stocks": {"box": {"anchor": "A1", "offset": {"x": -10, "y": -33, "z": -45}, "size": {"x": 66, "y": 43, "z": 45}}, "access": {"transfer": {"strategy": "vertical"}}},
            "tips": {"box": {"anchor": "A1", "offset": {"x": -8, "y": -70, "z": -45}, "size": {"x": 128, "y": 86, "z": 45}}, "occupied_tip_radius_mm": 2.5, "access": {"pick_up_tip": {"strategy": "vertical"}}},
        },
    }


def _attach_motion(deck: dict[str, Any], metadata: dict[str, Any]) -> None:
    deck["motion_planning"] = {"clearance_mm": metadata.get("clearance_mm", 2)}
    for key, fixture in metadata.get("fixtures", {}).items():
        if key in deck.get("labware", {}):
            deck["labware"][key]["motion"] = copy.deepcopy(fixture)


def _with_motion(deck: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(deck)
    _attach_motion(result, metadata)
    return result


def _saved_motion_metadata(*, side_exit: bool = False) -> dict[str, Any]:
    tips_access = {"strategy": "side_exit", "lift_mm": 30, "exit_edge": "x_min", "clearance_mm": 8} if side_exit else {"strategy": "vertical"}
    return {
        "clearance_mm": 2,
        "fixtures": {
            "plate": {"box": {"anchor": "A1", "offset": {"x": -14.38, "y": -74.24, "z": -15}, "size": {"x": 127.76, "y": 85.48, "z": 15}}, "access": {"transfer": {"strategy": "vertical"}, "mix": {"strategy": "vertical"}}},
            "stocks": {"box": {"anchor": "A1", "offset": {"x": -10, "y": -30, "z": -45}, "size": {"x": 120, "y": 40, "z": 45}}, "access": {"transfer": {"strategy": "vertical"}}},
            "tips": {"box": {"anchor": "A1", "offset": {"x": -7, "y": -78, "z": -70}, "size": {"x": 124, "y": 84, "z": 63}}, "occupied_tip_radius_mm": 2.5, "access": {"pick_up_tip": tips_access}},
            "waste": {"box": {"anchor": "location", "offset": {"x": -10, "y": -10, "z": -45}, "size": {"x": 20, "y": 20, "z": 45}}, "access": {"drop_tip": {"strategy": "vertical"}}},
        },
    }


DEMO_BUILDERS = {
    "ordinary-transfer": _ordinary,
    "saved-side-exit": _saved_side_exit,
    "saved-detour-to-waste": _saved_detour,
    "saved-picus120": _saved_picus120,
}


def demos() -> list[dict[str, Any]]:
    return [DEMO_BUILDERS[key]().bundle() for key in DEMO_BUILDERS]


def get_demo(profile: str | None = None) -> Demo:
    if profile in (None, "", "ordinary-transfer", "default"):
        return _ordinary()
    try:
        return DEMO_BUILDERS[profile]()
    except KeyError as exc:
        raise ValueError(f"Unknown simulator profile {profile!r}; choose from {sorted(DEMO_BUILDERS)}") from exc
