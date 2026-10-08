from pathlib import Path

import pytest

from cubos.optimization import analyze_color_image, ciede2000, ciede76, rgb_to_lab
from cubos.optimization import color_analysis


def _acquisition(height=20.0, *, fingerprint="stable", revision=1):
    profile = {"fingerprint": fingerprint, "configuration_revision": revision}
    return {
        "requested_capture_profile": profile,
        "actual_capture_profile": profile,
        "image_height": height,
    }


def _write_well_image(tmp_path, *, rgb=(120, 80, 40), center=(220, 160)):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    image = np.full((300, 400, 3), 210, dtype=np.uint8)
    cv2.circle(image, center, 21, (20, 20, 20), 3)
    cv2.circle(image, center, 18, tuple(reversed(rgb)), -1)
    path = tmp_path / "well.png"
    assert cv2.imwrite(str(path), image)
    return path


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
        ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
        ((50.0, 2.8361, -74.0200), (50.0, 0.0, -82.7485), 3.4412),
    ],
)
def test_ciede2000_matches_reference_pairs(first, second, expected):
    assert ciede2000(first, second) == pytest.approx(expected, abs=0.0001)


def test_rgb_to_lab_matches_d65_black_white_and_red():
    assert rgb_to_lab((0, 0, 0)) == pytest.approx((0, 0, 0), abs=0.001)
    assert rgb_to_lab((255, 255, 255)) == pytest.approx((100, 0, 0), abs=0.02)
    assert rgb_to_lab((255, 0, 0)) == pytest.approx(
        (53.2408, 80.0925, 67.2032), abs=0.02
    )


def test_ciede76_is_euclidean_lab_distance():
    assert ciede76((10, 20, 30), (13, 24, 30)) == 5


def test_color_inputs_are_strictly_bounded():
    with pytest.raises(ValueError, match="between 0 and 255"):
        rgb_to_lab((256, 0, 0))
    with pytest.raises(ValueError, match="exactly three"):
        ciede2000((1, 2), (1, 2, 3))


def test_well_analysis_uses_detected_inner_disc_and_records_provenance(tmp_path):
    path = _write_well_image(tmp_path)
    result = analyze_color_image(
        path,
        expected_center=(220 / 399, 160 / 299),
        expected_center_source="operator_selected",
        acquisition_context=_acquisition(),
    )

    assert result["measurement_status"] == "accepted"
    assert result["comparison_status"] == "not_requested"
    assert result["rgb"] == pytest.approx([120, 80, 40], abs=1)
    assert result["roi"]["center_x_px"] == pytest.approx(220, abs=2)
    assert result["roi"]["center_y_px"] == pytest.approx(160, abs=2)
    assert result["roi"]["sample_radius_px"] < result["roi"]["radius_px"]
    assert result["quality"]["accepted"] is True
    assert result["processing_profile"]["schema"] == "cubos.camera-well-cielab.v1"
    assert result["processing_profile"]["calibration_status"] == "uncalibrated"
    assert result["lab_interpretation"] == "estimated_camera_cielab"
    assert Path(result["annotated_preview_path"]).is_file()


def test_well_analysis_requires_verified_expected_center_for_quantitation(tmp_path):
    result = analyze_color_image(_write_well_image(tmp_path))

    assert result["measurement_status"] == "rejected"
    assert "expected_center_unverified" in result["quality"]["flags"]
    assert "lab" not in result


def test_well_analysis_rejects_stale_expected_center(tmp_path):
    result = analyze_color_image(
        _write_well_image(tmp_path),
        expected_center=(0.5, 0.5),
        expected_center_source="operator_selected",
        acquisition_context=_acquisition(),
    )

    assert result["measurement_status"] == "rejected"
    assert "expected_center_residual_too_large" in result["quality"]["flags"]
    assert "lab" not in result


@pytest.mark.parametrize(
    ("acquisition", "expected_flag"),
    [
        (None, "capture_profile_unverified"),
        (
            {
                "requested_capture_profile": {"fingerprint": "before"},
                "actual_capture_profile": {"fingerprint": "after"},
            },
            "capture_profile_changed_during_acquisition",
        ),
    ],
)
def test_well_analysis_requires_stable_capture_profile(
    tmp_path, acquisition, expected_flag
):
    result = analyze_color_image(
        _write_well_image(tmp_path),
        expected_center=(220 / 399, 160 / 299),
        expected_center_source="operator_selected",
        acquisition_context=acquisition,
    )

    assert result["measurement_status"] == "rejected"
    assert expected_flag in result["quality"]["flags"]
    assert "lab" not in result


def test_comparison_requires_exact_processing_profile(tmp_path):
    path = _write_well_image(tmp_path)
    target = analyze_color_image(
        path,
        expected_center=(220 / 399, 160 / 299),
        expected_center_source="operator_selected",
        acquisition_context=_acquisition(revision=1),
    )
    accepted = analyze_color_image(
        path,
        expected_center=(220 / 399, 160 / 299),
        expected_center_source="operator_selected",
        reference_lab=target["lab"],
        reference_processing_profile_id=target["processing_profile"]["id"],
        acquisition_context=_acquisition(revision=2),
    )
    incompatible = analyze_color_image(
        path,
        expected_center=(220 / 399, 160 / 299),
        expected_center_source="operator_selected",
        reference_lab=target["lab"],
        reference_processing_profile_id=target["processing_profile"]["id"],
        acquisition_context=_acquisition(height=21.0),
    )

    assert accepted["comparison_status"] == "accepted"
    assert accepted["delta_e_00"] == pytest.approx(0)
    assert incompatible["measurement_status"] == "accepted"
    assert incompatible["comparison_status"] == "rejected"
    assert incompatible["comparison_error"] == "incompatible_processing_profile"
    assert "delta_e_00" not in incompatible


def test_well_analysis_rejects_clipped_dark_well(tmp_path):
    result = analyze_color_image(
        _write_well_image(tmp_path, rgb=(0, 0, 0)),
        expected_center=(220 / 399, 160 / 299),
        expected_center_source="operator_selected",
        acquisition_context=_acquisition(),
    )

    assert result["measurement_status"] == "rejected"
    assert "excessive_low_clipping" in result["quality"]["flags"]
    assert result["quality"]["valid_pixel_count"] == 0
    assert "rgb" not in result


@pytest.mark.parametrize(
    ("rgb", "expected_flag"),
    [
        ((255, 255, 255), "excessive_high_clipping"),
        ((240, 240, 240), "excessive_glare"),
    ],
)
def test_well_analysis_rejects_bright_invalid_pixels(tmp_path, rgb, expected_flag):
    result = analyze_color_image(
        _write_well_image(tmp_path, rgb=rgb),
        expected_center=(220 / 399, 160 / 299),
        expected_center_source="operator_selected",
        acquisition_context=_acquisition(),
    )

    assert result["measurement_status"] == "rejected"
    assert expected_flag in result["quality"]["flags"]
    assert result["quality"]["excluded_pixel_count"] > 100


def test_well_analysis_reports_missing_well(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    path = tmp_path / "blank.png"
    assert cv2.imwrite(str(path), np.full((300, 400, 3), 128, dtype=np.uint8))

    result = analyze_color_image(
        path,
        expected_center=(0.5, 0.5),
        expected_center_source="operator_selected",
        acquisition_context=_acquisition(),
    )

    assert result["measurement_status"] == "rejected"
    assert result["roi"]["detection_status"] == "not_found"
    assert result["quality"]["flags"] == ["well_not_found"]


def test_well_analysis_rejects_ambiguous_or_weak_detection(tmp_path, monkeypatch):
    path = _write_well_image(tmp_path)
    candidates = [
        {
            "center_x_px": 220,
            "center_y_px": 160,
            "radius_px": 20,
            "center_residual_px": 0.0,
            "edge_score": 0.1,
            "confidence": 0.5,
        },
        {
            "center_x_px": 224,
            "center_y_px": 160,
            "radius_px": 20,
            "center_residual_px": 4.0,
            "edge_score": 0.1,
            "confidence": 0.5,
        },
    ]
    monkeypatch.setattr(
        color_analysis,
        "_circle_candidates",
        lambda *args: (candidates, None),
    )

    result = analyze_color_image(
        path,
        expected_center=(220 / 399, 160 / 299),
        expected_center_source="operator_selected",
        acquisition_context=_acquisition(),
    )

    assert result["measurement_status"] == "rejected"
    assert "low_detection_confidence" in result["quality"]["flags"]
    assert "ambiguous_well_detection" in result["quality"]["flags"]


def test_well_analysis_validates_inputs_and_image_type(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    path = _write_well_image(tmp_path)
    with pytest.raises(ValueError, match="roi_fraction"):
        analyze_color_image(path, roi_fraction=0)
    with pytest.raises(ValueError, match="requires reference_lab"):
        analyze_color_image(path, reference_processing_profile_id="profile")
    with pytest.raises(ValueError, match="exactly two"):
        analyze_color_image(path, expected_center=(0.5,))
    with pytest.raises(ValueError, match="between 0 and 1"):
        analyze_color_image(path, expected_center=(1.1, 0.5))
    with pytest.raises(ValueError, match="expected_center_source"):
        analyze_color_image(path, expected_center_source="registered_calibration")
    with pytest.raises(ValueError, match="does not exist"):
        analyze_color_image(tmp_path / "missing.png")

    grayscale = tmp_path / "grayscale.png"
    assert cv2.imwrite(str(grayscale), np.full((40, 40), 128, dtype=np.uint8))
    with pytest.raises(ValueError, match="unreadable"):
        analyze_color_image(grayscale)
    sixteen_bit = tmp_path / "sixteen-bit.png"
    assert cv2.imwrite(
        str(sixteen_bit), np.full((40, 40, 3), 1000, dtype=np.uint16),
    )
    with pytest.raises(ValueError, match="8-bit sRGB"):
        analyze_color_image(sixteen_bit)
