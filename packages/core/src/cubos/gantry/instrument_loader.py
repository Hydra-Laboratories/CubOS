"""Load mounted instruments from gantry machine config."""

from __future__ import annotations

import difflib
import inspect
import logging
from pathlib import Path
from typing import Any, Dict, Mapping, TYPE_CHECKING

from pydantic import ValidationError

from cubos.instruments.base_instrument import BaseInstrument
from cubos.instruments.registry import get_instrument_class, validate_instrument

from .errors import GantryLoaderError
from .instrument_mount import InstrumentedGantry

if TYPE_CHECKING:
    from cubos.gantry import Gantry

logger = logging.getLogger(__name__)

# Removed YAML fields still present in deployed configs: dropped with a
# warning instead of failing the load. Keyed by instrument type.
_RETIRED_INSTRUMENT_FIELDS: dict[str, dict[str, str]] = {
    "capper": {
        "park_position": "decap/cap no longer park; delete this key",
    },
}


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
                "Remove unknown YAML fields from the gantry instrument entry."
            )
        else:
            guidance = "Review the YAML values against the gantry schema."

        prefix = f" at `{location}`" if location else ""
        return f"Gantry instrument YAML error{prefix}: {detail}\nHow to fix: {guidance}"

    if isinstance(error, ValueError):
        if "must define mounted instruments" in detail:
            return (
                f"Machine config error in `{path}`: {detail}\n"
                "How to fix: Add a top-level 'instruments' section to the gantry YAML."
            )
        return (
            f"Instrument validation error in `{path}`: {detail}\n"
            f"How to fix: Check type and vendor against the instrument registry."
        )

    return (
        f"Gantry instrument loader error in `{path}`: {detail}\n"
        "How to fix: Verify the gantry YAML path and instrument entries."
    )


def _instantiate_instruments(
    instrument_configs: Mapping[str, Mapping[str, Any]],
    *,
    mock_mode: bool = False,
) -> Dict[str, BaseInstrument]:
    instruments: Dict[str, BaseInstrument] = {}
    for name, entry in instrument_configs.items():
        kwargs = dict(entry)
        type_key = kwargs.pop("type")
        vendor = kwargs.pop("vendor")
        validate_instrument(type_key, vendor)
        _drop_retired_fields(name, type_key, kwargs)
        if mock_mode:
            kwargs["offline"] = True
        cls = get_instrument_class(type_key, vendor)
        _validate_driver_kwargs(name, type_key, vendor, cls, kwargs)
        instruments[name] = cls(**kwargs)
    return instruments


def _drop_retired_fields(name: str, type_key: str, kwargs: Dict[str, Any]) -> None:
    for field, reason in _RETIRED_INSTRUMENT_FIELDS.get(type_key, {}).items():
        if field in kwargs:
            kwargs.pop(field)
            logger.warning(
                "Instrument '%s' (%s): YAML field '%s' is no longer used and "
                "was ignored (%s).", name, type_key, field, reason,
            )


def _validate_driver_kwargs(
    name: str,
    type_key: str,
    vendor: str,
    cls: type[BaseInstrument],
    kwargs: Mapping[str, Any],
) -> None:
    """Reject YAML keys that the resolved driver constructor will not consume."""
    signature = inspect.signature(cls.__init__)
    accepted = {
        param_name
        for param_name, parameter in signature.parameters.items()
        if param_name != "self"
        and parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    unknown = sorted(set(kwargs) - accepted)
    if not unknown:
        return

    details = []
    for key in unknown:
        matches = difflib.get_close_matches(key, sorted(accepted), n=1)
        hint = f" Did you mean '{matches[0]}'?" if matches else ""
        details.append(f"'{key}'{hint}")
    raise ValueError(
        f"Instrument '{name}' ({type_key}/{vendor}) has unsupported YAML "
        f"field(s): {', '.join(details)}. Accepted fields: {sorted(accepted)}"
    )


def build_instrumented_gantry(
    instrument_configs: Mapping[str, Mapping[str, Any]],
    gantry: Gantry,
    *,
    expected_grbl_settings: dict[str, float] | None = None,
    mock_mode: bool = False,
    safe_z: float | None = None,
) -> InstrumentedGantry:
    """Build an InstrumentedGantry from parsed instrument config entries."""
    return InstrumentedGantry(
        controller=gantry,
        instruments=_instantiate_instruments(
            instrument_configs,
            mock_mode=mock_mode,
        ),
        expected_grbl_settings=expected_grbl_settings,
        safe_z=safe_z,
    )


def load_instrumented_gantry_from_config(
    config: Any,
    gantry: Gantry,
    mock_mode: bool = False,
) -> InstrumentedGantry:
    """Load the runtime instrumented gantry from a loaded machine config."""
    instrument_configs = getattr(config, "instruments", None)
    if not instrument_configs:
        raise ValueError(
            "Gantry machine config must define mounted instruments under "
            "the top-level 'instruments' key."
        )
    expected_grbl_settings = getattr(config, "expected_grbl_settings", None)
    safe_z = getattr(config, "resolved_safe_z", None)
    return build_instrumented_gantry(
        instrument_configs,
        gantry,
        expected_grbl_settings=expected_grbl_settings,
        mock_mode=mock_mode,
        safe_z=safe_z,
    )


def load_instrumented_gantry_from_yaml(
    path: str | Path,
    gantry: Gantry,
    mock_mode: bool = False,
) -> InstrumentedGantry:
    """Load an InstrumentedGantry from instruments embedded in a gantry YAML."""
    from cubos.gantry.loader import load_gantry_from_yaml

    config = load_gantry_from_yaml(path)
    return load_instrumented_gantry_from_config(
        config,
        gantry,
        mock_mode=mock_mode,
    )


def load_instrumented_gantry_from_yaml_safe(
    path: str | Path,
    gantry: Gantry,
    mock_mode: bool = False,
) -> InstrumentedGantry:
    """Load mounted instruments from gantry YAML with user-friendly errors."""
    resolved = Path(path)
    try:
        return load_instrumented_gantry_from_yaml(
            resolved, gantry, mock_mode=mock_mode,
        )
    except Exception as exc:
        raise GantryLoaderError(_format_loader_exception(resolved, exc)) from exc
