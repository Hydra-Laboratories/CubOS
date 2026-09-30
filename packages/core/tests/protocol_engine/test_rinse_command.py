"""Tests for the potentiostat rinse protocol command."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cubos.deck.deck import Deck
from cubos.deck.labware.labware import Coordinate3D
from cubos.deck.labware.vial import Vial
from cubos.instruments.potentiostat.interface import PotentiostatInstrument
from cubos.instruments.potentiostat.vendors.emstat import EmstatPotentiostat
from cubos.protocol_engine.commands.rinse import rinse
from cubos.protocol_engine.errors import ProtocolExecutionError
from cubos.protocol_engine.runtime import ProtocolContext
from cubos.gantry.gantry_config import GantryConfig, GantryType, WorkingVolume
from cubos.gantry.instrument_mount import InstrumentedGantry
from cubos.protocol_engine.protocol import Protocol, ProtocolStep
from cubos.validation.protocol_semantics import validate_protocol_semantics
from cubos.validation.bounds import collect_protocol_motion_targets
from cubos.protocol_engine.setup_validator import run_setup_validation


def _context(*, instrument=None, labware=None):
    instrument = instrument or MagicMock(spec=PotentiostatInstrument)
    instrument.name = "potentiostat"
    board = MagicMock()
    board.instruments = {"potentiostat": instrument}
    board.safe_z = 80.0
    vial = Vial(
        name="rinse_vial",
        location=Coordinate3D(x=10.0, y=20.0, z=30.0),
        capacity_ul=1000.0,
        working_volume_ul=500.0,
    )
    return ProtocolContext(
        gantry=board,
        deck=Deck({"rinse_vial": labware or vial}),
    )


def test_rinse_dips_exactly_three_times_and_retracts_after_each():
    context = _context()

    rinse(context, instrument="potentiostat", vial="rinse_vial", measurement_height=-5.0)

    assert context.gantry.move_to_labware.call_count == 3
    assert context.gantry.move.call_count == 6
    assert context.gantry.move.call_args_list == [
        (("potentiostat", (10.0, 20.0, 25.0)), {}),
        (("potentiostat", (10.0, 20.0, 80.0)), {}),
        (("potentiostat", (10.0, 20.0, 25.0)), {}),
        (("potentiostat", (10.0, 20.0, 80.0)), {}),
        (("potentiostat", (10.0, 20.0, 25.0)), {}),
        (("potentiostat", (10.0, 20.0, 80.0)), {}),
    ]


@pytest.mark.parametrize("height", [0.0, 1.0, float("nan"), float("inf")])
def test_rinse_requires_negative_finite_immersion_height(height):
    context = _context()

    with pytest.raises(ProtocolExecutionError, match="finite negative"):
        rinse(context, instrument="potentiostat", vial="rinse_vial", measurement_height=height)

    context.gantry.move_to_labware.assert_not_called()
    context.gantry.move.assert_not_called()


def test_rinse_rejects_non_potentiostat_before_motion():
    instrument = MagicMock()
    context = _context(instrument=instrument)

    with pytest.raises(ProtocolExecutionError, match="not a potentiostat"):
        rinse(context, instrument="potentiostat", vial="rinse_vial", measurement_height=-1.0)

    context.gantry.move_to_labware.assert_not_called()


def test_rinse_rejects_unknown_instrument_before_motion():
    context = _context()
    with pytest.raises(ProtocolExecutionError, match="unknown instrument"):
        rinse(context, instrument="missing", vial="rinse_vial", measurement_height=-1.0)
    context.gantry.move_to_labware.assert_not_called()


def test_rinse_rejects_missing_vial_before_motion():
    context = _context()
    context.deck = MagicMock()
    context.deck.resolve_labware.side_effect = KeyError("missing")
    with pytest.raises(ProtocolExecutionError, match="cannot resolve vial"):
        rinse(context, instrument="potentiostat", vial="missing", measurement_height=-1.0)
    context.gantry.move_to_labware.assert_not_called()


def test_rinse_rejects_capped_vial_before_motion():
    context = _context(labware=Vial(
        name="rinse_vial", location=Coordinate3D(x=10.0, y=20.0, z=30.0),
        capacity_ul=1000.0, working_volume_ul=500.0, capped=True,
    ))
    with pytest.raises(ProtocolExecutionError, match="capped"):
        rinse(context, instrument="potentiostat", vial="rinse_vial", measurement_height=-1.0)
    context.gantry.move_to_labware.assert_not_called()


def test_rinse_uses_durable_uncapped_preflight(monkeypatch):
    context = _context()
    context.fluid_state_id = "state-1"
    context.data_store = MagicMock()
    context.campaign_id = "campaign-1"
    preflight = MagicMock()
    monkeypatch.setattr(
        "cubos.protocol_engine.commands.rinse.require_uncapped", preflight,
    )
    rinse(context, instrument="potentiostat", vial="rinse_vial", measurement_height=-1.0)
    preflight.assert_called_once_with(context, ["rinse_vial"], command_label="rinse")


def test_rinse_rejects_missing_or_invalid_safe_z_before_motion():
    for safe_z in (None, float("nan"), 30.0):
        context = _context()
        context.gantry.safe_z = safe_z
        with pytest.raises(ProtocolExecutionError):
            rinse(context, instrument="potentiostat", vial="rinse_vial", measurement_height=-1.0)
        context.gantry.move_to_labware.assert_not_called()


def test_rinse_wraps_motion_value_error_without_retrying():
    context = _context()
    context.gantry.move_to_labware.side_effect = ValueError("controller rejected move")
    with pytest.raises(ProtocolExecutionError, match="controller rejected move"):
        rinse(context, instrument="potentiostat", vial="rinse_vial", measurement_height=-1.0)
    assert context.gantry.move_to_labware.call_count == 1


def test_rinse_rejects_non_vial_target_before_motion():
    from cubos.deck.labware.well_plate import WellPlate

    plate = WellPlate(
        name="plate",
        model_name="plate",
        rows=1,
        columns=1,
        wells={"A1": Coordinate3D(x=10.0, y=20.0, z=30.0)},
        capacity_ul=100.0,
        working_volume_ul=50.0,
    )
    context = _context(labware=plate)

    with pytest.raises(ProtocolExecutionError, match="single Vial"):
        rinse(context, instrument="potentiostat", vial="rinse_vial", measurement_height=-1.0)

    context.gantry.move_to_labware.assert_not_called()


def _semantic_inputs(*, safe_z=80.0, vial=None, instrument=None):
    instrument = instrument or EmstatPotentiostat(name="potentiostat", offline=True)
    vial = vial or _context().deck["rinse_vial"]
    board = InstrumentedGantry(MagicMock(), {"potentiostat": instrument})
    deck = Deck({"rinse_vial": vial})
    config = GantryConfig(
        serial_port="/dev/null", gantry_type=GantryType.CUB_XL,
        factory_z_travel_mm=100.0,
        working_volume=WorkingVolume(0.0, 100.0, 0.0, 100.0, 0.0, 100.0),
        safe_z=safe_z,
    )
    return board, deck, config


def _semantic_protocol(**args):
    command_args = {
        "instrument": "potentiostat", "vial": "rinse_vial",
        "measurement_height": -5.0,
    }
    command_args.update(args)
    return Protocol([ProtocolStep(
        index=0, command_name="rinse", handler=rinse, args=command_args,
    )])


def test_rinse_semantics_accept_valid_setup():
    board, deck, config = _semantic_inputs()
    assert validate_protocol_semantics(_semantic_protocol(), board, deck, config) == []


def test_rinse_semantics_reject_safe_z_at_or_below_rim():
    board, deck, config = _semantic_inputs(safe_z=30.0)
    violations = validate_protocol_semantics(_semantic_protocol(), board, deck, config)
    assert any("above vial rim" in item.message for item in violations)


def test_rinse_semantics_rejects_capped_vial_only_at_runtime_boundary():
    capped = Vial(
        name="rinse_vial", location=Coordinate3D(x=10.0, y=20.0, z=30.0),
        capacity_ul=1000.0, working_volume_ul=500.0, capped=True,
    )
    board, deck, config = _semantic_inputs(vial=capped)
    assert validate_protocol_semantics(_semantic_protocol(), board, deck, config) == []


def test_rinse_bounds_collects_safe_and_immersion_targets():
    board, deck, config = _semantic_inputs()
    targets = collect_protocol_motion_targets(config, _semantic_protocol(), deck)
    assert [(target.position_id, target.z) for target in targets] == [
        ("location.safe_z", 80.0), ("location.action_z", 25.0),
    ]


@pytest.mark.parametrize("args, needle", [
    ({"instrument": "missing"}, "unknown instrument"),
    ({"measurement_height": "bad"}, "measurement_height"),
    ({"measurement_height": 0.0}, "must be negative"),
    ({"vial": "missing"}, "cannot be resolved"),
])
def test_rinse_semantics_reject_invalid_arguments(args, needle):
    board, deck, config = _semantic_inputs()
    violations = validate_protocol_semantics(
        _semantic_protocol(**args), board, deck, config,
    )
    assert any(needle in item.message for item in violations)


def test_rinse_semantics_reject_non_potentiostat_and_non_vial():
    from cubos.instruments.asmi.vendors.vernier import VernierASMI
    board, deck, config = _semantic_inputs(
        instrument=VernierASMI(name="not_potentiostat", offline=True),
    )
    violations = validate_protocol_semantics(
        _semantic_protocol(), board, deck, config,
    )
    assert any("PotentiostatInstrument" in item.message for item in violations)

    from cubos.deck.labware.well_plate import WellPlate
    plate = WellPlate(
        name="rinse_vial", model_name="plate", rows=1, columns=1,
        wells={"A1": Coordinate3D(x=10.0, y=20.0, z=30.0)},
        capacity_ul=100.0, working_volume_ul=50.0,
    )
    board, deck, config = _semantic_inputs(vial=plate)
    violations = validate_protocol_semantics(
        _semantic_protocol(), board, deck, config,
    )
    assert any("single Vial" in item.message for item in violations)


def test_rinse_semantics_reject_action_above_safe_z():
    board, deck, config = _semantic_inputs(safe_z=31.0)
    violations = validate_protocol_semantics(
        _semantic_protocol(measurement_height=2.0), board, deck, config,
    )
    assert any("above safe_z" in item.message for item in violations)


def test_sterling_rinse_setup_validates_offline():
    root = Path(__file__).resolve().parents[2]
    result = run_setup_validation(
        root / "configs/gantry/cub_xl_sterling.yaml",
        root / "configs/deck/sterling_deck.yaml",
        root / "configs/protocol/sterling/rinse.yaml",
    )
    assert result.passed, result.output


@pytest.mark.parametrize("status", ["uncapped", "capped", "reconciliation_required"])
def test_rinse_obeys_durable_cap_state(status):
    context = _context()
    context.deck["rinse_vial"].capped = True
    context.fluid_state_id = "state-1"
    context.data_store = MagicMock()
    context.data_store.get_cap_state.return_value = status
    if status == "uncapped":
        rinse(context, "potentiostat", "rinse_vial", -5.0)
        assert context.gantry.move.call_count == 6
    else:
        with pytest.raises(ProtocolExecutionError, match="durable cap state"):
            rinse(context, "potentiostat", "rinse_vial", -5.0)
        context.gantry.move_to_labware.assert_not_called()
        context.gantry.move.assert_not_called()


@pytest.mark.parametrize("failed_move", [1, 2, 3, 4, 5, 6])
def test_rinse_stops_on_any_dip_or_withdrawal_failure(failed_move):
    context = _context()
    failure = RuntimeError("motion interrupted")
    context.gantry.move.side_effect = [None] * (failed_move - 1) + [failure]
    with pytest.raises(RuntimeError) as error:
        rinse(context, "potentiostat", "rinse_vial", -5.0)
    assert error.value is failure
    assert context.gantry.move.call_count == failed_move
    assert context.gantry.move_to_labware.call_count == (failed_move + 1) // 2
