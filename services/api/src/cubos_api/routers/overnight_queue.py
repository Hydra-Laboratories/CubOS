"""Operator-controlled API for the persistent overnight campaign queue."""

from fastapi import APIRouter, HTTPException

from cubos_api.models.overnight_queue import OvernightQueuePrepare, OvernightQueueRecord
from cubos_api.services.overnight_queue import get_overnight_queue_manager
from cubos_api.services.run_manager import RunConflictError


router = APIRouter(
    prefix="/api/v1/campaigns/overnight",
    tags=["active-learning-overnight"],
)


@router.get("", response_model=list[OvernightQueueRecord])
def list_overnight_queues():
    return get_overnight_queue_manager().list()


@router.get("/latest", response_model=OvernightQueueRecord)
def latest_overnight_queue():
    try:
        return get_overnight_queue_manager().latest()
    except KeyError as exc:
        raise HTTPException(404, "No overnight queue has been prepared") from exc


@router.post("/prepare", response_model=OvernightQueueRecord, status_code=201)
def prepare_overnight_queue(body: OvernightQueuePrepare):
    try:
        return get_overnight_queue_manager().prepare(body)
    except (OSError, ValueError) as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc


@router.get("/{queue_id}", response_model=OvernightQueueRecord)
def get_overnight_queue(queue_id: str):
    try:
        return get_overnight_queue_manager().get(queue_id)
    except KeyError as exc:
        raise HTTPException(404, "Overnight queue not found") from exc


@router.post("/{queue_id}/start", response_model=OvernightQueueRecord, status_code=202)
def start_overnight_queue(queue_id: str):
    try:
        return get_overnight_queue_manager().start(queue_id)
    except KeyError as exc:
        raise HTTPException(404, "Overnight queue not found") from exc
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc


@router.post("/{queue_id}/cancel", response_model=OvernightQueueRecord)
def cancel_overnight_queue(queue_id: str):
    try:
        return get_overnight_queue_manager().cancel(queue_id)
    except KeyError as exc:
        raise HTTPException(404, "Overnight queue not found") from exc
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
