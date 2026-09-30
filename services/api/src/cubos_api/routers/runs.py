"""Asynchronous, addressable CubOS protocol runs under the versioned API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
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
