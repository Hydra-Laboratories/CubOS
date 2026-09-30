"""Instrument registry: supported types, vendors, and extension loading."""

from __future__ import annotations

import copy
import inspect
import importlib
import os
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path
from types import NoneType, UnionType
from typing import Any, Dict, Iterable, Mapping, Type, Union, get_args, get_origin, get_type_hints

import logging

from cubos.yaml_utils import load_yaml_file

from cubos.instruments.base_instrument import BaseInstrument

logger = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).parent / "registry.yaml"
_ENTRY_POINT_GROUP = "cubos.instrument_registries"
_OVERLAY_ENV_VAR = "CUBOS_INSTRUMENT_REGISTRY_PATHS"
_VALID_CALIBRATION_MODES = {"contact", "non_contact", "follow_camera"}
_BASE_CONFIG_PARAMS = {"name", "offset_x", "offset_y", "depth", "offline"}
_PRIMITIVE_CONFIG_TYPES = {str, int, float, bool}

_cache: Dict[str, Any] | None = None


@dataclass(frozen=True)
class FieldSpec:
    """Public instrument config field metadata for UI/schema consumers."""

    name: str
    type: str
    required: bool
    default: Any = None
    choices: tuple[Any, ...] | None = None


def load_registry() -> Dict[str, Any]:
    """Load built-in and external instrument registry definitions.

    Merge order is:
    1. CubOS built-in ``registry.yaml``.
    2. Installed package entry points in ``cubos.instrument_registries``.
    3. Explicit overlay YAML paths from ``CUBOS_INSTRUMENT_REGISTRY_PATHS``.

    Entry points may return a registry mapping, a path to a registry YAML file,
    or a callable returning either of those. Overlay paths use ``os.pathsep`` as
    the separator, so ``:`` on POSIX and ``;`` on Windows.
    """
    global _cache
    if _cache is None:
        registry = _load_registry_file(_REGISTRY_PATH)
        for source, overlay in _entry_point_registries():
            _merge_registry(registry, overlay, source=source)
        for path in _overlay_paths():
            _merge_registry(
                registry,
                _load_registry_file(path),
                source=f"overlay {path}",
            )
        _validate_registry(registry)
        _cache = registry
    return copy.deepcopy(_cache)


def get_supported_types() -> list[str]:
    """Return a sorted list of all supported instrument type keys."""
    return sorted(load_registry()["instruments"].keys())


def get_supported_vendors(instrument_type: str) -> list[str]:
    """Return allowed vendors for an instrument type."""
    entry = _instrument_entry(instrument_type)
    return sorted(entry["vendors"].keys())


def get_calibration_mode(instrument_type: str) -> str:
    """Return calibration mode for an instrument type.

    ``contact`` instruments touch the shared calibration block. ``non_contact``
    instruments are centered over the block and calibrated from a measured
    distance above the block top. ``follow_camera`` instruments are co-mounted
    with the camera and reuse its calibration without their own step.
    """
    entry = _instrument_entry(instrument_type)
    return entry.get("calibration_mode", "contact")


def get_instrument_interface(instrument_type: str) -> Type[BaseInstrument]:
    """Import and return the generic interface class for an instrument type."""
    entry = _instrument_entry(instrument_type)
    module_path, class_name = entry["interface"].rsplit(".", 1)
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    if not issubclass(cls, BaseInstrument):
        raise TypeError(
            f"Interface {entry['interface']} for '{instrument_type}' is not "
            "a BaseInstrument subclass."
        )
    return cls


def get_instrument_class(
    instrument_type: str,
    vendor: str,
) -> Type[BaseInstrument]:
    """Import and return the concrete driver class for a type/vendor pair."""
    vendor_entry = _vendor_entry(instrument_type, vendor)
    module = importlib.import_module(vendor_entry["module"])
    cls = getattr(module, vendor_entry["class_name"])
    if not issubclass(cls, get_instrument_interface(instrument_type)):
        raise TypeError(
            f"Driver {vendor_entry['module']}.{vendor_entry['class_name']} "
            f"for '{instrument_type}/{vendor}' does not implement "
            f"{get_instrument_interface(instrument_type).__name__}."
        )
    if inspect.isabstract(cls):
        missing_methods = sorted(getattr(cls, "__abstractmethods__", set()))
        raise TypeError(
            f"Driver {vendor_entry['module']}.{vendor_entry['class_name']} "
            f"for '{instrument_type}/{vendor}' is missing required interface "
            f"methods: {missing_methods}."
        )
    return cls


def validate_instrument(instrument_type: str, vendor: str) -> None:
    """Validate that a type+vendor combination is supported."""
    _vendor_entry(instrument_type, vendor)


def config_fields(instrument_type: str, vendor: str) -> list[FieldSpec]:
    """Return public config fields accepted by an instrument driver.

    The result is derived from the concrete driver's ``__init__`` signature,
    excluding CubOS's common mount fields. Drivers may expose
    ``CONFIG_FIELD_CHOICES`` as ``field_name -> iterable`` to attach option
    lists for UI consumers.
    """
    cls = get_instrument_class(instrument_type, vendor)
    signature = inspect.signature(cls.__init__)
    type_hints = _safe_type_hints(cls.__init__)
    choices_by_field = getattr(cls, "CONFIG_FIELD_CHOICES", {})
    specs: list[FieldSpec] = []
    for param_name, param in signature.parameters.items():
        if (
            param_name == "self"
            or param_name in _BASE_CONFIG_PARAMS
            or param.kind
            in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ):
            continue
        annotation = type_hints.get(param_name, param.annotation)
        if annotation is inspect.Parameter.empty:
            annotation = str
        if not _is_config_field_type(annotation):
            continue
        required = param.default is inspect.Parameter.empty
        choices = choices_by_field.get(param_name)
        specs.append(
            FieldSpec(
                name=param_name,
                type=_type_name(annotation),
                required=required,
                default=None if required else param.default,
                choices=None if choices is None else tuple(choices),
            )
        )
    return specs


def list_measurement_methods(
    instrument_type: str,
    vendor: str | None = None,
) -> list[str]:
    """Return public method names whose return values CubOS can persist.

    Parameters
    ----------
    instrument_type:
        Registry instrument type key.
    vendor:
        Optional vendor key. When omitted, methods across all registered
        vendors for the type are returned.
    """
    from cubos.protocol_engine.measurements import is_measurement_result

    vendors = [vendor] if vendor is not None else get_supported_vendors(instrument_type)
    methods: set[str] = set()
    for vendor_key in vendors:
        cls = get_instrument_class(instrument_type, vendor_key)
        for method_name, method in inspect.getmembers(cls, predicate=inspect.isfunction):
            if method_name.startswith("_"):
                continue
            annotation = _return_annotation(method)
            if is_measurement_result(annotation):
                methods.add(method_name)
    return sorted(methods, key=_measurement_method_sort_key)


# Parameters the protocol engine injects at dispatch time
# (cubos.protocol_engine.commands._dispatch.inject_runtime_args) — the UI /
# YAML never supplies these through method_kwargs.
_ENGINE_INJECTED_PARAMS = {
    "gantry", "well_z", "measurement_height", "indentation_limit_height",
}


def _method_param_field(name: str, hint: Any, required: bool, default: Any) -> dict[str, Any]:
    type_name = getattr(hint, "__name__", None) or (str(hint) if hint is not None else "Any")
    return {
        "name": name,
        "type": type_name,
        "required": required,
        "default": default,
        "fields": None,
    }


def list_measurement_method_params(
    instrument_type: str,
    vendor: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Per measurement method, the parameters ``method_kwargs`` must supply.

    Engine-injected parameters are excluded. A parameter annotated with a
    dataclass (e.g. ``run_OCP(params: OCPParams)``) carries its dataclass
    fields under ``fields`` so a UI can render inputs for them; the engine's
    dispatch layer coerces the resulting mapping back into the dataclass.
    When multiple vendors define the same method, the first vendor's
    signature wins (vendor implementations share the interface's contract).
    """
    import dataclasses

    from cubos.protocol_engine.measurements import is_measurement_result

    vendors = [vendor] if vendor is not None else get_supported_vendors(instrument_type)
    out: dict[str, list[dict[str, Any]]] = {}
    for vendor_key in vendors:
        cls = get_instrument_class(instrument_type, vendor_key)
        for method_name, method in inspect.getmembers(cls, predicate=inspect.isfunction):
            if method_name.startswith("_") or method_name in out:
                continue
            if not is_measurement_result(_return_annotation(method)):
                continue
            try:
                hints = get_type_hints(method)
            except Exception:
                hints = {}
            specs: list[dict[str, Any]] = []
            for param in inspect.signature(method).parameters.values():
                if param.name == "self" or param.name in _ENGINE_INJECTED_PARAMS:
                    continue
                if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                    continue
                hint = hints.get(param.name)
                required = param.default is inspect.Parameter.empty
                spec = _method_param_field(
                    param.name, hint, required,
                    None if required else param.default,
                )
                if hint is not None and dataclasses.is_dataclass(hint) and isinstance(hint, type):
                    try:
                        field_hints = get_type_hints(hint)
                    except Exception:
                        field_hints = {}
                    spec["fields"] = [
                        _method_param_field(
                            field.name,
                            field_hints.get(field.name),
                            field.default is dataclasses.MISSING
                            and field.default_factory is dataclasses.MISSING,
                            field.default if field.default is not dataclasses.MISSING else None,
                        )
                        for field in dataclasses.fields(hint)
                    ]
                specs.append(spec)
            out[method_name] = specs
    return {name: out[name] for name in sorted(out, key=_measurement_method_sort_key)}


def _instrument_entry(instrument_type: str) -> dict[str, Any]:
    instruments = load_registry()["instruments"]
    if instrument_type not in instruments:
        raise ValueError(
            f"Unknown instrument type '{instrument_type}'. "
            f"Tried type/vendor pair '{instrument_type}/<any>'. "
            f"Available pairs: {_available_pairs(instruments)}"
        )
    return instruments[instrument_type]


def _vendor_entry(instrument_type: str, vendor: str) -> dict[str, Any]:
    entry = _instrument_entry(instrument_type)
    vendors = entry["vendors"]
    if vendor not in vendors:
        raise ValueError(
            f"'{vendor}' is not a supported vendor for '{instrument_type}'. "
            f"Tried type/vendor pair '{instrument_type}/{vendor}'. "
            f"Available pairs: {_available_pairs(load_registry()['instruments'])}"
        )
    return vendors[vendor]


def _available_pairs(instruments: Mapping[str, Any]) -> list[str]:
    pairs: list[str] = []
    for type_key, entry in instruments.items():
        vendors = entry.get("vendors", {}) if isinstance(entry, Mapping) else {}
        if isinstance(vendors, Mapping):
            pairs.extend(f"{type_key}/{vendor_key}" for vendor_key in vendors)
    return sorted(pairs)


def _safe_type_hints(obj: Any) -> dict[str, Any]:
    try:
        return get_type_hints(obj)
    except Exception:
        return {}


def _return_annotation(method: Any) -> Any:
    signature = inspect.signature(method)
    return _safe_type_hints(method).get("return", signature.return_annotation)


def _is_config_field_type(annotation: Any) -> bool:
    if annotation in _PRIMITIVE_CONFIG_TYPES:
        return True
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        return any(
            arg in _PRIMITIVE_CONFIG_TYPES
            for arg in get_args(annotation)
            if arg is not NoneType
        )
    return False


def _type_name(annotation: Any) -> str:
    if annotation in _PRIMITIVE_CONFIG_TYPES:
        return annotation.__name__
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        non_none_args = [arg for arg in get_args(annotation) if arg is not NoneType]
        if len(non_none_args) == 1 and non_none_args[0] in _PRIMITIVE_CONFIG_TYPES:
            return non_none_args[0].__name__
    return getattr(annotation, "__name__", str(annotation))


def _measurement_method_sort_key(name: str) -> tuple[int, str]:
    if name == "indentation":
        return (0, name)
    if name == "measure":
        return (1, name)
    return (2, name)


def _load_registry_file(path: str | Path) -> dict[str, Any]:
    resolved = Path(path)
    loaded = load_yaml_file(resolved) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Instrument registry {resolved} must be a mapping.")
    if "instruments" not in loaded:
        raise ValueError(
            f"Instrument registry {resolved} must define an 'instruments' mapping."
        )
    return copy.deepcopy(loaded)


def _entry_point_registries() -> Iterable[tuple[str, dict[str, Any]]]:
    try:
        entry_points = importlib_metadata.entry_points()
    except Exception as exc:
        logger.warning("Failed to enumerate instrument registry entry points: %s", exc)
        return []

    if hasattr(entry_points, "select"):
        selected = entry_points.select(group=_ENTRY_POINT_GROUP)
    elif isinstance(entry_points, dict):  # pragma: no cover - older API
        selected = entry_points.get(_ENTRY_POINT_GROUP, [])
    else:  # pragma: no cover - defensive for test doubles / unusual APIs
        selected = []

    overlays: list[tuple[str, dict[str, Any]]] = []
    for entry_point in selected:
        source = f"entry point {entry_point.name}"
        loaded = entry_point.load()
        overlays.append((source, _registry_from_external_source(loaded, source)))
    return overlays


def _registry_from_external_source(source_obj: Any, source: str) -> dict[str, Any]:
    if callable(source_obj) and not isinstance(source_obj, (str, Path, Mapping)):
        source_obj = source_obj()
    if isinstance(source_obj, (str, Path)):
        return _load_registry_file(source_obj)
    if isinstance(source_obj, Mapping):
        registry = copy.deepcopy(dict(source_obj))
        if "instruments" not in registry:
            raise ValueError(f"Instrument registry from {source} is missing instruments.")
        return registry
    raise TypeError(
        f"Instrument registry {source} must load to a mapping, path, or callable."
    )


def _overlay_paths() -> list[Path]:
    raw = os.environ.get(_OVERLAY_ENV_VAR, "")
    if not raw.strip():
        return []
    return [Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip()]


def _merge_registry(
    base: dict[str, Any],
    incoming: Mapping[str, Any],
    *,
    source: str,
) -> None:
    base_instruments = base.setdefault("instruments", {})
    incoming_instruments = incoming.get("instruments")
    if not isinstance(incoming_instruments, Mapping):
        raise ValueError(f"Instrument registry {source} must define instruments.")

    for type_key, raw_entry in incoming_instruments.items():
        if not isinstance(raw_entry, Mapping):
            raise ValueError(f"Instrument entry {type_key!r} from {source} must map.")
        entry = copy.deepcopy(dict(raw_entry))
        type_override = bool(entry.pop("override", False))
        vendors = copy.deepcopy(dict(entry.pop("vendors", {}) or {}))
        if not isinstance(vendors, dict):
            raise ValueError(
                f"Instrument entry {type_key!r} from {source} has invalid vendors."
            )

        if type_key not in base_instruments:
            entry["vendors"] = _clean_vendor_entries(type_key, vendors, source)
            base_instruments[type_key] = entry
            continue

        existing = base_instruments[type_key]
        for key, value in entry.items():
            if key not in existing or existing[key] == value:
                existing[key] = value
                continue
            if not type_override:
                raise ValueError(
                    f"Instrument type '{type_key}' from {source} attempts to "
                    f"change '{key}'. Add override: true to the type entry if "
                    "this is intentional."
                )
            existing[key] = value

        existing_vendors = existing.setdefault("vendors", {})
        for vendor_key, vendor_entry in _clean_vendor_entries(
            type_key, vendors, source,
        ).items():
            vendor_override = bool(vendor_entry.pop("override", False))
            if vendor_key in existing_vendors and not vendor_override:
                raise ValueError(
                    f"Vendor '{vendor_key}' for instrument type '{type_key}' "
                    f"is already registered. Add override: true to the vendor "
                    f"entry in {source} if this replacement is intentional."
                )
            existing_vendors[vendor_key] = vendor_entry


def _clean_vendor_entries(
    type_key: str,
    vendors: Mapping[str, Any],
    source: str,
) -> dict[str, dict[str, Any]]:
    cleaned: dict[str, dict[str, Any]] = {}
    for vendor_key, raw_vendor in vendors.items():
        if not isinstance(raw_vendor, Mapping):
            raise ValueError(
                f"Vendor entry '{type_key}/{vendor_key}' from {source} must map."
            )
        cleaned[vendor_key] = copy.deepcopy(dict(raw_vendor))
    return cleaned


def _validate_registry(registry: Mapping[str, Any]) -> None:
    instruments = registry.get("instruments")
    if not isinstance(instruments, Mapping) or not instruments:
        raise ValueError("Instrument registry must define at least one instrument.")
    for type_key, entry in instruments.items():
        if not isinstance(entry, Mapping):
            raise ValueError(f"Instrument entry '{type_key}' must be a mapping.")
        if not isinstance(entry.get("interface"), str) or "." not in entry["interface"]:
            raise ValueError(
                f"Instrument entry '{type_key}' must define a dotted interface."
            )
        mode = entry.get("calibration_mode", "contact")
        if mode not in _VALID_CALIBRATION_MODES:
            raise ValueError(
                f"Instrument entry '{type_key}' has invalid calibration_mode "
                f"{mode!r}; expected one of {sorted(_VALID_CALIBRATION_MODES)}."
            )
        vendors = entry.get("vendors")
        if not isinstance(vendors, Mapping) or not vendors:
            raise ValueError(
                f"Instrument entry '{type_key}' must define at least one vendor."
            )
        for vendor_key, vendor_entry in vendors.items():
            if not isinstance(vendor_entry, Mapping):
                raise ValueError(
                    f"Vendor entry '{type_key}/{vendor_key}' must be a mapping."
                )
            for required in ("module", "class_name"):
                if not isinstance(vendor_entry.get(required), str):
                    raise ValueError(
                        f"Vendor entry '{type_key}/{vendor_key}' must define "
                        f"{required}."
                    )
