"""Campaign presentation and evidence-export endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from cubos_api.models.presentation import DemoMarker, DemoMarkerRequest, PresentationResponse
from cubos_api.services.campaign_manager import get_campaign_manager
from cubos_api.services.campaign_presentation import CampaignPresentationService
from cubos_api.services.run_manager import get_run_manager

router = APIRouter(prefix="/api/v1/campaigns", tags=["campaign-presentation"])


def _service() -> CampaignPresentationService:
    return CampaignPresentationService(get_campaign_manager(), get_run_manager())


@router.get("/{campaign_id}/presentation", response_model=PresentationResponse)
def get_presentation(campaign_id: str) -> PresentationResponse:
    try:
        return _service().project(campaign_id)
    except KeyError as exc:
        raise HTTPException(404, "Campaign not found") from exc
    except OverflowError as exc:
        raise HTTPException(413, str(exc)) from exc


@router.post("/{campaign_id}/presentation/markers", response_model=DemoMarker, status_code=201)
def create_marker(campaign_id: str, body: DemoMarkerRequest) -> DemoMarker:
    try:
        return _service().add_marker(campaign_id, body)
    except KeyError as exc:
        raise HTTPException(404, "Campaign not found") from exc


@router.get("/{campaign_id}/presentation/assets/{asset_id}", response_class=FileResponse)
def get_asset(campaign_id: str, asset_id: str) -> FileResponse:
    try:
        path = _service().resolve_asset(campaign_id, asset_id)
    except KeyError as exc:
        raise HTTPException(404, "Campaign not found") from exc
    if path is None:
        raise HTTPException(404, "Presentation asset not found")
    return FileResponse(path, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/{campaign_id}/presentation/export.zip")
def export_presentation(campaign_id: str) -> Response:
    try:
        payload = _service().export_zip(campaign_id)
    except KeyError as exc:
        raise HTTPException(404, "Campaign not found") from exc
    except OverflowError as exc:
        raise HTTPException(413, str(exc)) from exc
    return Response(
        payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{campaign_id}-presentation.zip"',
            "Cache-Control": "no-store",
        },
    )
