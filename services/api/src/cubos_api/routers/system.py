"""Versioned, hardware-safe CubOS appliance discovery endpoints."""

from __future__ import annotations

import os
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Dict, List

from fastapi import APIRouter, HTTPException, Response
from cubos.instruments.registry import get_supported_types, get_supported_vendors
from pydantic import BaseModel
from cubos.protocol_engine.registry import CommandRegistry
from cubos_api.config import get_settings
from cubos_api.services import updater
from cubos_api.services.run_manager import get_run_manager

# Register CubOS protocol commands without connecting to hardware.
import cubos.protocol_engine.commands  # noqa: F401


API_VERSION = "v1"
RUN_SCHEMA_VERSION = "1"
SERVICE_NAME = "cubos-api"

router = APIRouter(prefix="/api/v1", tags=["cubos-v1"])


class HealthResponse(BaseModel):
    status: str
    service: str
    api_version: str
    server_version: str
    cubos_version: str
    build_version: str | None = None
    run_schema_version: str
    checks: Dict[str, str]


class VersionResponse(BaseModel):
    service: str
    api_version: str
    api_service_version: str
    cubos_version: str
    build_version: str | None = None
    image_digest: str | None = None


class CapabilitiesResponse(BaseModel):
    api_version: str
    commands: List[str]
    instruments: Dict[str, List[str]]


class UpdateStatusResponse(BaseModel):
    current_sha: str
    latest_sha: str
    commits_behind: int
    update_available: bool
    checked_at: float
    summary: List[str]
    error: str | None
    current_tag: str | None = None
    latest_tag: str | None = None


class ApplyUpdateRequest(BaseModel):
    force: bool = False


class ApplyUpdateResponse(BaseModel):
    status: str
    target_sha: str


def _distribution_version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "unknown"


def _writable_directory(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".cub-health-", dir=path):
        pass
    return "writable"


@router.get("/health", response_model=HealthResponse)
def health(response: Response) -> HealthResponse:
    """Report offline readiness, release identity, and persistent-data health."""
    settings = get_settings()
    checks: Dict[str, str] = {"run_schema": "compatible"}
    paths = {
        "configs": settings.configs_dir,
        "runs": settings.run_dir.expanduser().resolve(),
        "data": settings.data_db_path.expanduser().resolve().parent,
    }
    for name, path in paths.items():
        try:
            checks[name] = _writable_directory(path)
        except OSError as exc:
            checks[name] = f"unwritable: {exc}"

    healthy = all(value in {"writable", "compatible"} for value in checks.values())
    if not healthy:
        response.status_code = 503
    return HealthResponse(
        status="ok" if healthy else "degraded",
        service=SERVICE_NAME,
        api_version=API_VERSION,
        server_version=_distribution_version("cubos-api"),
        cubos_version=_distribution_version("cubos"),
        build_version=os.environ.get("CUBOS_BUILD_VERSION"),
        run_schema_version=RUN_SCHEMA_VERSION,
        checks=checks,
    )


@router.get("/version", response_model=VersionResponse)
def get_version() -> VersionResponse:
    """Return release identity for diagnostics and update verification."""
    return VersionResponse(
        service=SERVICE_NAME,
        api_version=API_VERSION,
        api_service_version=_distribution_version("cubos-api"),
        cubos_version=_distribution_version("cubos"),
        build_version=os.environ.get("CUBOS_BUILD_VERSION"),
        image_digest=os.environ.get("CUBOS_IMAGE_DIGEST"),
    )


@router.get("/capabilities", response_model=CapabilitiesResponse)
def get_capabilities() -> CapabilitiesResponse:
    """Reflect installed CubOS commands and instrument vendor support."""
    instruments = {
        instrument_type: get_supported_vendors(instrument_type)
        for instrument_type in get_supported_types()
    }
    return CapabilitiesResponse(
        api_version=API_VERSION,
        commands=CommandRegistry.instance().command_names,
        instruments=instruments,
    )


@router.get("/system/update", response_model=UpdateStatusResponse)
def get_update_status(refresh: bool = False) -> UpdateStatusResponse:
    """Check the configured upstream branch for a newer appliance revision."""
    status = updater.check_for_update(refresh=refresh)
    return UpdateStatusResponse(**status.model_dump())


@router.post(
    "/system/update/apply",
    response_model=ApplyUpdateResponse,
    status_code=202,
)
def apply_system_update(body: ApplyUpdateRequest) -> ApplyUpdateResponse:
    """Launch a detached appliance update when no protocol run is active."""
    active_run_id = get_run_manager().active_run_id
    if active_run_id is not None:
        raise HTTPException(
            409,
            f"cannot update while run {active_run_id} is active",
        )

    status = updater.check_for_update()
    if status.error is not None:
        raise HTTPException(503, status.error)
    if not status.update_available and not body.force:
        raise HTTPException(409, "already up to date")

    try:
        updater.apply_update(status.latest_sha)
    except updater.UpdateLaunchError as exc:
        raise HTTPException(503, str(exc)) from exc
    return ApplyUpdateResponse(status="updating", target_sha=status.latest_sha)
