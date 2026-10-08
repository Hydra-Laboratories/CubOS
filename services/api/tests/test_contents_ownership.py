"""Concurrency tests for the persistent contents ownership boundary."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from cubos_api.services.contents_ownership import (
    ContentsOwnership,
    ContentsOwnershipError,
)
from cubos_api.services import run_manager


def test_manual_transaction_blocks_while_stateful_run_owns_contents():
    ownership = ContentsOwnership()
    ownership.claim_run("run-1")
    ownership.bind_fluid_state("run-1", 17)

    with pytest.raises(ContentsOwnershipError, match="run 'run-1'"):
        with ownership.manual_transaction():
            pass

    ownership.bind_campaign("run-1", 23)
    assert ownership.snapshot().kind == "campaign"
    assert ownership.snapshot().campaign_id == 23
    ownership.release("other-run")
    assert ownership.snapshot() is not None
    ownership.release("run-1")
    assert ownership.snapshot() is None


def test_manual_transaction_serializes_and_run_cannot_slip_between_check_and_commit():
    ownership = ContentsOwnership()
    entered = threading.Event()
    continue_edit = threading.Event()
    edit_errors: list[Exception] = []
    run_claimed = threading.Event()

    def edit() -> None:
        try:
            with ownership.manual_transaction():
                entered.set()
                assert continue_edit.wait(timeout=2)
        except Exception as exc:  # pragma: no cover - assertion below reports it
            edit_errors.append(exc)

    def run() -> None:
        assert entered.wait(timeout=2)
        ownership.claim_run("run-2")
        run_claimed.set()

    edit_thread = threading.Thread(target=edit)
    run_thread = threading.Thread(target=run)
    edit_thread.start()
    run_thread.start()
    assert not run_claimed.wait(timeout=0.1)
    continue_edit.set()
    edit_thread.join(timeout=2)
    run_thread.join(timeout=2)

    assert edit_errors == []
    assert run_claimed.is_set()
    ownership.release("run-2")
    assert ownership.snapshot() is None


def test_legacy_stateless_workflow_still_owns_physical_station():
    ownership = ContentsOwnership()
    ownership.claim_run("legacy-run")
    assert ownership.snapshot().fluid_state_id is None
    with pytest.raises(ContentsOwnershipError, match="legacy-run"):
        with ownership.manual_transaction():
            pass
    ownership.release("legacy-run")
    with ownership.manual_transaction():
        pass


def test_bind_requires_matching_run_and_release_is_idempotent():
    ownership = ContentsOwnership()
    ownership.claim_run("run-1")

    with pytest.raises(ContentsOwnershipError, match="does not own"):
        ownership.bind_fluid_state("run-2", 4)
    with pytest.raises(ContentsOwnershipError, match="does not own"):
        ownership.bind_campaign("run-2", 9)

    ownership.release("run-2")
    ownership.release("run-1")
    ownership.release("run-1")
    assert ownership.snapshot() is None


class _FakeDeck:
    def resolve_labware_target(self, name):
        return SimpleNamespace(labware_key=name.split(".")[0], location_id=name.split(".")[-1])


class _FakeStore:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get_fluid_snapshot(self, state_id):
        return self.snapshot

    def close(self):
        pass


def _fake_protocol():
    return SimpleNamespace(
        steps=[
            SimpleNamespace(
                command_name="transfer",
                args={"source": "src.A1", "destination": "dst.A1", "volume_ul": 10},
            )
        ]
    )


def test_preflight_rejects_unknown_required_target_but_ignores_unrelated_unknown(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(run_manager, "load_protocol_from_yaml", lambda path: _fake_protocol())
    monkeypatch.setattr(
        run_manager,
        "DataStore",
        lambda path: _FakeStore(
            {
                "containers": [
                    {
                        "labware_key": "src",
                        "location_id": "A1",
                        "current_volume_ul": 20,
                        "volume_known": True,
                    },
                    {
                        "labware_key": "unrelated",
                        "location_id": "A1",
                        "current_volume_ul": 0,
                        "volume_known": False,
                    },
                ]
            }
        ),
    )
    with pytest.raises(run_manager.RunPolicyError, match="dst.A1"):
        run_manager._validate_fluid_state_preflight(
            state_id=1,
            deck=_FakeDeck(),
            protocol_yaml="protocol: []",
            db_path=tmp_path / "state.db",
        )


@pytest.mark.parametrize(
    "message",
    ["source needs 10 uL but only 2 uL", "destination would exceed working volume"],
)
def test_preflight_surfaces_existing_shortage_or_overflow_before_execution(
    monkeypatch, tmp_path, message
):
    monkeypatch.setattr(run_manager, "load_protocol_from_yaml", lambda path: _fake_protocol())
    monkeypatch.setattr(
        run_manager,
        "DataStore",
        lambda path: _FakeStore(
            {
                "containers": [
                    {"labware_key": "src", "location_id": "A1", "current_volume_ul": 20, "volume_known": True},
                    {"labware_key": "dst", "location_id": "A1", "current_volume_ul": 0, "volume_known": True},
                ]
            }
        ),
    )
    violation = SimpleNamespace(step_index=0, command_name="transfer", message=message)
    monkeypatch.setattr(run_manager, "validate_protocol_fluid_volumes", lambda *args: [violation])

    with pytest.raises(run_manager.RunPolicyError, match=message):
        run_manager._validate_fluid_state_preflight(
            state_id=1,
            deck=_FakeDeck(),
            protocol_yaml="protocol: []",
            db_path=tmp_path / "state.db",
        )


@pytest.mark.parametrize("command", ["rinse_well", "flush_pipette", "purge_pipette", "clear_well"])
def test_preflight_rejects_dynamic_waste_selection_before_connect(
    monkeypatch, tmp_path, command
):
    monkeypatch.setattr(
        run_manager,
        "load_protocol_from_yaml",
        lambda path: SimpleNamespace(
            # Explicit source isolates the dynamic-waste guard for each command.
            steps=[SimpleNamespace(command_name=command, args={"source": "src.A1"})]
        ),
    )
    with pytest.raises(run_manager.RunPolicyError, match="dynamic waste selection"):
        run_manager._validate_fluid_state_preflight(
            state_id=1,
            deck=_FakeDeck(),
            protocol_yaml="protocol: []",
            db_path=tmp_path / "state.db",
        )


def test_preflight_rejects_dynamic_stock_selection_before_connect(monkeypatch, tmp_path):
    monkeypatch.setattr(
        run_manager,
        "load_protocol_from_yaml",
        lambda path: SimpleNamespace(
            steps=[
                SimpleNamespace(
                    command_name="flush_pipette",
                    args={"solution": "water", "waste": "waste.A1"},
                )
            ]
        ),
    )
    with pytest.raises(run_manager.RunPolicyError, match="dynamic stock selection"):
        run_manager._validate_fluid_state_preflight(
            state_id=1,
            deck=_FakeDeck(),
            protocol_yaml="protocol: []",
            db_path=tmp_path / "state.db",
        )


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("rinse_well", {"well": "well.A1", "source": "src.A1", "waste": "waste.A1"}),
        ("flush_pipette", {"source": "src.A1", "waste": "waste.A1"}),
        ("purge_pipette", {"source": "src.A1", "waste": "waste.A1"}),
        ("clear_well", {"well": "well.A1", "waste": "waste.A1"}),
    ],
)
def test_preflight_checks_explicit_waste_for_every_liquid_command(
    monkeypatch, tmp_path, command, args
):
    monkeypatch.setattr(
        run_manager,
        "load_protocol_from_yaml",
        lambda path: SimpleNamespace(
            steps=[SimpleNamespace(command_name=command, args=args)]
        ),
    )
    monkeypatch.setattr(
        run_manager,
        "DataStore",
        lambda path: _FakeStore(
            {
                "containers": [
                    {"labware_key": "src", "location_id": "A1", "current_volume_ul": 20, "volume_known": True},
                    {"labware_key": "well", "location_id": "A1", "current_volume_ul": 20, "volume_known": True},
                ]
            }
        ),
    )
    with pytest.raises(run_manager.RunPolicyError, match="waste.A1"):
        run_manager._validate_fluid_state_preflight(
            state_id=1,
            deck=_FakeDeck(),
            protocol_yaml="protocol: []",
            db_path=tmp_path / "state.db",
        )
