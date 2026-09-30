"""Tests for semantic protocol validation beyond static bounds."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cubos.gantry.instrument_mount import InstrumentedGantry
from cubos.deck.deck import Deck
from cubos.deck.labware.labware import Coordinate3D
from cubos.deck.labware.vial import Vial
from cubos.deck.labware.well_plate import WellPlate
from cubos.deck.loader import load_deck_from_yaml
from cubos.gantry.gantry_config import GantryConfig, GantryType, WorkingVolume
from cubos.protocol_engine.protocol import Protocol, ProtocolStep
from cubos.validation.protocol_semantics import validate_protocol_semantics

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _plate() -> WellPlate:
    return WellPlate(
        name="plate",
        model_name="test_plate",
        length=127.71,
        width=85.43,
        height=14.10,
        rows=1,
        columns=1,
        # Well-surface deck-frame Z = 14.10 (the calibration anchor); the
        # validator uses this as ref_z, not the plate's outer ``height``.
        wells={"A1": Coordinate3D(x=0.0, y=0.0, z=14.10)},
        capacity_ul=200.0,
        working_volume_ul=150.0,
    )


def _instrument(name: str = "asmi"):
    """Return an offline VernierASMI instance.

    A real subclass (not a MagicMock) is needed because
    ``_validate_asmi_indentation`` matches by type — the depth-bound
    check should fire whether the user names this instrument ``asmi``,
    ``force_sensor``, or anything else.
    """
    from cubos.instruments.asmi.vendors.vernier import VernierASMI

    instr = VernierASMI(name=name, offline=True)
    return instr


def _protocol(args: dict, command_name: str = "scan") -> Protocol:
    return Protocol([
        ProtocolStep(
            index=0,
            command_name=command_name,
            handler=lambda *a, **k: None,
            args=args,
        )
    ])


def _gantry_config(
    *,
    x_max: float = 300.0,
    y_max: float = 200.0,
    z_max: float = 100.0,
    safe_z: float | None = None,
) -> GantryConfig:
    return GantryConfig(
        serial_port="/dev/null",
        gantry_type=GantryType.CUB_XL,
        factory_z_travel_mm=z_max,
        working_volume=WorkingVolume(
            x_min=0.0, x_max=x_max,
            y_min=0.0, y_max=y_max,
            z_min=0.0, z_max=z_max,
        ),
        safe_z=safe_z,
    )


def _board_and_deck(instrument=None):
    instrumented_gantry = InstrumentedGantry(
        controller=MagicMock(),
        instruments={"asmi": instrument or _instrument("asmi")},
    )
    deck = Deck({"plate": _plate()})
    return instrumented_gantry, deck


def _scan_args(
    *,
    measurement_height: float = -1.0,
    interwell_scan_height: float = 10.0,
    indentation_limit_height: float | None = None,
    method: str = "indentation",
    method_kwargs: dict | None = None,
) -> dict:
    args = {
        "plate": "plate",
        "instrument": "asmi",
        "method": method,
        "measurement_height": measurement_height,
        "interwell_scan_height": interwell_scan_height,
        "method_kwargs": method_kwargs or {"step_size": 0.01},
    }
    if indentation_limit_height is not None:
        args["indentation_limit_height"] = indentation_limit_height
    return args


def _measure_args(
    *,
    measurement_height: float = -1.0,
    method: str = "measure",
    indentation_limit_height: float | None = None,
) -> dict:
    args = {
        "instrument": "asmi",
        "position": "plate.A1",
        "method": method,
        "measurement_height": measurement_height,
    }
    if indentation_limit_height is not None:
        args["indentation_limit_height"] = indentation_limit_height
    return args


def _move_step(*, position, instrument: str = "asmi", travel_z: float | None = None,
               command_name: str = "move") -> Protocol:
    args: dict = {"instrument": instrument, "position": position}
    if travel_z is not None:
        args["travel_z"] = travel_z
    return _protocol(args, command_name=command_name)


def test_asmi_indentation_within_z_bounds_passes():
    """Indentation deepest abs Z = ref_z + indentation_limit_height."""
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config(z_max=100.0)
    protocol = _protocol(_scan_args(indentation_limit_height=-5.0))

    assert validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry) == []


def test_asmi_indentation_below_z_min_violates():
    """ref_z=14.10 + indentation_limit_height=-20.0 = -5.90 < z_min=0."""
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config(z_max=100.0)
    protocol = _protocol(_scan_args(indentation_limit_height=-20.0))

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any("indentation deepest" in v.message for v in violations)


def test_asmi_indentation_depth_uses_well_z_not_plate_height():
    """The deepest absolute Z is ``well.z + indentation_limit_height`` —
    pin the formula in a fixture where ``well.z`` and ``plate.height``
    differ. A regression that read the plate's outer ``height`` instead
    of the calibrated well Z would compute a wrong (and possibly
    safe-looking) deepest-Z bound."""
    plate = WellPlate(
        name="plate",
        model_name="test_plate",
        length=127.71,
        width=85.43,
        height=14.10,                              # outer plate dimension
        rows=1,
        columns=1,
        # Holder-mounted plate: surface Z is 70.0, NOT plate.height.
        wells={"A1": Coordinate3D(x=0.0, y=0.0, z=70.0)},
        capacity_ul=200.0,
        working_volume_ul=150.0,
    )
    instrumented_gantry = InstrumentedGantry(
        controller=MagicMock(),
        instruments={"asmi": _instrument("asmi")},
    )
    deck = Deck({"plate": plate})
    gantry = _gantry_config(z_max=100.0)

    # measurement_height=-1.0 + indentation_limit_height=-69.5 → deepest =
    # well.z (70) + (-69.5) = 0.5  → above z_min=0, OK.
    # The legacy magnitude formula would give 14.10 - 1 - 69.5 = -56.4 — a
    # spurious violation.
    ok_protocol = _protocol(_scan_args(
        measurement_height=-1.0, indentation_limit_height=-69.5,
    ))
    assert validate_protocol_semantics(ok_protocol, instrumented_gantry, deck, gantry) == []

    # Push deepest below z_min using well.z=70 reference: -71 → 70-71 = -1.
    bad_protocol = _protocol(_scan_args(
        measurement_height=-1.0, indentation_limit_height=-71.0,
    ))
    violations = validate_protocol_semantics(bad_protocol, instrumented_gantry, deck, gantry)
    assert any("indentation deepest" in v.message for v in violations)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), True, "1.0"])
def test_asmi_indentation_non_finite_indentation_limit_height_violates(bad_value):
    """A non-finite ``indentation_limit_height`` must produce a per-field
    violation rather than silently bypassing the depth bound. NaN
    comparisons are False, so without an explicit finite gate the
    ``deepest_abs < z_min`` check would silently no-op."""
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config(z_max=100.0)
    protocol = _protocol(_scan_args(indentation_limit_height=bad_value))

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any(
        "indentation_limit_height must be a finite number" in v.message
        for v in violations
    ), violations


def test_asmi_indentation_depth_bound_matches_by_instrument_type_not_name():
    """A user-named VernierASMI (e.g. 'force_sensor') still triggers the
    depth-bound check — the validator matches on the driver type, not
    the instrument key in the instrumented_gantry config. This is the only thing
    protecting against driving the gantry through the deck on a
    misconfigured VernierASMI scan."""
    instrument = _instrument(name="force_sensor")
    instrumented_gantry = InstrumentedGantry(controller=MagicMock(), instruments={"force_sensor": instrument})
    deck = Deck({"plate": _plate()})
    gantry = _gantry_config(z_max=100.0)

    protocol = _protocol({
        "plate": "plate",
        "instrument": "force_sensor",
        "method": "indentation",
        "measurement_height": -1.0,
        "interwell_scan_height": 10.0,
        "indentation_limit_height": -20.0,
        "method_kwargs": {"step_size": 0.01},
    })

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any("indentation deepest" in v.message for v in violations)


def test_indentation_limit_height_above_measurement_violates():
    """indentation_limit_height must be at or below measurement_height —
    the descent has to go down."""
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config(z_max=100.0)
    protocol = _protocol(_scan_args(
        measurement_height=-1.0, indentation_limit_height=2.0,
    ))

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any(
        "indentation_limit_height" in v.message and "above" in v.message
        for v in violations
    )


def test_detect_surface_limit_is_surface_relative_not_compared_to_measurement():
    """With detect_surface the limit is anchored to the detected surface,
    so a value above measurement_height (different frame) is legal."""
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config(z_max=100.0)
    protocol = _protocol(_scan_args(
        measurement_height=-1.0, indentation_limit_height=-0.5,
        method_kwargs={"step_size": 0.01, "detect_surface": True},
    ))

    assert validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry) == []


def test_detect_surface_positive_limit_violates():
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config(z_max=100.0)
    protocol = _protocol(_scan_args(
        measurement_height=-1.0, indentation_limit_height=0.5,
        method_kwargs={"step_size": 0.01, "detect_surface": True},
    ))

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any(
        "indentation_limit_height" in v.message and "detect_surface" in v.message
        for v in violations
    )


def test_detect_surface_worst_case_depth_below_z_min_violates():
    """Detect mode bounds the worst case: surface found at the bottom of
    the search window, indentation continuing below it. ref_z=14.10,
    measurement_height=-1.0, default max travel 10.0, limit=-4.0 →
    deepest 14.10-1-10-4 = -0.9 < z_min=0."""
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config(z_max=100.0)

    without_detect = _protocol(_scan_args(
        measurement_height=-1.0, indentation_limit_height=-4.0,
    ))
    assert validate_protocol_semantics(
        without_detect, instrumented_gantry, deck, gantry,
    ) == []

    with_detect = _protocol(_scan_args(
        measurement_height=-1.0, indentation_limit_height=-4.0,
        method_kwargs={"step_size": 0.01, "detect_surface": True},
    ))
    violations = validate_protocol_semantics(
        with_detect, instrumented_gantry, deck, gantry,
    )
    assert any("indentation deepest" in v.message for v in violations)


def test_detect_surface_explicit_max_travel_drives_worst_case_depth():
    """A short explicit search window keeps the worst case above z_min."""
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config(z_max=100.0)
    protocol = _protocol(_scan_args(
        measurement_height=-1.0, indentation_limit_height=-4.0,
        method_kwargs={
            "step_size": 0.01,
            "detect_surface": True,
            "surface_search_max_travel": 5.0,
        },
    ))

    assert validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry) == []


@pytest.mark.parametrize("field", [
    "surface_search_step",
    "surface_force_threshold",
    "surface_search_max_travel",
])
def test_detect_surface_non_positive_search_params_violate(field):
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config(z_max=100.0)
    protocol = _protocol(_scan_args(
        measurement_height=-1.0, indentation_limit_height=-1.0,
        method_kwargs={"step_size": 0.01, "detect_surface": True, field: 0.0},
    ))

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any(
        field in v.message and "positive" in v.message for v in violations
    )


def test_scan_safe_approach_below_measurement_violates():
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config()
    protocol = _protocol(
        _scan_args(measurement_height=2.0, interwell_scan_height=1.0),
    )

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any("approach must be at or above" in v.message for v in violations)


def test_scan_approach_above_safe_z_violates():
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config(z_max=100.0, safe_z=20.0)
    protocol = _protocol(_scan_args())

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any("safe_z" in v.message for v in violations)


def test_valid_asmi_scan_semantics_pass():
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config(z_max=100.0, safe_z=85.0)
    protocol = _protocol(_scan_args(indentation_limit_height=-5.0))

    assert validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry) == []


def test_legacy_scan_travel_names_are_semantic_violations():
    instrumented_gantry, deck = _board_and_deck()
    protocol = _protocol(_scan_args(method_kwargs={
        "interwell_travel_height": 70.0,
        "step_size": 0.01,
    }))

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any("interwell_travel_height" in v.message for v in violations)


def test_scan_missing_measurement_height_violates():
    instrumented_gantry, deck = _board_and_deck()
    args = _scan_args()
    args.pop("measurement_height")
    protocol = _protocol(args)

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any("measurement_height" in v.message for v in violations)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), True, "1.0"])
def test_scan_non_finite_measurement_height_names_field(bad_value):
    instrumented_gantry, deck = _board_and_deck()
    protocol = _protocol(_scan_args(measurement_height=bad_value))

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any(
        "measurement_height must be a finite number" in v.message
        for v in violations
    ), violations


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), True, "1.0"])
def test_scan_non_finite_interwell_scan_height_names_field(bad_value):
    instrumented_gantry, deck = _board_and_deck()
    protocol = _protocol(_scan_args(interwell_scan_height=bad_value))

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any(
        "interwell_scan_height must be a finite number" in v.message
        for v in violations
    ), violations


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), True, "1.0"])
def test_measure_non_finite_measurement_height_names_field(bad_value):
    instrumented_gantry, deck = _board_and_deck()
    protocol = _protocol(
        _measure_args(measurement_height=bad_value), command_name="measure",
    )

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any(
        "measurement_height must be a finite number" in v.message
        for v in violations
    ), violations


def test_scan_missing_interwell_scan_height_violates():
    instrumented_gantry, deck = _board_and_deck()
    args = _scan_args()
    args.pop("interwell_scan_height")
    protocol = _protocol(args)

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any("interwell_scan_height" in v.message for v in violations)


def test_legacy_asmi_z_limit_is_semantic_violation():
    instrumented_gantry, deck = _board_and_deck()
    protocol = _protocol(_scan_args(method_kwargs={
        "z_limit": 70.0, "step_size": 0.01,
    }))

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any("`z_limit` is no longer supported" in v.message for v in violations)


def test_measure_missing_measurement_height_violates():
    instrumented_gantry, deck = _board_and_deck()
    args = _measure_args()
    args.pop("measurement_height")
    protocol = _protocol(args, command_name="measure")

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any("measurement_height" in v.message for v in violations)


def test_measure_with_command_measurement_height_passes():
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config(z_max=100.0, safe_z=85.0)
    protocol = _protocol(_measure_args(), command_name="measure")

    assert validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry) == []


def test_measure_indentation_below_z_min_violates():
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config(z_max=100.0)
    protocol = _protocol(
        _measure_args(
            method="indentation",
            indentation_limit_height=-20.0,
        ),
        command_name="measure",
    )

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any("indentation deepest" in v.message for v in violations), violations


def test_measure_indentation_limit_height_above_measurement_violates():
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config(z_max=100.0)
    protocol = _protocol(
        _measure_args(
            measurement_height=-1.0,
            method="indentation",
            indentation_limit_height=2.0,
        ),
        command_name="measure",
    )

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any(
        "indentation_limit_height" in v.message and "above" in v.message
        for v in violations
    ), violations


def test_measure_unknown_position_emits_violation():
    instrumented_gantry, deck = _board_and_deck()
    args = _measure_args()
    args["position"] = "plate.Z99"
    protocol = _protocol(args, command_name="measure")

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any("cannot be resolved" in v.message for v in violations), violations


def test_measure_unknown_instrument_emits_violation():
    instrumented_gantry, deck = _board_and_deck()
    args = _measure_args()
    args["instrument"] = "missing"
    protocol = _protocol(args, command_name="measure")

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any("unknown instrument" in v.message for v in violations), violations


def test_scan_unknown_plate_emits_violation():
    instrumented_gantry, deck = _board_and_deck()
    args = _scan_args()
    args["plate"] = "missing_plate"
    protocol = _protocol(args)

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any("cannot be resolved" in v.message for v in violations), violations


def test_scan_unknown_instrument_emits_violation():
    instrumented_gantry, deck = _board_and_deck()
    args = _scan_args()
    args["instrument"] = "missing"
    protocol = _protocol(args)

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any("unknown instrument" in v.message for v in violations), violations


# ─── deck-reference checks for `cure`/`cap`/`decap`/`image_well` ────────────
#
# Regression coverage for #327: these commands each resolve a deck
# position/vial through the same `KeyError`-raising lookups as
# `measure`/`scan`, but previously fell through `validate_protocol_semantics`'
# catch-all `else` branch, which only validates pipette commands -- an
# unknown well/vial reference went uncaught until an actual (potentially
# hardware-connected) run.


def _vial(name: str = "vial") -> Vial:
    return Vial(
        name=name,
        location=Coordinate3D(x=50.0, y=50.0, z=20.0),
        capacity_ul=2000.0,
        working_volume_ul=1800.0,
    )


def _board_and_deck_with_vial(instrument=None):
    instrumented_gantry = InstrumentedGantry(
        controller=MagicMock(),
        instruments={"asmi": instrument or _instrument("asmi")},
    )
    deck = Deck({"plate": _plate(), "vial": _vial()})
    return instrumented_gantry, deck


def test_cure_unknown_position_emits_violation():
    instrumented_gantry, deck = _board_and_deck()
    args = {
        "instrument": "asmi",
        "position": "plate.Z99",
        "measurement_height": 0.0,
        "exposure_time": 1.0,
    }
    protocol = _protocol(args, command_name="cure")

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any("cannot be resolved" in v.message for v in violations), violations


def test_cure_known_position_passes():
    instrumented_gantry, deck = _board_and_deck()
    args = {
        "instrument": "asmi",
        "position": "plate.A1",
        "measurement_height": 0.0,
        "exposure_time": 1.0,
    }
    protocol = _protocol(args, command_name="cure")

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert violations == []


@pytest.mark.parametrize("command_name", ["cap", "decap"])
def test_cap_decap_unknown_vial_emits_violation(command_name):
    instrumented_gantry, deck = _board_and_deck_with_vial()
    args = {"instrument": "asmi", "vial": "missing_vial"}
    protocol = _protocol(args, command_name=command_name)

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any("cannot be resolved" in v.message for v in violations), violations


@pytest.mark.parametrize("command_name", ["cap", "decap"])
def test_cap_decap_known_vial_passes(command_name):
    instrumented_gantry, deck = _board_and_deck_with_vial()
    args = {"instrument": "asmi", "vial": "vial"}
    protocol = _protocol(args, command_name=command_name)

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert violations == []


def test_image_well_unknown_well_emits_violation():
    instrumented_gantry, deck = _board_and_deck()
    args = {"camera": "asmi", "well": "plate.Z99", "image_height": 0.0}
    protocol = _protocol(args, command_name="image_well")

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any("cannot be resolved" in v.message for v in violations), violations


def test_image_well_known_well_passes():
    instrumented_gantry, deck = _board_and_deck()
    args = {"camera": "asmi", "well": "plate.A1", "image_height": 0.0}
    protocol = _protocol(args, command_name="image_well")

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert violations == []


def test_cure_unknown_instrument_emits_violation():
    instrumented_gantry, deck = _board_and_deck()
    args = {
        "instrument": "missing",
        "position": "plate.A1",
        "measurement_height": 0.0,
        "exposure_time": 1.0,
    }
    protocol = _protocol(args, command_name="cure")

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any("unknown instrument" in v.message for v in violations), violations


@pytest.mark.parametrize("command_name", ["cap", "decap"])
def test_cap_decap_unknown_instrument_emits_violation(command_name):
    instrumented_gantry, deck = _board_and_deck_with_vial()
    args = {"instrument": "missing", "vial": "vial"}
    protocol = _protocol(args, command_name=command_name)

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any("unknown instrument" in v.message for v in violations), violations


def test_image_well_unknown_camera_emits_violation():
    instrumented_gantry, deck = _board_and_deck()
    args = {"camera": "missing", "well": "plate.A1", "image_height": 0.0}
    protocol = _protocol(args, command_name="image_well")

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any("unknown camera" in v.message for v in violations), violations


def test_image_well_unknown_lights_instrument_emits_violation():
    instrumented_gantry, deck = _board_and_deck()
    args = {
        "camera": "asmi",
        "well": "plate.A1",
        "image_height": 0.0,
        "lights": "missing",
    }
    protocol = _protocol(args, command_name="image_well")

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert any("unknown lights instrument" in v.message for v in violations), violations


def test_image_well_omitted_lights_passes():
    instrumented_gantry, deck = _board_and_deck()
    args = {"camera": "asmi", "well": "plate.A1", "image_height": 0.0, "lights": None}
    protocol = _protocol(args, command_name="image_well")

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, _gantry_config())

    assert violations == []


@pytest.mark.parametrize("command_name,args", [
    ("pause", {"seconds": 0.1}),
    ("breakpoint", {"message": "continue"}),
])
def test_no_motion_commands_pass_semantic_validation(command_name, args):
    instrumented_gantry, deck = _board_and_deck()
    protocol = _protocol(args, command_name=command_name)

    assert validate_protocol_semantics(
        protocol, instrumented_gantry, deck, _gantry_config(),
    ) == []


# ─── working-volume bound checks for `move` ──────────────────────────────────


def test_move_target_in_bounds_with_zero_offsets_passes():
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config()
    protocol = _move_step(position=(150.0, 100.0, 50.0))

    assert validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry) == []


def test_move_x_offset_is_subtracted_so_offset_x_can_drive_violation():
    """Instrument offset_x must be SUBTRACTED from user X to get gantry X.

    A naive sign error (adding instead of subtracting) would not catch this:
    user x=290 with offset_x=20 → gantry x=270 (in-bounds), and would only
    appear out-of-bounds if the offset were added (290+20=310 > 300).
    Conversely, a user x=10 with offset_x=-300 must place gantry x at 310,
    which violates x_max=300.
    """
    instr = _instrument("asmi")
    instr.offset_x = -300.0
    instrumented_gantry, deck = _board_and_deck(instr)
    gantry = _gantry_config(x_max=300.0)
    protocol = _move_step(position=(10.0, 100.0, 50.0))

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any("x" in v.message and "310" in v.message for v in violations), violations


def test_move_y_offset_is_subtracted():
    instr = _instrument("asmi")
    instr.offset_y = -250.0
    instrumented_gantry, deck = _board_and_deck(instr)
    gantry = _gantry_config(y_max=200.0)
    protocol = _move_step(position=(100.0, 10.0, 50.0))

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any("y" in v.message for v in violations), violations


def test_move_depth_is_added_to_z():
    """instr.depth must be ADDED to user Z to get gantry Z.

    User z=80 with depth=30 → gantry z=110, which violates z_max=100.
    """
    instr = _instrument("asmi")
    instr.depth = 30.0
    instrumented_gantry, deck = _board_and_deck(instr)
    gantry = _gantry_config(z_max=100.0)
    protocol = _move_step(position=(100.0, 100.0, 80.0))

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any("z" in v.message and "110" in v.message for v in violations), violations


def test_move_target_at_volume_boundary_is_valid():
    """Inclusive bounds: value == low and value == high pass."""
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config(x_max=300.0, y_max=200.0, z_max=100.0)
    protocol = _move_step(position=(300.0, 200.0, 100.0))
    assert validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry) == []
    protocol = _move_step(position=(0.0, 0.0, 0.0))
    assert validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry) == []


def test_move_travel_z_violation_independent_of_target():
    instr = _instrument("asmi")
    instr.depth = 0.0
    instrumented_gantry, deck = _board_and_deck(instr)
    gantry = _gantry_config(z_max=100.0)
    protocol = _move_step(position=(100.0, 100.0, 50.0), travel_z=150.0)

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any("travel_z" in v.message for v in violations), violations


def test_move_to_unknown_named_position_emits_violation():
    """Bare-except previously hid this case; now resolve failures must surface."""
    instrumented_gantry, deck = _board_and_deck()
    gantry = _gantry_config()
    protocol = _move_step(position="does_not_exist")

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any("cannot be resolved" in v.message for v in violations), violations


# ─── working-volume bound checks for `scan` ──────────────────────────────────


def test_scan_well_offset_x_drives_volume_violation():
    instr = _instrument("asmi")
    instr.offset_x = -350.0
    instrumented_gantry, deck = _board_and_deck(instr)
    gantry = _gantry_config(x_max=300.0)
    protocol = _protocol(_scan_args())

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any("x" in v.message for v in violations), violations


def test_scan_depth_drives_z_violation():
    """height=14.10 + measurement_height=80.0 + depth=30.0 = 124.10 > z_max=100."""
    instr = _instrument("asmi")
    instr.depth = 30.0
    instrumented_gantry, deck = _board_and_deck(instr)
    gantry = _gantry_config(z_max=100.0)
    protocol = _protocol(
        _scan_args(measurement_height=80.0, interwell_scan_height=85.0),
    )

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any("z" in v.message for v in violations), violations


def test_nested_plate_scan_target_passes_semantic_validation():
    deck = load_deck_from_yaml(FIXTURES / "configs/deck/mock_nested_plate_deck.yaml")
    instrumented_gantry = InstrumentedGantry(
        controller=MagicMock(),
        instruments={"uv": _instrument("uv")},
    )
    gantry = _gantry_config(x_max=300.0, y_max=300.0, z_max=100.0, safe_z=95.0)
    protocol = _protocol({
        "plate": "plate_holder.plate",
        "instrument": "uv",
        "method": "measure",
        "measurement_height": 1.0,
        "interwell_scan_height": 5.0,
        "method_kwargs": {"intensity": 1, "exposure_time": 1.0},
    })

    assert validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry) == []


def test_nested_plate_scan_target_uses_nested_surface_z_for_safe_z_validation():
    deck = load_deck_from_yaml(FIXTURES / "configs/deck/mock_nested_plate_deck.yaml")
    instrumented_gantry = InstrumentedGantry(
        controller=MagicMock(),
        instruments={"uv": _instrument("uv")},
    )
    gantry = _gantry_config(x_max=300.0, y_max=300.0, z_max=100.0, safe_z=94.0)
    protocol = _protocol({
        "plate": "plate_holder.plate",
        "instrument": "uv",
        "method": "measure",
        "measurement_height": 1.0,
        "interwell_scan_height": 5.0,
        "method_kwargs": {"intensity": 1, "exposure_time": 1.0},
    })

    violations = validate_protocol_semantics(protocol, instrumented_gantry, deck, gantry)

    assert any("94.500 = 89.5+5.0" in v.message for v in violations), violations
