"""Asynchronous, addressable CubOS protocol runs under the versioned API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pathlib import Path
from typing import Any
from pydantic import Field
import tempfile
import logging
from cubos.data import DataStore
from cubos.protocol_engine.setup_validator import run_setup_validation
from fastapi.responses import FileResponse
from cubos.data import CapStateError, FluidStateError, TipStateError
from cubos.protocol_engine.loader import load_protocol_from_yaml
from cubos.protocol_engine.registry import CommandRegistry
from cubos.gantry.session import (
    GantryNotConnectedError,
    InterruptFeedHoldTimeoutError,
)

from cubos_api.models.runs import (
    PlanStep,
    RunArtifactsResponse,
    RunEventsResponse,
    RunPlanResponse,
    RunRecord,
    RunSubmission,
)
from cubos_api.models.state import RunStateSelection
from cubos_api.services.run_manager import (
    RunConflictError,
    RunPolicyError,
    get_run_manager,
)
from cubos_api.services.state_errors import map_state_exception


router = APIRouter(prefix="/api/v1/runs", tags=["cubos-runs-v1"])


def _describe(registry: CommandRegistry, command: str, args: dict) -> str:
    """Render one step's args via its registered summary formatter."""
    try:
        return registry.get(command).describe(args)
    except KeyError:
        # A compiled protocol can only contain registered commands, so this
        # is unreachable in practice; degrade to the command name rather than
        # failing the whole plan if it ever is not.
        return command


def _jsonable_args(args: dict) -> dict:
    return {key: _jsonable(value) for key, value in args.items()}


def _jsonable(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


class RunValidationRequest(RunSubmission):
    tip_snapshot: dict[str, Any] | None = Field(default=None)


@router.post("/validate")
def validate_run(body: RunValidationRequest) -> dict:
    manager = get_run_manager()
    try:
        gantry_yaml, deck_yaml, protocol_yaml = manager._resolve_bundle(body)
        manager._validate_bundle(gantry_yaml, deck_yaml, protocol_yaml)
        tip_snapshot = None
        if body.mock_mode and body.state is not None:
            raise ValueError("Mock runs cannot modify durable physical state")
        if body.state is not None:
            if body.state.initial_state is not None:
                raise ValueError("Create and seed the durable state before validating a stateful run")
            state_id = manager._resolve_run_state(deck_yaml, body.state)
            store = DataStore(manager.settings.data_db_path)
            try:
                tip_snapshot = store.get_tip_snapshot(state_id)
            finally:
                store.close()
        if body.tip_snapshot is not None:
            virtual_state_id = body.tip_snapshot.get("fluid_state_id")
            if tip_snapshot is None and virtual_state_id is not None:
                state_id = manager._resolve_run_state(deck_yaml, RunStateSelection(fluid_state_id=virtual_state_id))
                store = DataStore(manager.settings.data_db_path)
                try:
                    tip_snapshot = store.get_tip_snapshot(state_id)
                finally:
                    store.close()
            if tip_snapshot is None and not body.mock_mode:
                raise ValueError("A virtual tip snapshot requires an existing durable state or mock mode")
            if tip_snapshot is not None:
                current = {(item["rack_key"], item["slot_id"]): item for item in tip_snapshot["containers"]}
                virtual = body.tip_snapshot.get("containers", [])
                if {(item["rack_key"], item["slot_id"]) for item in virtual} != set(current):
                    raise ValueError("Virtual tip inventory must contain exactly the current slots")
                for item in virtual:
                    actual = current[(item["rack_key"], item["slot_id"])]
                    if item["status"] == "available" and actual["status"] != "available":
                        raise ValueError("Virtual tip inventory cannot make consumed tips available")
                if body.tip_snapshot.get("pipette") != tip_snapshot["pipette"]:
                    raise ValueError("Virtual validation cannot override physical pipette attachment")
            tip_snapshot = body.tip_snapshot
        with tempfile.TemporaryDirectory(prefix="cubos-run-check-") as directory:
            paths = []
            for name, content in (("gantry", gantry_yaml), ("deck", deck_yaml), ("protocol", protocol_yaml)):
                path = Path(directory) / f"{name}.yaml"
                path.write_text(content, encoding="utf-8")
                paths.append(str(path))
            result = run_setup_validation(*paths, tip_snapshot=tip_snapshot)
        return {"valid": result.passed, "errors": list(result.errors), "output": result.output}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return {"valid": False, "errors": [f"{type(exc).__name__}: {exc}"], "output": ""}
    except Exception as exc:
        logging.exception("Run validation failed unexpectedly")
        return {"valid": False, "errors": [f"{type(exc).__name__}: {exc}"], "output": ""}


@router.post("", response_model=RunRecord, status_code=202)
def submit_run(body: RunSubmission, response: Response) -> RunRecord:
    try:
        record = get_run_manager().submit(body)
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (FluidStateError, TipStateError, CapStateError) as exc:
        raise map_state_exception(exc) from exc
    except (RunPolicyError, ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    response.headers["Location"] = f"/api/v1/runs/{record.run_id}"
    return record


@router.get("/{run_id}", response_model=RunRecord)
def get_run(run_id: str) -> RunRecord:
    record = get_run_manager().get(run_id)
    if record is None:
        raise HTTPException(404, f"run {run_id!r} was not found")
    return record


@router.post("/{run_id}/cancel", response_model=RunRecord, status_code=202)
def cancel_run(run_id: str) -> RunRecord:
    try:
        return get_run_manager().cancel(run_id)
    except KeyError as exc:
        raise HTTPException(404, f"run {run_id!r} was not found") from exc
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except InterruptFeedHoldTimeoutError:
        record = get_run_manager().get(run_id)
        assert record is not None
        return record
    except GantryNotConnectedError as exc:
        raise HTTPException(400, "gantry is not connected") from exc


@router.get("/{run_id}/events", response_model=RunEventsResponse)
def get_run_events(run_id: str, after: int = 0) -> RunEventsResponse:
    manager = get_run_manager()
    if manager.get(run_id) is None:
        raise HTTPException(404, f"run {run_id!r} was not found")
    events = [event for event in manager.events(run_id) if event.sequence > after]
    return RunEventsResponse(run_id=run_id, events=events)


@router.get("/{run_id}/plan", response_model=RunPlanResponse)
def get_run_plan(run_id: str) -> RunPlanResponse:
    """Return the compiled step list for *run_id*.

    Compiled from the run's own stored ``protocol.yaml``, so the result is
    deterministic and stays available after the run finishes -- the step view
    has to survive a page reload mid-run and still render a completed run.
    """
    manager = get_run_manager()
    if manager.get(run_id) is None:
        raise HTTPException(404, f"run {run_id!r} was not found")
    protocol_path = manager.store.run_dir(run_id) / "protocol.yaml"
    if not protocol_path.is_file():
        raise HTTPException(404, f"run {run_id!r} has no stored protocol")
    try:
        protocol = load_protocol_from_yaml(protocol_path)
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the operator
        raise HTTPException(
            422, f"protocol for run {run_id!r} could not be compiled: {exc}"
        ) from exc
    registry = CommandRegistry.instance()
    steps = [
        PlanStep(
            index=step.index,
            command=step.command_name,
            summary=_describe(registry, step.command_name, step.args),
            args=_jsonable_args(step.args),
        )
        for step in protocol.steps
    ]
    return RunPlanResponse(run_id=run_id, steps=steps)


@router.get("/{run_id}/artifacts", response_model=RunArtifactsResponse)
def get_run_artifacts(run_id: str) -> RunArtifactsResponse:
    record = get_run_manager().get(run_id)
    if record is None:
        raise HTTPException(404, f"run {run_id!r} was not found")
    return RunArtifactsResponse(run_id=run_id, artifacts=record.artifacts)


@router.get("/{run_id}/artifacts/{name}", response_class=FileResponse)
def download_run_artifact(run_id: str, name: str) -> FileResponse:
    manager = get_run_manager()
    if manager.get(run_id) is None:
        raise HTTPException(404, f"run {run_id!r} was not found")
    path = manager.store.artifact_path(run_id, name)
    if path is None:
        raise HTTPException(404, f"artifact {name!r} was not found")
    return FileResponse(path, filename=name)
