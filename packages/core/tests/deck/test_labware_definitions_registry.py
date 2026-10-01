"""Coverage tests for every entry in the labware definitions registry.

Each definition's config YAML is deck-YAML-entry shaped (it carries a
``type:`` key that the deck loader dispatches on), so the meaningful
validation is against the matching ``*YamlEntry`` schema rather than the
labware constructor directly.

Without this sweep a definition with a stray key, a bad ``type:``, or an
unimportable ``module:`` stays green until some deck YAML happens to
reference it — which means the failure surfaces at hardware bring-up
rather than in CI.
"""

from __future__ import annotations

from typing import Any

import pytest

from cubos.deck import LABWARE_YAML_ENTRY_MODELS
from cubos.deck.labware.definitions.registry import (
    get_labware_class,
    get_supported_definitions,
    load_definition_config,
)
from cubos.deck.labware.labware import Labware


# Minimal per-instance fields a deck YAML must supply for each entry type.
# Definitions deliberately omit these (measured coordinates, pickup heights),
# so the sweep injects placeholders rather than treating their absence as a
# definition defect.
_MINIMAL_INSTANCE_FIELDS: dict[str, dict[str, Any]] = {
    "well_plate": {
        "calibration": {
            "a1": {"x": 0.0, "y": 0.0, "z": -10.0},
            "a2": {"x": 9.0, "y": 0.0, "z": -10.0},
        },
    },
    "vial_grid": {
        "calibration": {
            "a1": {"x": 0.0, "y": 0.0, "z": -10.0},
            "a2": {"x": 9.0, "y": 0.0, "z": -10.0},
        },
    },
    "tip_rack": {
        "calibration": {
            "a1": {"x": 0.0, "y": 0.0, "z": -10.0},
            "a2": {"x": 9.0, "y": 0.0, "z": -10.0},
        },
        "pickup_z": -10.0,
    },
    "vial": {"location": {"x": 0.0, "y": 0.0, "z": -10.0}},
    "well_plate_holder": {"location": {"x": 0.0, "y": 0.0, "z": -10.0}},
    "vial_holder": {"location": {"x": 0.0, "y": 0.0, "z": -10.0}},
    "tip_disposal": {"location": {"x": 0.0, "y": 0.0, "z": -10.0}},
    "wall": {
        "corner_1": {"x": 0.0, "y": 0.0, "z": 0.0},
        "corner_2": {"x": 10.0, "y": 10.0, "z": 10.0},
    },
}


def test_registry_is_not_empty():
    """Guards against the parametrized sweeps below silently covering nothing."""
    assert get_supported_definitions()


@pytest.mark.parametrize("definition", get_supported_definitions())
def test_definition_config_and_registered_class_are_consistent(definition: str):
    cls = get_labware_class(definition)
    assert isinstance(cls, type)
    assert issubclass(cls, Labware), f"{definition} does not resolve to a Labware subclass"

    config = load_definition_config(definition)
    assert "type" in config, f"{definition} config is missing a `type:` key"
    entry_type = config["type"]
    assert entry_type in LABWARE_YAML_ENTRY_MODELS, (
        f"{definition} declares unknown type {entry_type!r}; "
        f"known types: {sorted(LABWARE_YAML_ENTRY_MODELS)}"
    )
    model = LABWARE_YAML_ENTRY_MODELS[entry_type]
    assert model.__name__ == f"{cls.__name__}YamlEntry", (
        f"{definition}: config type {entry_type!r} maps to "
        f"{model.__name__}, but the registry class is {cls.__name__}"
    )

    # Definition values take precedence over per-instance placeholders.
    payload = {**_MINIMAL_INSTANCE_FIELDS.get(entry_type, {}), **config}
    model.model_validate(payload)
