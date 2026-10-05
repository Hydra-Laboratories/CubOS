"""Active-learning campaign editing and lifecycle endpoints."""
import hashlib
import os
import uuid
from collections.abc import Mapping
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from cubos.gantry.session import GantryNotConnectedError, InterruptFeedHoldTimeoutError
from cubos.gantry.loader import load_gantry_from_yaml
from cubos.data import DataStore
from cubos.optimization import analyze_color_image, rgb_to_lab
from cubos.protocol_engine.commands.camera import default_images_dir
from cubos_api.config import get_settings
from cubos_api.models.campaigns import (
    CampaignRecord, CampaignSpec, CampaignStateBinding, CampaignSubmission,
    CampaignPresetDocument, CampaignPresetResponse, CampaignPresetSaveRequest,
    CampaignPresetSummary,
    ColorCampaignSetup, ColorRgbPreviewRequest, ColorTargetReanalysisRequest,
    ColorTargetRequest,
    Observation,
)
from cubos_api.models.runs import RunRecord, RunSubmission
from cubos_api.models.state import RunStateSelection
from cubos_api.services.color_campaign import build_color_campaign, target_protocol
from cubos_api.services.campaign_manager import get_campaign_manager
from cubos_api.services.run_manager import RunConflictError, get_run_manager
from cubos_api.services.yaml_io import (
    list_configs, read_yaml, resolve_config_path, safe_filename, write_yaml,
)

router = APIRouter(prefix="/api/v1/campaigns", tags=["active-learning"])


def _campaign_preset_directory() -> Path:
    directory = get_settings().configs_dir / "campaign"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _campaign_preset_path(filename: str) -> Path:
    filename = safe_filename(filename)
    if not filename.endswith(".yaml"):
        raise ValueError("Campaign preset filename must end in .yaml")
    _campaign_preset_directory()
    return resolve_config_path(get_settings().configs_dir, "campaign", filename)


def _target_measurement(record: RunRecord) -> dict:
    result = record.result
    if isinstance(result, Mapping) and isinstance(result.get("results"), list):
        result = result["results"]
    if isinstance(result, list):
        matches = [item for item in result if isinstance(item, dict) and item.get("image_path")]
        if matches:
            return matches[-1]
    if isinstance(result, dict) and result.get("image_path"):
        return result
    raise HTTPException(409, "Color target run has no saved image result")


def _trusted_target_image(manager, record: RunRecord) -> Path:
    artifact = record.metadata.get("color_target_source_artifact")
    expected_digest = record.metadata.get("color_target_source_sha256")
    if not isinstance(artifact, str) or not isinstance(expected_digest, str):
        raise HTTPException(
            409,
            "Color target predates immutable source capture; acquire a new target frame",
        )
    path = manager.store.artifact_path(record.run_id, artifact)
    if path is None or not path.is_file():
        raise HTTPException(404, "Immutable color target image was not found")
    observed_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if observed_digest != expected_digest:
        raise HTTPException(409, "Immutable color target image digest does not match its run")
    return path


def _color_target_record(run_id: str) -> tuple[object, RunRecord]:
    manager = get_run_manager()
    record = manager.get(run_id)
    if record is None or not record.metadata.get("active_learning_target"):
        raise HTTPException(404, "Color target run not found")
    if record.state != "succeeded" or record.result is None:
        raise HTTPException(409, "Color target run has not completed successfully")
    return manager, record


def _available_campaign_tips(setup: ColorCampaignSetup) -> list[str] | None:
    if setup.fluid_state_id is None:
        return None
    settings = get_settings()
    deck_path = resolve_config_path(settings.configs_dir, "deck", setup.deck_file)
    if not deck_path.is_file():
        raise ValueError(f"Missing deck file: {setup.deck_file}")
    get_run_manager()._resolve_run_state(
        deck_path.read_text(),
        RunStateSelection(fluid_state_id=setup.fluid_state_id),
    )
    store = DataStore(settings.data_db_path)
    try:
        snapshot = store.get_tip_snapshot(setup.fluid_state_id)
    finally:
        store.close()
    pipette = snapshot["pipette"]
    if pipette["attachment_uncertain"]:
        raise ValueError("The durable state has an uncertain pipette attachment")
    if pipette["tip_extension_mm"] is not None:
        raise ValueError(
            "Color campaigns must start with a bare pipette; reconcile or drop "
            "the attached tip before preparing the campaign"
        )
    return [
        f"{item['rack_key']}.{item['slot_id']}"
        for item in snapshot["containers"]
        if item["status"] == "available"
    ]


def _accepted_target_setup(setup: ColorCampaignSetup) -> ColorCampaignSetup:
    if setup.target_mode == "rgb":
        assert setup.target_rgb is not None
        return setup.model_copy(update={
            "target_lab": rgb_to_lab(setup.target_rgb),
            "reference_processing_profile_id": None,
            "reference_origin": "user_selected_srgb",
        })
    if setup.mock_mode:
        return setup.model_copy(update={
            "reference_origin": setup.reference_origin or "accepted_camera_measurement",
        })
    assert setup.target_run_id is not None
    assert setup.target_analysis_revision is not None
    manager, record = _color_target_record(setup.target_run_id)
    _trusted_target_image(manager, record)
    if record.metadata["active_learning_target"] != setup.target_well:
        raise ValueError("Target run well does not match the requested campaign target well")
    if setup.target_analysis_revision == 0:
        analysis = _target_measurement(record)
        frame_metadata = analysis.get("frame_metadata")
        analysis_source_digest = (
            frame_metadata.get("image_sha256")
            if isinstance(frame_metadata, Mapping) else None
        )
    else:
        revisions = record.metadata.get("color_target_reanalyses", [])
        selected_revision = next(
            (
                item for item in revisions
                if isinstance(item, Mapping)
                and item.get("revision") == setup.target_analysis_revision
            ),
            None,
        ) if isinstance(revisions, list) else None
        if not isinstance(selected_revision, Mapping):
            raise ValueError("Target analysis revision was not found")
        match = selected_revision.get("analysis")
        if not isinstance(match, Mapping):
            raise ValueError("Target analysis revision has no measurement payload")
        analysis = dict(match)
        analysis_source_digest = selected_revision.get("source_image_sha256")
    frozen_digest = record.metadata.get("color_target_source_sha256")
    if not (
        isinstance(analysis_source_digest, str)
        and analysis_source_digest
        and analysis_source_digest == frozen_digest
    ):
        raise ValueError(
            "Selected target analysis does not match the frozen captured image digest"
        )
    profile = analysis.get("processing_profile")
    quality = analysis.get("quality")
    if not (
        analysis.get("measurement_status") == "accepted"
        and analysis.get("comparison_status") == "not_requested"
        and isinstance(quality, Mapping)
        and quality.get("accepted") is True
        and isinstance(profile, Mapping)
        and profile.get("schema") == "cubos.camera-well-cielab.v1"
        and isinstance(profile.get("id"), str)
        and profile.get("id")
        and isinstance(analysis.get("lab"), list)
        and len(analysis["lab"]) == 3
    ):
        raise ValueError("Target analysis is not an accepted color measurement")
    configuration = profile.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ValueError("Target processing profile has no configuration provenance")
    if configuration.get("expected_center_normalized") != list(setup.expected_center):
        raise ValueError("Campaign expected center does not match the accepted target profile")
    if configuration.get("expected_center_source") != setup.expected_center_source:
        raise ValueError("Campaign expected-center source does not match the target profile")
    if configuration.get("roi_fraction_of_detected_radius") != setup.roi_fraction:
        raise ValueError("Campaign ROI fraction does not match the accepted target profile")
    acquisition = configuration.get("acquisition")
    requested_capture = (
        acquisition.get("requested_capture_profile")
        if isinstance(acquisition, Mapping) else None
    )
    actual_capture = (
        acquisition.get("actual_capture_profile")
        if isinstance(acquisition, Mapping) else None
    )
    requested_fingerprint = (
        requested_capture.get("fingerprint")
        if isinstance(requested_capture, Mapping) else None
    )
    actual_fingerprint = (
        actual_capture.get("fingerprint")
        if isinstance(actual_capture, Mapping) else None
    )
    if not (
        isinstance(requested_fingerprint, str)
        and requested_fingerprint
        and requested_fingerprint == actual_fingerprint
    ):
        raise ValueError(
            "Target capture profile is missing or differs from the saved frame metadata"
        )
    if acquisition.get("image_height") != setup.image_height:
        raise ValueError("Campaign image height does not match the accepted target capture")
    return setup.model_copy(update={
        "target_lab": tuple(float(value) for value in analysis["lab"]),
        "reference_processing_profile_id": profile["id"],
        "reference_origin": "accepted_camera_measurement",
    })


@router.get("", response_model=list[CampaignRecord])
def list_campaigns():
    return get_campaign_manager().list()


@router.post("/validate")
def validate_campaign(body: CampaignSubmission):
    return get_campaign_manager().validate(body.spec)


@router.post("/color-target", response_model=RunRecord, status_code=202)
def read_color_target(body: ColorTargetRequest):
    """Submit a native run that reads Lab from the selected target well."""
    settings = get_settings()
    try:
        gantry = resolve_config_path(settings.configs_dir, "gantry", body.gantry_file).read_text()
        deck = resolve_config_path(settings.configs_dir, "deck", body.deck_file).read_text()
        submission = RunSubmission(
            run_id=f"color-target-{uuid.uuid4().hex[:12]}",
            gantry_config=gantry,
            deck_config=deck,
            protocol_yaml=target_protocol(
                body.target_well, body.camera_instrument, body.roi_fraction,
                body.image_height,
                body.expected_center,
                body.expected_center_source,
            ),
            mock_mode=body.mock_mode,
            metadata={
                "active_learning_target": body.target_well,
                "source_gantry_file": body.gantry_file,
                "source_deck_file": body.deck_file,
            },
        )
        return get_run_manager().submit(submission)
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc


@router.post("/color-target/rgb-preview")
def preview_rgb_target(body: ColorRgbPreviewRequest):
    """Return the authoritative D65 Lab preview for a user-selected sRGB color."""
    try:
        return {
            "rgb": list(body.rgb),
            "lab": list(rgb_to_lab(body.rgb)),
            "reference_origin": "user_selected_srgb",
            "encoding": "sRGB",
            "whitepoint": "D65",
            "calibration_status": "uncalibrated_reference",
        }
    except ValueError as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc


@router.get("/color-target/{run_id}", response_model=RunRecord)
def get_color_target(run_id: str):
    record = get_run_manager().get(run_id)
    if record is None or not record.metadata.get("active_learning_target"):
        raise HTTPException(404, "Color target run not found")
    return record


@router.get("/color-target/{run_id}/image")
def get_color_target_image(run_id: str) -> Response:
    manager, record = _color_target_record(run_id)
    path = _trusted_target_image(manager, record)
    try:
        import cv2
    except ImportError as exc:
        raise HTTPException(503, "Browser image conversion requires the camera extra") from exc
    frame = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if frame is None:
        raise HTTPException(409, "Saved color target image cannot be decoded")
    encoded, payload = cv2.imencode(".png", frame)
    if not encoded:
        raise HTTPException(500, "Saved color target image cannot be encoded for review")
    return Response(
        payload.tobytes(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/color-target/{run_id}/analysis-image", response_class=FileResponse)
def get_color_target_analysis_image(
    run_id: str,
    revision: int | None = None,
) -> FileResponse:
    manager, record = _color_target_record(run_id)
    revisions = record.metadata.get("color_target_reanalyses", [])
    if revision is not None and revision < 0:
        raise HTTPException(400, "Analysis revision must be zero or greater")
    if revision == 0 or (revision is None and not revisions):
        path = manager.store.artifact_path(
            run_id, "color-target-analysis-0.png",
        )
    elif isinstance(revisions, list) and revisions:
        selected = (
            revisions[-1]
            if revision is None
            else next(
                (
                    item for item in revisions
                    if isinstance(item, Mapping) and item.get("revision") == revision
                ),
                None,
            )
        )
        if not isinstance(selected, Mapping):
            raise HTTPException(404, "Color target analysis revision was not found")
        name = selected.get("image_artifact")
        path = manager.store.artifact_path(run_id, name) if isinstance(name, str) else None
    else:
        raise HTTPException(404, "Color target analysis revision was not found")
    if path is None or not path.is_file():
        raise HTTPException(404, "Color target analysis image was not found")
    return FileResponse(path, media_type="image/png")


@router.post("/color-target/{run_id}/reanalyze")
def reanalyze_color_target(run_id: str, body: ColorTargetReanalysisRequest):
    """Reanalyze the saved target frame without camera access or motion."""
    manager, record = _color_target_record(run_id)
    original = _target_measurement(record)
    image_path = _trusted_target_image(manager, record)
    profile = original.get("processing_profile")
    configuration = profile.get("configuration") if isinstance(profile, Mapping) else None
    acquisition = (
        configuration.get("acquisition")
        if isinstance(configuration, Mapping) else None
    )
    expected_digest = record.metadata["color_target_source_sha256"]
    try:
        transaction = manager.store.color_target_analysis_transaction()
        with transaction:
            staging_path = image_path.with_name(
                f"{image_path.stem}.analysis.png"
            )
            if os.path.lexists(staging_path):
                raise ValueError(
                    "Frozen-source analysis staging path already exists; inspect "
                    "the run artifact directory before retrying"
                )
            try:
                analysis = analyze_color_image(
                    image_path,
                    roi_fraction=float(original.get("roi_fraction", 0.5)),
                    expected_center=body.expected_center,
                    expected_center_source=body.expected_center_source,
                    acquisition_context=(
                        acquisition if isinstance(acquisition, Mapping) else None
                    ),
                )
                analysis["well_identity"] = original.get("well_identity") or {
                    "expected_well": record.metadata["active_learning_target"],
                    "source": "protocol_position",
                    "verification_status": "not_verified_by_cv",
                }
                annotated = analysis.get("annotated_preview_path")
                if not isinstance(annotated, str):
                    raise ValueError("Reanalysis did not produce an annotated preview")
                annotated_path = Path(annotated).expanduser()
                if annotated_path != staging_path:
                    raise ValueError(
                        "Reanalysis preview is not the expected frozen-source derivative"
                    )
                if annotated_path.is_symlink() or not annotated_path.is_file():
                    raise ValueError(
                        "Reanalysis preview is not a regular staging file"
                    )
                digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
                if digest != expected_digest:
                    raise ValueError(
                        "Frozen color target image changed during reanalysis"
                    )
                artifact = {
                    "schema": "cubos.color-target-reanalysis.v1",
                    "source_image_sha256": digest,
                    "expected_center": list(body.expected_center),
                    "expected_center_source": body.expected_center_source,
                    "expected_well": record.metadata["active_learning_target"],
                    "analysis": analysis,
                }
                _, complete_artifact = manager.store.append_color_target_analysis(
                    record,
                    artifact=artifact,
                    annotated_preview=annotated_path,
                )
                analysis = complete_artifact["analysis"]
            finally:
                if os.path.lexists(staging_path):
                    staging_path.unlink(missing_ok=True)
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(409, f"{type(exc).__name__}: {exc}") from exc
    return {
        **analysis,
        "analysis_revision": complete_artifact["revision"],
        "source_image_sha256": digest,
    }


@router.post("/color-setup", response_model=CampaignSpec)
def prepare_color_campaign(body: ColorCampaignSetup):
    """Create a generated candidate protocol and complete campaign draft."""
    settings = get_settings()
    try:
        body = _accepted_target_setup(body)
        source_path = resolve_config_path(
            settings.configs_dir, "protocol", body.source_protocol_file,
        )
        if not source_path.is_file():
            raise ValueError(
                f"Selected source protocol does not exist: {body.source_protocol_file}"
            )
        source_protocol_yaml = source_path.read_text()
        return build_color_campaign(
            body,
            settings.configs_dir / "protocol",
            available_tip_positions=_available_campaign_tips(body),
            source_protocol_yaml=source_protocol_yaml,
            gantry_config=(
                load_gantry_from_yaml(
                    resolve_config_path(settings.configs_dir, "gantry", body.gantry_file),
                )
                if body.photo_position is not None else None
            ),
        )
    except HTTPException:
        raise
    except (ValueError, OSError) as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc


@router.get("/presets", response_model=list[CampaignPresetSummary])
def list_campaign_presets():
    _campaign_preset_directory()
    summaries = []
    for filename in list_configs(get_settings().configs_dir, "campaign"):
        path = _campaign_preset_path(filename)
        try:
            preset = CampaignPresetDocument.model_validate(read_yaml(path))
        except (ValueError, OSError) as exc:
            raise HTTPException(
                400, f"Invalid campaign preset {filename!r}: {exc}",
            ) from exc
        summaries.append(CampaignPresetSummary(
            filename=filename,
            name=preset.name,
            modified_at=path.stat().st_mtime,
        ))
    return summaries


@router.get("/presets/{filename}", response_model=CampaignPresetResponse)
def get_campaign_preset(filename: str):
    try:
        path = _campaign_preset_path(filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(404, f"Campaign preset not found: {filename}")
    try:
        preset = CampaignPresetDocument.model_validate(read_yaml(path))
    except (ValueError, OSError) as exc:
        raise HTTPException(400, f"Invalid campaign preset {filename!r}: {exc}") from exc
    return CampaignPresetResponse(filename=filename, preset=preset)


@router.put("/presets/{filename}", response_model=CampaignPresetResponse)
def save_campaign_preset(filename: str, body: CampaignPresetSaveRequest):
    """Save an editable template without runtime state or target evidence."""
    try:
        path = _campaign_preset_path(filename)
        preset = CampaignPresetDocument(
            name=body.name,
            spec=body.spec.model_copy(
                deep=True, update={"fluid_state_id": None},
            ),
            color_setup=body.color_setup.model_copy(deep=True)
            if body.color_setup is not None else None,
        )
        write_yaml(path, preset.model_dump(mode="json"))
    except (ValueError, OSError) as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
    # TODO(iter): test strict preset round trips, path rejection, and runtime
    # evidence clearing after the quick operator preset iteration.
    return CampaignPresetResponse(filename=filename, preset=preset)


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


@router.post("/{campaign_id}/fluid-state", response_model=CampaignRecord)
def attach_fluid_state(campaign_id: str, body: CampaignStateBinding):
    try:
        return get_campaign_manager().attach_fluid_state(
            campaign_id,
            body.fluid_state_id,
            body.reconciliation_note,
        )
    except KeyError as exc:
        raise HTTPException(404, "Campaign not found") from exc
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc


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
