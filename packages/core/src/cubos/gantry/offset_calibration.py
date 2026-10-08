"""Offline instrument-offset calibration in an existing deck coordinate frame."""

from __future__ import annotations

from copy import deepcopy
from math import isfinite
from typing import Any, Mapping


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number.") from exc
    if not isfinite(number):
        raise ValueError(f"{label} must be a finite number.")
    return number


def calibrate_instrument_offsets(
    config: Mapping[str, Any],
    reference_instrument: str,
    captures: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a config copy with offsets measured against one calibrated tool.

    Captures are WPos at the same fixed physical mark. ``tip_length_mm`` is
    the protrusion below the bare nozzle at capture; ``stand_off_mm`` is the
    camera-to-mark distance. The reference's mounting values anchor the
    existing deck frame and remain unchanged. No controller is accessed.
    """
    next_config = deepcopy(dict(config))
    instruments = next_config.get("instruments", {})
    reference = instruments.get(reference_instrument)
    if not reference or reference.get("type") in {"camera", "lighting"}:
        raise ValueError("Choose an already calibrated contact instrument as reference.")
    required = {name for name, entry in instruments.items() if entry.get("type") != "lighting"}
    if set(captures) != required:
        raise ValueError("Record every contact instrument and camera at the same fixed mark.")

    adjusted: dict[str, tuple[float, float, float]] = {}
    for name in required:
        entry = instruments[name]
        capture = captures[name]
        x, y, z = (_finite(capture.get(axis), f"{name} {axis}") for axis in ("x", "y", "z"))
        tip = _finite(capture.get("tip_length_mm", 0), f"{name} tip length")
        if tip < 0 or (tip and entry.get("type") != "pipette"):
            raise ValueError(f"{name}: tip length must be nonnegative and only applies to pipettes.")
        if entry.get("type") == "camera":
            distance = _finite(capture.get("stand_off_mm"), f"{name} camera stand-off")
            if distance < 0:
                raise ValueError(f"{name}: camera stand-off must be nonnegative.")
            z -= distance
        else:
            if capture.get("stand_off_mm") is not None:
                raise ValueError(f"{name}: stand-off only applies to cameras.")
            z -= tip
        adjusted[name] = x, y, _finite(z, f"{name} adjusted Z")

    ref_x, ref_y, ref_z = adjusted[reference_instrument]
    offset_x = _finite(reference.get("offset_x", 0), "Reference X offset")
    offset_y = _finite(reference.get("offset_y", 0), "Reference Y offset")
    depth = _finite(reference.get("depth", 0), "Reference depth")
    for name, (x, y, z) in adjusted.items():
        if name == reference_instrument:
            continue
        instruments[name].update(
            offset_x=round(_finite(offset_x + ref_x - x, f"{name} X offset"), 3),
            offset_y=round(_finite(offset_y + ref_y - y, f"{name} Y offset"), 3),
            depth=round(_finite(depth + z - ref_z, f"{name} depth"), 3),
        )
    camera = next((entry for entry in instruments.values() if entry.get("type") == "camera"), None)
    for entry in instruments.values():
        if entry.get("type") == "lighting":
            if camera is None:
                raise ValueError("Lighting offset calibration requires a camera.")
            for field in ("offset_x", "offset_y", "depth"):
                entry[field] = camera.get(field, 0)
    return next_config
