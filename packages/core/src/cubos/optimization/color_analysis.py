"""Well-local image analysis for camera color measurements."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Mapping, Sequence


PROFILE_SCHEMA = "cubos.camera-well-cielab.v1"
_VERIFIED_CENTER_SOURCES = {"operator_selected", "frame_center"}
_MIN_RADIUS_FRACTION = 0.015
_MAX_RADIUS_FRACTION = 0.08
_MAX_CENTER_OFFSET_FRACTION = 0.25
_MAX_CENTER_RESIDUAL_RADII = 1.0
_LOW_CLIP = 2
_HIGH_CLIP = 253
_GLARE_MIN = 230
_GLARE_MAX_CHROMA = 15
_MIN_VALID_PIXELS = 100
_MIN_VALID_FRACTION = 0.5
_MAX_LOW_CLIPPED_FRACTION = 0.2
_MAX_HIGH_CLIPPED_FRACTION = 0.05
_MAX_GLARE_FRACTION = 0.1
_MIN_MEDIAN_LUMINANCE = 10.0
_MAX_MEDIAN_LUMINANCE = 245.0
_DYNAMIC_ACQUISITION_KEYS = {
    "frame_id", "captured_at", "received_at", "configuration_revision",
}


def _normalized_center(
    expected_center: Sequence[float] | None,
) -> tuple[float, float]:
    if expected_center is None:
        return 0.5, 0.5
    if len(expected_center) != 2:
        raise ValueError("expected_center must contain exactly two normalized values")
    center = tuple(float(value) for value in expected_center)
    if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in center):
        raise ValueError("expected_center values must be finite and between 0 and 1")
    return center  # type: ignore[return-value]


def _stable_acquisition_value(value):
    if isinstance(value, Mapping):
        return {
            str(key): _stable_acquisition_value(item)
            for key, item in value.items()
            if key not in _DYNAMIC_ACQUISITION_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_stable_acquisition_value(item) for item in value]
    return value


def _acquisition_quality_flags(
    acquisition_context: Mapping[str, object] | None,
) -> list[str]:
    if not isinstance(acquisition_context, Mapping):
        return ["capture_profile_unverified"]
    requested = acquisition_context.get("requested_capture_profile")
    actual = acquisition_context.get("actual_capture_profile")
    requested_fingerprint = (
        requested.get("fingerprint") if isinstance(requested, Mapping) else None
    )
    actual_fingerprint = (
        actual.get("fingerprint") if isinstance(actual, Mapping) else None
    )
    if not isinstance(requested_fingerprint, str) or not requested_fingerprint:
        return ["capture_profile_unverified"]
    if not isinstance(actual_fingerprint, str) or not actual_fingerprint:
        return ["capture_profile_unverified"]
    if requested_fingerprint != actual_fingerprint:
        return ["capture_profile_changed_during_acquisition"]
    return []


def processing_profile(
    *,
    roi_fraction: float,
    expected_center: Sequence[float] | None = None,
    expected_center_source: str | None = None,
    reference_origin: str | None = None,
    acquisition_context: Mapping[str, object] | None = None,
    image_properties: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Describe every setting that must match between two measurements."""
    center = _normalized_center(expected_center)
    configuration: dict[str, object] = {
        "roi_fraction_of_detected_radius": float(roi_fraction),
        "expected_center_normalized": list(center),
        "expected_center_source": expected_center_source or "unverified_frame_center",
        "well_detector": {
            "method": "hough_circle_near_expected_center",
            "minimum_radius_fraction": _MIN_RADIUS_FRACTION,
            "maximum_radius_fraction": _MAX_RADIUS_FRACTION,
            "maximum_center_offset_fraction": _MAX_CENTER_OFFSET_FRACTION,
            "maximum_center_residual_radii": _MAX_CENTER_RESIDUAL_RADII,
        },
        "quality_mask": {
            "low_clip_max": _LOW_CLIP,
            "high_clip_min": _HIGH_CLIP,
            "neutral_glare_min": _GLARE_MIN,
            "neutral_glare_max_chroma": _GLARE_MAX_CHROMA,
            "minimum_valid_pixels": _MIN_VALID_PIXELS,
            "minimum_valid_fraction": _MIN_VALID_FRACTION,
            "maximum_low_clipped_fraction": _MAX_LOW_CLIPPED_FRACTION,
            "maximum_high_clipped_fraction": _MAX_HIGH_CLIPPED_FRACTION,
            "maximum_glare_fraction": _MAX_GLARE_FRACTION,
            "minimum_median_luminance": _MIN_MEDIAN_LUMINANCE,
            "maximum_median_luminance": _MAX_MEDIAN_LUMINANCE,
        },
        "estimator": "per_channel_median_of_valid_pixels",
        "acquisition": _stable_acquisition_value(
            acquisition_context or {"status": "unavailable"}
        ),
        "image_properties": dict(image_properties or {}),
    }
    if reference_origin is not None:
        configuration["reference_origin"] = reference_origin
    encoded = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return {
        "schema": PROFILE_SCHEMA,
        "id": f"{PROFILE_SCHEMA}:{digest}",
        "algorithm": "detected_well_inner_disc_median",
        "configuration": configuration,
        "rgb_encoding": "sRGB",
        "transfer_function": "IEC 61966-2-1 sRGB",
        "xyz_matrix": "sRGB to XYZ D65",
        "illuminant": "D65",
        "observer": "CIE 1931 2 degree",
        "reference_white_xyz": [0.95047, 1.0, 1.08883],
        "calibration_status": "uncalibrated",
        "calibration_provenance": (
            "Camera RGB interpreted as sRGB without an ICC profile or color-target "
            "correction; reported Lab is an estimate and is not traceable colorimetry."
        ),
    }


def _circle_candidates(frame, expected_x: float, expected_y: float):
    import cv2
    import numpy as np

    height, width = frame.shape[:2]
    scale = min(height, width)
    gray = cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2GRAY)
    blurred = cv2.medianBlur(gray, 7)
    min_radius = max(6, int(round(scale * _MIN_RADIUS_FRACTION)))
    max_radius = max(min_radius + 2, int(round(scale * _MAX_RADIUS_FRACTION)))
    circles = cv2.HoughCircles(
        blurred,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=max(12, min_radius * 2),
        param1=80,
        param2=20,
        minRadius=min_radius,
        maxRadius=max_radius,
    )
    if circles is None:
        return [], gray

    gradient_y, gradient_x = np.gradient(blurred.astype(float))
    gradient = np.hypot(gradient_x, gradient_y)
    gradient_reference = max(float(np.percentile(gradient, 95)), 1.0)
    yy, xx = np.ogrid[:height, :width]
    max_offset = scale * _MAX_CENTER_OFFSET_FRACTION
    candidates = []
    for x_value, y_value, radius_value in np.round(circles[0]).astype(int):
        residual = math.hypot(x_value - expected_x, y_value - expected_y)
        if residual > max_offset:
            continue
        distance_score = max(0.0, 1.0 - residual / max_offset)
        distance = np.sqrt((xx - x_value) ** 2 + (yy - y_value) ** 2)
        ring = (distance >= radius_value - 2) & (distance <= radius_value + 2)
        edge_score = min(
            1.0,
            float(np.percentile(gradient[ring], 75)) / gradient_reference,
        )
        confidence = 0.55 * edge_score + 0.45 * distance_score
        candidates.append({
            "center_x_px": int(x_value),
            "center_y_px": int(y_value),
            "radius_px": int(radius_value),
            "center_residual_px": float(residual),
            "edge_score": float(edge_score),
            "confidence": float(confidence),
        })
    candidates.sort(key=lambda candidate: candidate["center_residual_px"])
    return candidates, gray


def _write_preview(
    frame,
    destination: Path,
    *,
    expected_center: tuple[float, float],
    candidates: list[dict[str, object]],
    selected: dict[str, object] | None,
    sample_mask,
    valid_mask,
    accepted: bool,
    flags: list[str],
) -> str | None:
    import cv2
    import numpy as np

    preview = frame[:, :, :3].copy()
    expected = tuple(int(round(value)) for value in expected_center)
    cv2.drawMarker(preview, expected, (255, 0, 255), cv2.MARKER_CROSS, 24, 2)
    for candidate in candidates:
        center = (int(candidate["center_x_px"]), int(candidate["center_y_px"]))
        cv2.circle(preview, center, int(candidate["radius_px"]), (0, 165, 255), 1)
    if selected is not None:
        center = (int(selected["center_x_px"]), int(selected["center_y_px"]))
        color = (0, 180, 0) if accepted else (0, 0, 255)
        cv2.circle(preview, center, int(selected["radius_px"]), color, 2)
        cv2.circle(preview, center, int(selected["sample_radius_px"]), color, 2)
    if sample_mask is not None and valid_mask is not None:
        rejected = sample_mask & ~valid_mask
        overlay = preview.copy()
        overlay[rejected] = np.array([0, 0, 255], dtype=np.uint8)
        preview = cv2.addWeighted(overlay, 0.35, preview, 0.65, 0)
    status = "ACCEPTED" if accepted else "REJECTED"
    detail = ", ".join(flags) if flags else "quality checks passed"
    cv2.rectangle(preview, (0, 0), (preview.shape[1], 52), (0, 0, 0), -1)
    cv2.putText(preview, status, (12, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                (0, 220, 0) if accepted else (0, 0, 255), 2)
    cv2.putText(preview, detail[:110], (12, 44), cv2.FONT_HERSHEY_SIMPLEX,
                0.42, (255, 255, 255), 1)
    destination.parent.mkdir(parents=True, exist_ok=True)
    return str(destination) if cv2.imwrite(str(destination), preview) else None


def analyze_well_color_image(
    image_path: str | Path,
    *,
    roi_fraction: float,
    expected_center: Sequence[float] | None = None,
    expected_center_source: str | None = None,
    reference_origin: str | None = None,
    reference_lab: Sequence[float] | None = None,
    reference_processing_profile_id: str | None = None,
    acquisition_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Detect a well-local ROI, reject poor pixels, and return measurement evidence."""
    if not math.isfinite(roi_fraction) or not 0.0 < roi_fraction <= 1.0:
        raise ValueError("roi_fraction must be greater than 0 and at most 1")
    if reference_processing_profile_id is not None and reference_lab is None:
        raise ValueError("reference_processing_profile_id requires reference_lab")
    if reference_origin not in {
        None, "accepted_camera_measurement", "user_selected_srgb",
    }:
        raise ValueError(
            "reference_origin must be accepted_camera_measurement or user_selected_srgb"
        )
    if reference_origin == "user_selected_srgb" and reference_processing_profile_id is not None:
        raise ValueError("user-selected sRGB references cannot use a camera profile")
    center_normalized = _normalized_center(expected_center)
    if expected_center_source is not None and expected_center_source not in _VERIFIED_CENTER_SOURCES:
        allowed = ", ".join(sorted(_VERIFIED_CENTER_SOURCES))
        raise ValueError(f"expected_center_source must be one of: {allowed}")
    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"color image does not exist: {path}")
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "Color analysis requires the CubOS camera extra (opencv-python and numpy)."
        ) from exc
    frame = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if frame is None or frame.ndim != 3 or frame.shape[2] < 3:
        raise ValueError(f"color image is unreadable: {path}")
    if frame.dtype != np.uint8:
        raise ValueError(
            f"color image must contain 8-bit sRGB samples, found {frame.dtype}"
        )

    height, width = frame.shape[:2]
    profile = processing_profile(
        roi_fraction=roi_fraction,
        expected_center=center_normalized,
        expected_center_source=expected_center_source,
        reference_origin=(
            reference_origin if reference_origin == "user_selected_srgb" else None
        ),
        acquisition_context=acquisition_context,
        image_properties={
            "width_px": int(width),
            "height_px": int(height),
            "channels": int(frame.shape[2]),
            "dtype": str(frame.dtype),
        },
    )
    expected_x = center_normalized[0] * (width - 1)
    expected_y = center_normalized[1] * (height - 1)
    candidates, _ = _circle_candidates(frame, expected_x, expected_y)
    selected = candidates[0] if candidates else None
    result: dict[str, object] = {
        "image_path": str(path),
        "roi_fraction": float(roi_fraction),
        "measurement_status": "rejected",
        "comparison_status": "not_requested" if reference_lab is None else "rejected",
        "lab_interpretation": "estimated_camera_cielab",
        "processing_profile": profile,
    }
    flags: list[str] = []
    warnings = [
        "well identity is not verified by image analysis",
        "Lab is an uncalibrated camera estimate",
    ]
    if expected_center is None or expected_center_source is None:
        flags.append("expected_center_unverified")
    flags.extend(_acquisition_quality_flags(acquisition_context))
    sample_mask = valid_mask = None
    if selected is None:
        flags.append("well_not_found")
        roi: dict[str, object] = {
            "method": "detected_well_inner_disc",
            "detection_status": "not_found",
            "center_x_px": None,
            "center_y_px": None,
            "radius_px": None,
            "sample_radius_px": None,
            "expected_center_x_px": float(expected_x),
            "expected_center_y_px": float(expected_y),
            "expected_center_source": expected_center_source or "unverified_frame_center",
            "center_residual_px": None,
            "detection_confidence": 0.0,
            "candidate_count": 0,
        }
        quality: dict[str, object] = {
            "accepted": False,
            "status": "rejected",
            "score": 0.0,
            "flags": flags,
            "warnings": warnings,
            "roi_pixel_count": 0,
            "valid_pixel_count": 0,
            "excluded_pixel_count": 0,
            "valid_fraction": 0.0,
            "low_clipped_pixel_count": 0,
            "high_clipped_pixel_count": 0,
            "glare_pixel_count": 0,
            "low_clipped_fraction": 0.0,
            "high_clipped_fraction": 0.0,
            "glare_fraction": 0.0,
            "exposure_percentiles": {"p01": None, "p50": None, "p99": None},
        }
    else:
        x_value = int(selected["center_x_px"])
        y_value = int(selected["center_y_px"])
        radius = int(selected["radius_px"])
        sample_radius = max(1.0, radius * roi_fraction)
        selected["sample_radius_px"] = float(sample_radius)
        yy, xx = np.ogrid[:height, :width]
        sample_mask = (
            (xx - x_value) ** 2 + (yy - y_value) ** 2 <= sample_radius**2
        )
        bgr_pixels = frame[sample_mask, :3]
        rgb_pixels = bgr_pixels[:, ::-1]
        low_clipped = np.any(rgb_pixels <= _LOW_CLIP, axis=1)
        high_clipped = np.any(rgb_pixels >= _HIGH_CLIP, axis=1)
        glare = (
            (np.min(rgb_pixels, axis=1) >= _GLARE_MIN)
            & (
                np.max(rgb_pixels, axis=1) - np.min(rgb_pixels, axis=1)
                <= _GLARE_MAX_CHROMA
            )
        )
        valid_pixels_selector = ~(low_clipped | high_clipped | glare)
        valid_mask = np.zeros((height, width), dtype=bool)
        valid_mask[sample_mask] = valid_pixels_selector
        valid_pixels = rgb_pixels[valid_pixels_selector]
        roi_count = int(rgb_pixels.shape[0])
        valid_count = int(valid_pixels.shape[0])
        valid_fraction = valid_count / roi_count if roi_count else 0.0
        luminance = (
            0.2126 * rgb_pixels[:, 0]
            + 0.7152 * rgb_pixels[:, 1]
            + 0.0722 * rgb_pixels[:, 2]
        )
        exposure = np.percentile(luminance, [1, 50, 99])
        low_fraction = float(np.mean(low_clipped))
        high_fraction = float(np.mean(high_clipped))
        glare_fraction = float(np.mean(glare))
        confidence = float(selected["confidence"])
        residual = float(selected["center_residual_px"])
        second_residual = (
            float(candidates[1]["center_residual_px"])
            if len(candidates) > 1 else math.inf
        )
        if confidence < 0.65:
            flags.append("low_detection_confidence")
        if residual > radius * _MAX_CENTER_RESIDUAL_RADII:
            flags.append("expected_center_residual_too_large")
        if second_residual <= residual + radius * 0.5:
            flags.append("ambiguous_well_detection")
        if valid_count < _MIN_VALID_PIXELS:
            flags.append("insufficient_valid_pixels")
        if valid_fraction < _MIN_VALID_FRACTION:
            flags.append("low_valid_fraction")
        if low_fraction > _MAX_LOW_CLIPPED_FRACTION:
            flags.append("excessive_low_clipping")
        if high_fraction > _MAX_HIGH_CLIPPED_FRACTION:
            flags.append("excessive_high_clipping")
        if glare_fraction > _MAX_GLARE_FRACTION:
            flags.append("excessive_glare")
        if float(exposure[1]) < _MIN_MEDIAN_LUMINANCE:
            flags.append("underexposed")
        if float(exposure[1]) > _MAX_MEDIAN_LUMINANCE:
            flags.append("overexposed")
        accepted = not flags
        exposure_score = min(
            1.0,
            max(0.0, float(exposure[1]) / _MIN_MEDIAN_LUMINANCE),
            max(0.0, (255.0 - float(exposure[1])) / (255.0 - _MAX_MEDIAN_LUMINANCE)),
        )
        score = min(confidence, valid_fraction, exposure_score)
        roi = {
            "method": "detected_well_inner_disc",
            "detection_status": "selected",
            "center_x_px": x_value,
            "center_y_px": y_value,
            "radius_px": radius,
            "sample_radius_px": float(sample_radius),
            "expected_center_x_px": float(expected_x),
            "expected_center_y_px": float(expected_y),
            "expected_center_source": expected_center_source or "unverified_frame_center",
            "center_residual_px": float(selected["center_residual_px"]),
            "detection_confidence": confidence,
            "candidate_count": len(candidates),
        }
        quality = {
            "accepted": accepted,
            "status": "accepted" if accepted else "rejected",
            "score": float(score),
            "flags": flags,
            "warnings": warnings,
            "roi_pixel_count": roi_count,
            "valid_pixel_count": valid_count,
            "excluded_pixel_count": roi_count - valid_count,
            "valid_fraction": float(valid_fraction),
            "low_clipped_pixel_count": int(np.count_nonzero(low_clipped)),
            "high_clipped_pixel_count": int(np.count_nonzero(high_clipped)),
            "glare_pixel_count": int(np.count_nonzero(glare)),
            "low_clipped_fraction": low_fraction,
            "high_clipped_fraction": high_fraction,
            "glare_fraction": glare_fraction,
            "exposure_percentiles": {
                "p01": float(exposure[0]),
                "p50": float(exposure[1]),
                "p99": float(exposure[2]),
            },
        }
        if accepted:
            red, green, blue = (
                float(value) for value in np.median(valid_pixels, axis=0)
            )
            result["rgb"] = [red, green, blue]
            result["measurement_status"] = "accepted"

    result["roi"] = roi
    result["quality"] = quality
    preview_path = path.with_name(f"{path.stem}.analysis.png")
    written_preview = _write_preview(
        frame,
        preview_path,
        expected_center=(expected_x, expected_y),
        candidates=candidates,
        selected=selected,
        sample_mask=sample_mask,
        valid_mask=valid_mask,
        accepted=result["measurement_status"] == "accepted",
        flags=flags,
    )
    if written_preview is not None:
        result["annotated_preview_path"] = written_preview
    return result
