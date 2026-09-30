"""Protocol router: CRUD for protocol YAML files + command registry.

Commands are introspected from CubOS's CommandRegistry at runtime,
so any new @protocol_command in CubOS is automatically available.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from cubos.gantry.session import (
    CalibrationBlockedError,
    GantryNotConnectedError,
    GantrySessionError,
    GantrySessionHealthCheckError,
    InterruptFeedHoldTimeoutError,
)
from pydantic import BaseModel
from pydantic import ValidationError
from cubos.protocol_engine.registry import CommandRegistry
from cubos.protocol_engine.setup_validator import run_setup_validation
from cubos.protocol_engine.yaml_schema import ProtocolYamlSchema
from cubos.validation.errors import SetupValidationError

# Side-effect import: triggers @protocol_command registration.
import cubos.protocol_engine.commands  # noqa: F401

from cubos_api.config import get_settings
from cubos_api.models.protocol import (
    CommandArg,
    CommandInfo,
    ProtocolConfig,
    ProtocolResponse,
    ProtocolSetupValidationRequest,
    ProtocolSetupValidationResponse,
    ProtocolStepConfig,
    ProtocolValidationResponse,
)
from cubos_api.services.yaml_io import delete_config, list_configs, read_yaml, resolve_config_path, write_yaml

router = APIRouter(prefix="/api/v1/protocol", tags=["protocol"])


def _type_name(annotation: Any) -> str:
    """Convert a Python type annotation to a simple string for the frontend."""
    name = getattr(annotation, "__name__", None)
    if name:
        return name
    return str(annotation)


def _build_command_info(name: str) -> CommandInfo:
    """Build a CommandInfo from a registered CubOS command."""
    registry = CommandRegistry.instance()
    cmd = registry.get(name)
    args = []
    for field_name, field_info in cmd.schema.model_fields.items():
        args.append(
            CommandArg(
                name=field_name,
                type=_type_name(field_info.annotation),
                required=field_info.is_required(),
                default=None if field_info.is_required() else field_info.default,
            )
        )
    return CommandInfo(
        name=cmd.name,
        description=(cmd.handler.__doc__ or "").strip(),
        args=args,
    )


def _protocol_schema_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return the CubOS-owned protocol fields while leaving sidecar keys alone."""
    payload: Dict[str, Any] = {}
    if "positions" in data:
        payload["positions"] = data["positions"]
    if "protocol" in data:
        payload["protocol"] = data["protocol"]
    return payload


def _validate_protocol_schema(data: Dict[str, Any]) -> ProtocolYamlSchema:
    return ProtocolYamlSchema.model_validate(_protocol_schema_payload(data))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/commands")
def get_commands() -> List[CommandInfo]:
    """Return all registered protocol commands with their argument schemas."""
    registry = CommandRegistry.instance()
    return [_build_command_info(name) for name in registry.command_names]


@router.get("/commands/{name}")
def get_command(name: str) -> CommandInfo:
    """Return schema for a single protocol command."""
    registry = CommandRegistry.instance()
    if name not in registry.command_names:
        raise HTTPException(404, f"Unknown command '{name}'")
    return _build_command_info(name)


@router.get("/configs")
def list_protocol_configs() -> List[str]:
    return list_configs(get_settings().configs_dir, "protocol")


class RunStatusResponse(BaseModel):
    active: bool
    protocol_file: str | None = None


@router.get("/run-status")
def get_run_status() -> RunStatusResponse:
    """Report whether a protocol run is currently in progress.

    Registered before the catch-all ``GET /{filename}`` route below so the
    literal path ``run-status`` isn't swallowed as a filename lookup.
    """
    from cubos_api.routers import gantry as gantry_router

    return RunStatusResponse(**gantry_router.run_status())


@router.get("/{filename}")
def get_protocol(filename: str) -> ProtocolResponse:
    try:
        path = resolve_config_path(get_settings().configs_dir, "protocol", filename)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not path.is_file():
        raise HTTPException(404, f"Protocol file not found: {filename}")
    try:
        data = read_yaml(path)
        schema = _validate_protocol_schema(data)
    except (ValueError, ValidationError) as e:
        raise HTTPException(400, str(e)) from e

    steps = [
        ProtocolStepConfig(command=step.command, args=step.args)
        for step in schema.protocol
    ]

    return ProtocolResponse(filename=filename, positions=schema.positions, steps=steps)


@router.put("/{filename}")
def save_protocol(filename: str, body: ProtocolConfig) -> dict:
    try:
        path = resolve_config_path(get_settings().configs_dir, "protocol", filename)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    # Convert to YAML-native format: list of {command: {args}}
    protocol_list = []
    for step in body.protocol:
        protocol_list.append({step.command: step.args if step.args else None})

    try:
        data: Dict[str, Any] = read_yaml(path) if path.is_file() else {}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if body.positions is not None:
        data["positions"] = body.positions
    else:
        data.pop("positions", None)
    data["protocol"] = protocol_list
    try:
        _validate_protocol_schema(data)
    except ValidationError as exc:
        raise HTTPException(400, str(exc)) from exc

    write_yaml(path, data)
    return {"status": "ok", "filename": filename}


@router.delete("/{filename}")
def delete_protocol(filename: str) -> dict:
    from cubos_api.routers import gantry as gantry_router

    if gantry_router.run_active():
        raise HTTPException(409, "Cannot delete a protocol while a protocol run is active")
    try:
        delete_config(get_settings().configs_dir, "protocol", filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError:
        raise HTTPException(404, f"Config not found: {filename}")
    return {"status": "deleted", "filename": filename}


@router.post("/validate")
def validate_protocol(body: ProtocolConfig) -> ProtocolValidationResponse:
    """Validate a protocol against CubOS's command schemas."""
    registry = CommandRegistry.instance()
    errors: List[str] = []
    for i, step in enumerate(body.protocol):
        if step.command not in registry.command_names:
            errors.append(
                f"Step {i}: Unknown command '{step.command}'. "
                f"Available: {', '.join(registry.command_names)}"
            )
            continue

        cmd = registry.get(step.command)
        try:
            cmd.schema.model_validate(step.args)
        except Exception as e:
            errors.append(f"Step {i} ({step.command}): {e}")

    return ProtocolValidationResponse(valid=len(errors) == 0, errors=errors)


@router.post("/validate-setup")
def validate_protocol_setup(
    body: ProtocolSetupValidationRequest,
) -> ProtocolSetupValidationResponse:
    """Run full CubOS gantry/deck/protocol setup validation."""
    settings = get_settings()
    try:
        gantry_path = resolve_config_path(settings.configs_dir, "gantry", body.gantry_file)
        deck_path = resolve_config_path(settings.configs_dir, "deck", body.deck_file)
        protocol_path = resolve_config_path(settings.configs_dir, "protocol", body.protocol_file)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    try:
        result = run_setup_validation(
            str(gantry_path),
            str(deck_path),
            str(protocol_path),
        )
    except Exception as exc:
        logging.exception("Setup validation failed unexpectedly")
        raise HTTPException(500, f"Setup validation failed: {exc}") from exc

    return ProtocolSetupValidationResponse(
        valid=result.passed,
        errors=list(result.errors),
        output=result.output,
    )


class RunProtocolRequest(BaseModel):
    gantry_file: str
    deck_file: str
    protocol_file: str


@router.post("/cancel")
def cancel_protocol_run() -> dict:
    """Request immediate feed hold for the active protocol run."""
    from cubos_api.routers import gantry as gantry_router

    try:
        gantry_router.request_feed_hold_interrupt()
    except InterruptFeedHoldTimeoutError as exc:
        logging.warning(
            "Protocol cancel feed hold timed out after being sent: %s",
            exc,
        )
        return gantry_router.translate_interrupt_timeout(exc)
    except HTTPException:
        raise
    except GantryNotConnectedError as exc:
        raise HTTPException(400, "Gantry is not connected") from exc
    except Exception as exc:
        logging.exception("Protocol cancel failed")
        raise HTTPException(500, f"Cancel failed: {exc}") from exc
    return {"status": "cancel_requested"}


@router.post("/run")
def run_protocol_endpoint(body: RunProtocolRequest) -> dict:
    """Run a protocol through the persistent CubOS gantry session.

    Legacy synchronous run surface, kept for compatibility. It has no
    ``fluid_state_id``/``initial_state`` parameter and creates no durable
    run record, so it structurally cannot select or persist fluid-state
    linkage (Feature 07) — that requires the versioned, addressable
    ``POST /api/v1/runs`` resource (``cubos_api.routers.runs``), which is
    the sole hardware owner for stateful protocol execution.
    """
    from cubos_api.routers import gantry as gantry_router

    settings = get_settings()
    try:
        gantry_path = resolve_config_path(settings.configs_dir, "gantry", body.gantry_file)
        deck_path = resolve_config_path(settings.configs_dir, "deck", body.deck_file)
        protocol_path = resolve_config_path(settings.configs_dir, "protocol", body.protocol_file)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    gantry_router.begin_run(protocol_file=body.protocol_file)
    try:
        try:
            result = gantry_router.run_protocol_on_session(
                gantry_path=str(gantry_path),
                deck_path=str(deck_path),
                protocol_path=str(protocol_path),
                gantry_file=body.gantry_file,
                deck_file=body.deck_file,
                protocol_file=body.protocol_file,
                db_path=settings.data_db_path,
            )
        except HTTPException:
            raise
        except (GantryNotConnectedError, GantrySessionHealthCheckError) as exc:
            raise HTTPException(400, "Gantry is not connected") from exc
        except CalibrationBlockedError as exc:
            raise HTTPException(400, str(exc)) from exc
        except SetupValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        except GantrySessionError as exc:
            raise HTTPException(500, f"Execution failed: {exc}") from exc
        except Exception as exc:
            logging.exception("Protocol execution failed")
            raise HTTPException(500, f"Execution failed: {exc}") from exc
    finally:
        gantry_router.end_run()

    return {
        "status": result.status,
        "steps_executed": result.steps_executed,
        "campaign_id": result.campaign_id,
    }
