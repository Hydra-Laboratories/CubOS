"""Active-learning campaign editing and lifecycle endpoints."""
from fastapi import APIRouter, HTTPException
from cubos.gantry.session import GantryNotConnectedError, InterruptFeedHoldTimeoutError
from cubos_api.models.campaigns import CampaignRecord, CampaignSubmission, Observation
from cubos_api.services.campaign_manager import get_campaign_manager
from cubos_api.services.run_manager import RunConflictError

router = APIRouter(prefix="/api/v1/campaigns", tags=["active-learning"])


@router.get("", response_model=list[CampaignRecord])
def list_campaigns():
    return get_campaign_manager().list()


@router.post("/validate")
def validate_campaign(body: CampaignSubmission):
    return get_campaign_manager().validate(body.spec)


@router.post("", response_model=CampaignRecord, status_code=202)
def start_campaign(body: CampaignSubmission):
    try:
        return get_campaign_manager().start(body.spec)
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc


@router.get("/{campaign_id}", response_model=CampaignRecord)
def get_campaign(campaign_id: str):
    try:
        return get_campaign_manager().get(campaign_id)
    except KeyError as exc:
        raise HTTPException(404, "Campaign not found") from exc


@router.post("/{campaign_id}/observation", response_model=CampaignRecord)
def observe(campaign_id: str, body: Observation):
    try:
        return get_campaign_manager().observe(campaign_id, body.value)
    except KeyError as exc:
        raise HTTPException(404, "Campaign not found") from exc
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{campaign_id}/{action}", response_model=CampaignRecord)
def control(campaign_id: str, action: str):
    if action not in {"pause", "resume", "stop", "cancel"}:
        raise HTTPException(404, "Unknown campaign action")
    try:
        return get_campaign_manager().control(campaign_id, action)
    except KeyError as exc:
        raise HTTPException(404, "Campaign not found") from exc
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except GantryNotConnectedError as exc:
        raise HTTPException(400, "Stop was requested, but the gantry is disconnected and immediate cancellation could not be confirmed.") from exc
    except InterruptFeedHoldTimeoutError as exc:
        raise HTTPException(409, "Stop was requested, but feed hold did not confirm in time. Inspect the controller before further actions.") from exc
