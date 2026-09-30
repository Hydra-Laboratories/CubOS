"""Load gantry YAML into a GantryConfig."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from cubos.yaml_utils import load_yaml_file

from .errors import GantryLoaderError
from .gantry_config import (
    GantryConfig,
    GantryType,
    OriginPolicy,
    WorkingVolume,
    YAxisMotion,
)
from .grbl_settings import normalize_expected_grbl_settings
from .yaml_schema import GantryYamlSchema


def _format_loader_exception(path: Path, error: Exception) -> str:
    """Return a concise, actionable error message."""
    detail = str(error)

    if isinstance(error, ValidationError):
        first = error.errors()[0] if error.errors() else {}
        detail = first.get("msg", detail)
        location = ".".join(str(part) for part in first.get("loc", []))
        error_type = first.get("type", "")

        if "missing" in error_type or "Field required" in detail:
            guidance = "Add the missing required YAML field shown in the error location."
        elif "extra_forbidden" in error_type or "Extra inputs are not permitted" in detail:
            guidance = (
                "Remove unknown YAML fields; only 'serial_port', 'cnc', "
                "'gantry_type', 'working_volume', 'origin_policy', "
                "'grbl_settings', and 'instruments' are allowed at root."
            )
        else:
            guidance = "Review the YAML values against the gantry schema."

        prefix = f" at `{location}`" if location else ""
        return f"Gantry YAML error{prefix}: {detail}\nHow to fix: {guidance}"

    if isinstance(error, yaml.YAMLError):
        return (
            f"Gantry YAML parse error in `{path}`.\n"
            "How to fix: Check YAML indentation, colons, and structure."
        )

    if isinstance(error, FileNotFoundError):
        return (
            f"Gantry config file not found: `{path}`.\n"
            "How to fix: Verify the file path exists."
        )

    return (
        f"Gantry loader error in `{path}`: {detail}\n"
        "How to fix: Verify the file path and gantry YAML contents."
    )


def load_gantry_from_yaml(path: str | Path) -> GantryConfig:
    """Load a gantry YAML file and return a GantryConfig.

    Raises:
        FileNotFoundError: If the YAML file does not exist.
        yaml.YAMLError: If the file is not valid YAML.
        ValidationError: If the YAML does not match the schema.
    """
    path = Path(path)
    raw = load_yaml_file(path)
    if raw is None:
        raw = {}

    schema = GantryYamlSchema.model_validate(raw)

    expected_grbl = normalize_expected_grbl_settings(schema.grbl_settings)
    instruments = {
        name: entry.model_dump()
        for name, entry in schema.instruments.items()
    }

    return GantryConfig(
        serial_port=schema.serial_port,
        gantry_type=GantryType(schema.gantry_type),
        factory_z_travel_mm=schema.cnc.factory_z_travel_mm,
        calibration_block_height_mm=schema.cnc.calibration_block_height_mm,
        safe_z=schema.safe_z,
        default_feed_rate_mm_min=schema.cnc.default_feed_rate_mm_min,
        working_volume=WorkingVolume(
            x_min=schema.working_volume.x_min,
            x_max=schema.working_volume.x_max,
            y_min=schema.working_volume.y_min,
            y_max=schema.working_volume.y_max,
            z_min=schema.working_volume.z_min,
            z_max=schema.working_volume.z_max,
        ),
        y_axis_motion=YAxisMotion(schema.cnc.y_axis_motion),
        origin_policy=OriginPolicy(schema.origin_policy),
        expected_grbl_settings=expected_grbl,
        instruments=instruments,
    )


def load_gantry_from_yaml_safe(path: str | Path) -> GantryConfig:
    """Load gantry YAML with user-friendly exception formatting.

    Raises:
        GantryLoaderError: Concise, actionable message intended for CLI output.
    """
    resolved = Path(path)
    try:
        return load_gantry_from_yaml(resolved)
    except Exception as exc:
        raise GantryLoaderError(_format_loader_exception(resolved, exc)) from exc
