"""Multi-instrument gantry calibration flow.

Internal implementation used by the sole user-facing entrypoint:
``packages/core/src/cubos/tools/calibrate_gantry.py``.
"""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))

from cubos.gantry import Gantry, load_gantry_from_yaml  # noqa: E402
from cubos.gantry.gantry_config import OriginPolicy  # noqa: E402
from cubos.gantry.origin import validate_working_volume_origin  # noqa: E402
from cubos.instruments.registry import get_calibration_mode  # noqa: E402
from cubos.tools.calibration.single_instrument_calibration import (  # noqa: E402
    _assert_near_xyz,
    _apply_calibration_grbl_baseline,
    _calculate_block_z_calibration,
    _calculate_grbl_max_travel,
    _calibration_block_height_mm,
    _cancel_jog_if_available,
    _factory_z_travel_mm,
    _configured_grbl_setting,
    _interactive_jog_to_reference,
    _load_raw_config,
    _maybe_write_gantry_yaml,
    _print_yaml_block,
    _restore_hard_limits_after_origin_jog,
    _restore_soft_limits_after_origin_jog,
    _round_mm,
    _set_serial_timeout_if_available,
    _temporarily_disable_soft_limits_for_origin_jog,
    _temporarily_enable_hard_limits_for_origin_jog,
    _wait_until_idle_if_available,
)
from cubos.tools.keyboard_input import flush_stdin, read_keypress_batch  # noqa: E402


KeyReader = Callable[[], tuple[str, int]]


@dataclass(frozen=True)
class MultiInstrumentCalibrationResult:
    """Result of a multi-instrument gantry calibration run."""

    measured_working_volume: tuple[float, float, float]
    xy_bounds_after_origin: tuple[float, float, float]
    xy_origin_verification: tuple[float, float, float]
    z_origin_verification: tuple[float, float, float]
    instrument_calibrations: dict[str, dict[str, float]]
    grbl_max_travel: tuple[float, float, float]
    reference_instrument: str
    lowest_instrument: str
    block_reference_coordinates: dict[str, tuple[float, float, float]]


def _coords_tuple(coords: dict[str, float]) -> tuple[float, float, float]:
    return (float(coords["x"]), float(coords["y"]), float(coords["z"]))


def _assert_near_xy_origin(
    coords: dict[str, float],
    *,
    tolerance_mm: float,
) -> None:
    misses = [
        f"{axis}: got {float(coords[axis]):.4f}, expected 0.0000"
        for axis in ("x", "y")
        if abs(float(coords[axis])) > tolerance_mm
    ]
    if misses:
        raise RuntimeError(
            "Deck-origin XY reference did not verify within "
            f"{tolerance_mm} mm: " + "; ".join(misses)
        )


def compute_relative_instrument_calibrations(
    *,
    block_coordinates: dict[str, dict[str, float]],
    reference_instrument: str,
    lowest_instrument: str,
) -> dict[str, dict[str, float]]:
    """Compute offsets/depths from one shared, arbitrary block point.

    The block does not need known deck-frame X/Y/Z coordinates. The reference
    instrument defines zero XY offset, and the lowest instrument defines zero
    depth after the Z-reference step. For every other instrument, touching the
    same physical block point gives the relative WPos deltas needed by
    InstrumentedGantry.move() semantics:
        offset_i = offset_ref + gantry_ref - gantry_i
        depth_i = depth_lowest + gantry_i_z - gantry_lowest_z
    with offset_ref=(0, 0) and depth_lowest=0 in this calibration flow.
    """
    missing = [
        name
        for name in (reference_instrument, lowest_instrument)
        if name not in block_coordinates
    ]
    if missing:
        raise ValueError(
            "Missing block coordinate(s) for required baseline instrument(s): "
            + ", ".join(missing)
        )
    reference_coords = block_coordinates[reference_instrument]
    lowest_coords = block_coordinates[lowest_instrument]
    calibrations: dict[str, dict[str, float]] = {}
    for instrument, coords in block_coordinates.items():
        calibrations[instrument] = {
            "offset_x": _round_mm(
                float(reference_coords["x"]) - float(coords["x"])
            ),
            "offset_y": _round_mm(
                float(reference_coords["y"]) - float(coords["y"])
            ),
            "depth": _round_mm(float(coords["z"]) - float(lowest_coords["z"])),
        }
    return calibrations


def compute_non_contact_block_calibration(
    *,
    block_reference_coordinates: dict[str, float],
    non_contact_coordinates: dict[str, float],
    block_height_mm: float,
    height_above_block_mm: float,
) -> dict[str, float]:
    """Compute non-contact mount fields from a centered-over-block pose.

    ``block_reference_coordinates`` is the WPos pose recorded when the
    reference contact instrument was at the shared block point.
    ``non_contact_coordinates`` is the WPos pose recorded after the operator
    centers the non-contact instrument over that same block mark. The entered
    height defines the non-contact reference point's distance above the block
    top.
    """
    if height_above_block_mm < 0:
        raise ValueError(
            "Non-contact distance from calibration block must be >= 0 mm."
        )
    return {
        "offset_x": _round_mm(
            float(block_reference_coordinates["x"])
            - float(non_contact_coordinates["x"])
        ),
        "offset_y": _round_mm(
            float(block_reference_coordinates["y"])
            - float(non_contact_coordinates["y"])
        ),
        "depth": _round_mm(
            float(non_contact_coordinates["z"])
            - (float(block_height_mm) + float(height_above_block_mm))
        ),
    }


def _build_grbl_settings(
    raw_config: dict[str, Any],
    max_travel: dict[str, float],
    homing_pull_off_mm: float | None = None,
) -> dict[str, Any]:
    settings = dict(raw_config.get("grbl_settings") or {})
    settings.update(
        {
            "status_report": 0,
            "soft_limits": True,
            "homing_enable": True,
            "max_travel_x": max_travel["max_travel_x"],
            "max_travel_y": max_travel["max_travel_y"],
            "max_travel_z": max_travel["max_travel_z"],
        }
    )
    if homing_pull_off_mm is not None:
        settings["homing_pull_off"] = homing_pull_off_mm
    return settings


def _updated_yaml_text(
    raw_config: dict[str, Any],
    *,
    measured_coords: dict[str, float],
    instrument_calibrations: dict[str, dict[str, float]],
    max_travel: dict[str, float],
    z_min_mm: float,
    z_max_mm: float,
    calibration_block_height_mm: float,
    homing_pull_off_mm: float | None = None,
    origin_policy: str = "deck_origin",
) -> str:
    updated = copy.deepcopy(raw_config)
    if origin_policy == OriginPolicy.HOME_ORIGIN.value:
        updated["origin_policy"] = OriginPolicy.HOME_ORIGIN.value
        updated["working_volume"] = {
            "x_min": _round_mm(-measured_coords["x"]),
            "x_max": 0.0,
            "y_min": _round_mm(-measured_coords["y"]),
            "y_max": 0.0,
            "z_min": _round_mm(z_min_mm - z_max_mm),
            "z_max": 0.0,
        }
    else:
        updated["working_volume"] = {
            "x_min": 0.0,
            "x_max": _round_mm(measured_coords["x"]),
            "y_min": 0.0,
            "y_max": _round_mm(measured_coords["y"]),
            "z_min": _round_mm(z_min_mm),
            "z_max": _round_mm(z_max_mm),
        }
    updated["grbl_settings"] = _build_grbl_settings(
        raw_config,
        max_travel,
        homing_pull_off_mm=homing_pull_off_mm,
    )
    cnc = updated.setdefault("cnc", {})
    if not isinstance(cnc, dict):
        raise ValueError("Input gantry YAML cnc section must be a mapping.")
    cnc["calibration_block_height_mm"] = _round_mm(calibration_block_height_mm)

    instruments = updated.setdefault("instruments", {})
    for name, calibration in instrument_calibrations.items():
        entry = instruments.setdefault(name, {})
        entry.update(calibration)

    return yaml.safe_dump(updated, sort_keys=False)


def _validate_instrument_names(
    raw_config: dict[str, Any],
    names: Sequence[str],
) -> None:
    instruments = raw_config.get("instruments")
    if not isinstance(instruments, dict) or not instruments:
        raise ValueError("Gantry YAML must define top-level instruments to calibrate.")
    missing = [name for name in names if name not in instruments]
    if missing:
        available = ", ".join(sorted(instruments.keys()))
        raise ValueError(
            "Unknown instrument(s): "
            + ", ".join(missing)
            + f". Available instruments: {available}"
        )


def _instrument_names(raw_config: dict[str, Any]) -> tuple[str, ...]:
    instruments = raw_config.get("instruments")
    if not isinstance(instruments, dict) or not instruments:
        raise ValueError("Gantry YAML must define top-level instruments to calibrate.")
    return tuple(instruments.keys())


def _instrument_type(raw_config: dict[str, Any], name: str) -> str | None:
    instruments = raw_config.get("instruments")
    if not isinstance(instruments, dict):
        return None
    entry = instruments.get(name)
    if not isinstance(entry, dict):
        return None
    value = entry.get("type")
    return str(value) if value is not None else None


def _non_contact_instrument_names(raw_config: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        name
        for name in _instrument_names(raw_config)
        if (
            _instrument_type(raw_config, name) is not None
            and get_calibration_mode(_instrument_type(raw_config, name)) == "non_contact"
        )
    )


def _follow_camera_instrument_names(raw_config: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        name
        for name in _instrument_names(raw_config)
        if (
            _instrument_type(raw_config, name) is not None
            and get_calibration_mode(_instrument_type(raw_config, name))
            == "follow_camera"
        )
    )


def _contact_instrument_names(raw_config: dict[str, Any]) -> tuple[str, ...]:
    skip = set(_non_contact_instrument_names(raw_config))
    skip.update(_follow_camera_instrument_names(raw_config))
    return tuple(
        name for name in _instrument_names(raw_config) if name not in skip
    )


def _unique_instrument_sequence(names: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            ordered.append(name)
            seen.add(name)
    return tuple(ordered)


def _prompt_z_reference_height(
    *,
    input_reader: Callable[[str], str],
    output: Callable[[str], None],
    max_height_mm: float | None = None,
) -> float:
    output("")
    output(
        "Z reference: use a calibration block (or any rigid, flat-topped reference of known "
        "height that every instrument can reach). The lowest instrument should touch its top."
    )
    while True:
        raw = input_reader("Calibration block height in mm: ").strip()
        try:
            value = float(raw)
        except ValueError:
            output("Enter a numeric block height in millimeters.")
            continue
        if value <= 0:
            output("Calibration block height must be > 0 mm.")
            continue
        if max_height_mm is not None and value > max_height_mm:
            output(
                "Calibration block height must be <= configured factory Z travel "
                f"({max_height_mm:g} mm)."
            )
            continue
        return value


def _prompt_non_contact_block_distance(
    *,
    instrument_name: str,
    input_reader: Callable[[str], str],
    output: Callable[[str], None],
) -> float:
    output("")
    output(
        "Measure the height from the calibration block top to "
        f"{instrument_name}'s non-contact reference point now."
    )
    output("Keep the instrument centered over the block mark.")
    while True:
        raw = input_reader(
            "Distance from calibration block top to "
            f"{instrument_name} reference point in mm: "
        ).strip()
        try:
            value = float(raw)
        except ValueError:
            output("Enter a numeric distance in millimeters.")
            continue
        if value < 0:
            output("Distance from calibration block must be >= 0 mm.")
            continue
        return value


def _looks_like_serial_device_not_configured(exc: Exception) -> bool:
    message = str(exc).lower()
    return "device not configured" in message


def _home_with_serial_reconnect(
    gantry: Gantry,
    *,
    output: Callable[[str], None],
) -> None:
    try:
        gantry.home()
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        if not _looks_like_serial_device_not_configured(exc):
            raise
        output(
            "Serial device disappeared during homing ('Device not configured'). "
            "Reconnecting once, then retrying $H."
        )
        gantry.disconnect()
        gantry.connect()
        gantry.home()


def _move_to_xy_center(
    gantry: Gantry,
    bounds_coords: dict[str, float],
    *,
    output: Callable[[str], None],
    label: str,
) -> dict[str, float]:
    center_x = _round_mm(float(bounds_coords["x"]) / 2.0)
    center_y = _round_mm(float(bounds_coords["y"]) / 2.0)
    z = float(bounds_coords["z"])
    output(
        f"Moving to deck XY center before {label}: "
        f"X={center_x:.3f} Y={center_y:.3f} while keeping Z={z:.3f}."
    )
    gantry.move_to(center_x, center_y, z)
    _wait_until_idle_if_available(gantry)
    return dict(gantry.get_coordinates())


def _retract_up_after_contact(
    gantry: Gantry,
    *,
    retract_z_mm: float,
    feed_rate: float,
    output: Callable[[str], None],
) -> None:
    if retract_z_mm <= 0:
        return
    output(
        f"Raising Z by {retract_z_mm:g} mm before moving to the next tool/reference step."
    )
    gantry.jog(z=retract_z_mm, feed_rate=feed_rate)
    _wait_until_idle_if_available(gantry)


def run_multi_instrument_calibration(
    gantry_path: Path,
    *,
    reference_instrument: str | None = None,
    lowest_instrument: str | None = None,
    instruments_to_calibrate: Sequence[str] | None = None,
    dry_run: bool = False,
    tolerance_mm: float = 0.25,
    jog_step_mm: float = 1.0,
    jog_feed_rate: float = 2000.0,
    post_contact_retract_z_mm: float = 15.0,
    skip_soft_limit_config: bool = False,
    write_gantry_yaml: bool = False,
    output_gantry_path: Path | None = None,
    backup_existing_output: bool = False,
    homing_serial_timeout_s: float = 10.0,
    jog_serial_timeout_s: float = 1.0,
    output: Callable[[str], None] = print,
    input_reader: Callable[[str], str] = input,
    gantry_factory: Callable[..., Gantry] = Gantry,
    key_reader: KeyReader = read_keypress_batch,
    stdin_flusher: Callable[[], None] = flush_stdin,
) -> MultiInstrumentCalibrationResult | None:
    """Run the guided multi-instrument calibration flow."""
    gantry_path = gantry_path.resolve()
    gantry_config = load_gantry_from_yaml(gantry_path)
    validate_working_volume_origin(gantry_config)
    origin_policy = OriginPolicy(gantry_config.origin_policy)
    origin_corner = (
        "back-right-top" if origin_policy is OriginPolicy.HOME_ORIGIN else "front-left"
    )
    raw_config = _load_raw_config(gantry_path)
    factory_z_travel_mm = _factory_z_travel_mm(raw_config)
    if output_gantry_path is not None:
        output_gantry_path = output_gantry_path.resolve()
    available_instruments = _instrument_names(raw_config)
    contact_instruments = _contact_instrument_names(raw_config)
    non_contact_instruments = set(_non_contact_instrument_names(raw_config))
    follow_camera_instruments = set(_follow_camera_instrument_names(raw_config))
    if not contact_instruments:
        raise ValueError(
            "Multi-instrument calibration requires at least one contact-capable "
            "instrument. Non-contact instruments are calibrated after a contact "
            "instrument establishes the block reference."
        )
    output(f"Loaded {origin_policy.value} gantry config: {gantry_path}")
    output("Calibration overview:")
    output(
        f"  Step 1: place the origin block/artifact at the {origin_corner} corner and "
        "jog the reference tool over it to set WPos X=0, Y=0."
    )
    output("  Step 2: attach all instruments and touch the lowest tool to the same point to set Z.")
    output("")
    reference_instrument = reference_instrument or _prompt_instrument_name(
        f"Pick the number for the first/left-most tool for {origin_corner} origin",
        contact_instruments,
        raw_config=raw_config,
        input_reader=input_reader,
        output=output,
    )
    instruments = tuple(instruments_to_calibrate or available_instruments)
    names_to_validate = [reference_instrument, *instruments]
    if lowest_instrument is not None:
        names_to_validate.append(lowest_instrument)
    _validate_instrument_names(raw_config, names_to_validate)
    if reference_instrument in non_contact_instruments:
        raise ValueError(
            f"Reference instrument {reference_instrument!r} cannot be non-contact; "
            "choose a contact-capable instrument."
        )
    if lowest_instrument in non_contact_instruments:
        raise ValueError(
            f"Lowest instrument {lowest_instrument!r} cannot be non-contact; "
            "choose a contact-capable instrument."
        )
    for role, name in (("Reference", reference_instrument), ("Lowest", lowest_instrument)):
        if name in follow_camera_instruments:
            raise ValueError(
                f"{role} instrument {name!r} follows the camera's calibration "
                "and has no calibration step; choose a contact-capable instrument."
            )
    if dry_run:
        output(f"Loaded {origin_policy.value} gantry config: {gantry_path}")
        output("Dry run only. Physical calibration flow:")
        output("  $H")
        output("  temporarily disable stale GRBL soft limits during calibration jogs")
        output("  attach the first/left-most tool at the homed pose")
        output(f"  place an origin block/artifact at the {origin_corner} corner")
        output("  jog that tool's active tip/probe point over the X mark as closely as possible")
        output("  G10 L20 P1 X0 Y0  # XY only, do not set Z here")
        output("  $H and read X/Y bounds")
        output("  move to measured X/Y center for calibration-block work")
        output("  attach all instruments and jog lowest instrument to the shared Z/block point")
        output("  enter the calibration block height")
        output("  G10 L20 P1 Z<block_height>")
        output("  record the lowest instrument's X/Y/Z block coordinate immediately")
        output("  jog each remaining contact instrument to that same block point and compute offsets/depths")
        if non_contact_instruments:
            output(
                "  center each non-contact instrument over the block, enter "
                "its block distance, and record its pose"
            )
        if follow_camera_instruments:
            output(
                "  lighting instruments are skipped: they reuse the camera's "
                "calibration"
            )
        output("  $H and read final working-volume maxima")
        return None

    z_reference_height = _calibration_block_height_mm(
        raw_config,
        explicit_block_height_mm=_prompt_z_reference_height(
            input_reader=input_reader,
            output=output,
            max_height_mm=factory_z_travel_mm,
        ),
        max_height_mm=factory_z_travel_mm,
    )

    output("Preflight:")
    output("  - Keep E-stop reachable.")
    output(f"  - First/left-most tool for {origin_corner} origin: {reference_instrument}")
    if lowest_instrument is None:
        output("  - Lowest mounted tool will be selected after all instruments are attached.")
    else:
        output(f"  - Lowest mounted tool for Z/reference point: {lowest_instrument}")
    output(
        "  - Calibration block/reference point: place it near the deck center, "
        "reachable by every instrument."
    )
    output("")

    gantry_runtime_config = copy.deepcopy(raw_config)
    gantry_runtime_config.pop("grbl_settings", None)
    gantry = gantry_factory(config=gantry_runtime_config)
    restore_soft_limits_after_calibration = False
    restore_hard_limits_after_calibration = False
    try:
        output("Connecting to gantry...")
        gantry.connect()

        _apply_calibration_grbl_baseline(gantry, raw_config, output=output)
        output("Homing to normalized BRT corner...")
        _set_serial_timeout_if_available(gantry, homing_serial_timeout_s)
        _home_with_serial_reconnect(gantry, output=output)
        _set_serial_timeout_if_available(gantry, jog_serial_timeout_s)
        output("Forcing GRBL WPos status reporting ($10=0), G90, G54, and clearing G92...")
        gantry.enforce_work_position_reporting()
        gantry.activate_work_coordinate_system("G54")
        gantry.clear_g92_offsets()
        restore_soft_limits_after_calibration = (
            _temporarily_disable_soft_limits_for_origin_jog(
                gantry,
                output=output,
            )
        )
        restore_hard_limits_after_calibration = (
            _temporarily_enable_hard_limits_for_origin_jog(
                gantry,
                output=output,
            )
        )

        output(
            f"Attach {reference_instrument!r} at the homed pose. "
            f"Place the origin block/artifact at the {origin_corner} corner."
        )
        _interactive_jog_to_reference(
            gantry,
            target_description=(
                "Step 1: jog the tool's tip/probe point over the origin mark "
                "as closely as possible. Do not set Z in this step."
            ),
            confirmation_description=(
                "Press ENTER to set WPos X=0, Y=0 here. Z is not changed in this step."
            ),
            key_reader=key_reader,
            stdin_flusher=stdin_flusher,
            output=output,
            feed_rate=jog_feed_rate,
            initial_step_mm=jog_step_mm,
            limit_pull_off_mm=5.0,
        )
        output("Setting current physical pose to WPos X=0, Y=0 only...")
        gantry.set_work_coordinates(x=0.0, y=0.0)
        _wait_until_idle_if_available(gantry)
        xy_origin_coords = dict(gantry.get_coordinates())
        _assert_near_xy_origin(xy_origin_coords, tolerance_mm=tolerance_mm)

        # Re-enable soft limits before dropping the hard-limit backstop.
        if restore_soft_limits_after_calibration:
            restore_soft_limits_after_calibration = False
            _restore_soft_limits_after_origin_jog(gantry, output=output)
        if restore_hard_limits_after_calibration:
            restore_hard_limits_after_calibration = False
            _restore_hard_limits_after_origin_jog(gantry, output=output)

        output("Re-homing after XY origining to measure machine-derived X/Y bounds...")
        _set_serial_timeout_if_available(gantry, homing_serial_timeout_s)
        _home_with_serial_reconnect(gantry, output=output)
        _set_serial_timeout_if_available(gantry, jog_serial_timeout_s)
        stdin_flusher()
        _wait_until_idle_if_available(gantry)
        xy_bounds_coords = dict(gantry.get_coordinates())

        center_before_z_coords = _move_to_xy_center(
            gantry,
            xy_bounds_coords,
            output=output,
            label="lowest-instrument Z calibration",
        )
        stdin_flusher()
        output(
            "Attach/verify all mounted instruments at the deck XY center before setting Z."
        )
        if lowest_instrument is None:
            lowest_instrument = _prompt_instrument_name(
                "Pick the number for the lowest mounted tool / first Z-reference touch",
                contact_instruments,
                raw_config=raw_config,
                input_reader=input_reader,
                output=output,
            )
            _validate_instrument_names(raw_config, (lowest_instrument,))
        block_touch_coords = _interactive_jog_to_reference(
            gantry,
            target_description=(
                f"Step 2: from the deck XY center "
                f"(X={center_before_z_coords['x']:.3f}, "
                f"Y={center_before_z_coords['y']:.3f}), jog the lowest instrument "
                f"({lowest_instrument!r}) to the shared calibration block/reference point. "
                "This one touch records its X/Y/Z and defines Z."
            ),
            confirmation_description=(
                "Press ENTER when this lowest instrument is touching the shared point. "
                "X/Y will not be changed when assigning the Z work coordinate, and this "
                "instrument will not be requested again in the per-instrument pass."
            ),
            key_reader=key_reader,
            stdin_flusher=stdin_flusher,
            output=output,
            feed_rate=jog_feed_rate,
            initial_step_mm=jog_step_mm,
            limit_pull_off_mm=5.0,
        )
        block_z_calibration = _calculate_block_z_calibration(
            initial_home_z_mm=float(xy_bounds_coords["z"]),
            block_touch_wpos_z_mm=float(block_touch_coords["z"]),
            block_height_mm=z_reference_height,
            factory_z_travel_mm=factory_z_travel_mm,
            tolerance_mm=tolerance_mm,
        )
        output(
            f"Setting current physical pose to WPos Z={z_reference_height:g} only..."
        )
        gantry.set_work_coordinates(z=z_reference_height)
        _wait_until_idle_if_available(gantry)
        z_origin_coords = dict(gantry.get_coordinates())
        _assert_near_xyz(
            z_origin_coords,
            expected={
                "x": z_origin_coords["x"],
                "y": z_origin_coords["y"],
                "z": z_reference_height,
            },
            tolerance_mm=tolerance_mm,
            label="Lowest-instrument Z reference",
        )

        block_coordinates: dict[str, dict[str, float]] = {
            lowest_instrument: dict(z_origin_coords)
        }
        output(
            f"Recorded block WPos for lowest instrument {lowest_instrument}: "
            f"X={block_coordinates[lowest_instrument]['x']:.3f}, "
            f"Y={block_coordinates[lowest_instrument]['y']:.3f}, "
            f"Z={block_coordinates[lowest_instrument]['z']:.3f}"
        )
        _retract_up_after_contact(
            gantry,
            retract_z_mm=post_contact_retract_z_mm,
            feed_rate=jog_feed_rate,
            output=output,
        )
        output(
            "Now calibrate each remaining instrument against that same physical point. "
            "Do not move the block/reference point between instruments."
        )
        calibration_sequence = tuple(
            instrument
            for instrument in _unique_instrument_sequence(
                (reference_instrument, *instruments)
            )
            if instrument != lowest_instrument
            and instrument not in follow_camera_instruments
        )
        non_contact_calibrations: dict[str, dict[str, float]] = {}
        for instrument in calibration_sequence:
            if instrument in non_contact_instruments:
                non_contact_coords = _interactive_jog_to_reference(
                    gantry,
                    target_description=(
                        f"Step 3: calibrate non-contact instrument {instrument!r}. "
                        "Jog until the instrument reference point is centered "
                        "over the same calibration block mark used by the "
                        "contact instruments. It does not touch the block."
                    ),
                    confirmation_description=(
                        "Press ENTER when the instrument is centered over the block mark."
                    ),
                    key_reader=key_reader,
                    stdin_flusher=stdin_flusher,
                    output=output,
                    feed_rate=jog_feed_rate,
                    initial_step_mm=jog_step_mm,
                    limit_pull_off_mm=5.0,
                )
                output(
                    f"Non-contact pose recorded for {instrument}: "
                    f"X={non_contact_coords['x']:.3f}, "
                    f"Y={non_contact_coords['y']:.3f}, "
                    f"Z={non_contact_coords['z']:.3f}"
                )
                height_above_block = _prompt_non_contact_block_distance(
                    instrument_name=instrument,
                    input_reader=input_reader,
                    output=output,
                )
                reference_coords = block_coordinates.get(reference_instrument)
                if reference_coords is None:
                    raise RuntimeError(
                        f"Missing block coordinates for reference instrument {reference_instrument!r}."
                    )
                non_contact_calibrations[instrument] = (
                    compute_non_contact_block_calibration(
                        block_reference_coordinates=reference_coords,
                        non_contact_coordinates=non_contact_coords,
                        block_height_mm=z_reference_height,
                        height_above_block_mm=height_above_block,
                    )
                )
                output(
                    f"Recorded non-contact calibration for {instrument}: "
                    f"X={non_contact_coords['x']:.3f}, "
                    f"Y={non_contact_coords['y']:.3f}, "
                    f"Z={non_contact_coords['z']:.3f}, "
                    f"distance from block={height_above_block:.3f} mm"
                )
                continue
            block_coordinates[instrument] = _interactive_jog_to_reference(
                gantry,
                target_description=(
                    f"Step 3: calibrate {instrument!r}. Jog this tool's tip/probe point "
                    "to the same physical point used by the lowest instrument."
                ),
                confirmation_description=(
                    "Press ENTER when this instrument is touching the same block point "
                    "used for the other instruments. Do not move the block between "
                    "instruments."
                ),
                key_reader=key_reader,
                stdin_flusher=stdin_flusher,
                output=output,
                feed_rate=jog_feed_rate,
                initial_step_mm=jog_step_mm,
                limit_pull_off_mm=5.0,
            )
            output(
                f"Recorded block WPos for {instrument}: "
                f"X={block_coordinates[instrument]['x']:.3f}, "
                f"Y={block_coordinates[instrument]['y']:.3f}, "
                f"Z={block_coordinates[instrument]['z']:.3f}"
            )
            _retract_up_after_contact(
                gantry,
                retract_z_mm=post_contact_retract_z_mm,
                feed_rate=jog_feed_rate,
                output=output,
            )

        output("Re-homing after instrument calibration to measure final working-volume maxima...")
        _set_serial_timeout_if_available(gantry, homing_serial_timeout_s)
        _home_with_serial_reconnect(gantry, output=output)
        _set_serial_timeout_if_available(gantry, jog_serial_timeout_s)
        stdin_flusher()
        _wait_until_idle_if_available(gantry)
        measured_coords = dict(gantry.get_coordinates())
        homing_pull_off_mm = gantry.homing_pull_off_mm()
        z_min_mm = block_z_calibration.z_min_mm
        z_max_mm = _round_mm(float(measured_coords["z"]))
        z_span_mm = _round_mm(z_max_mm - z_min_mm)
        max_travel = _calculate_grbl_max_travel(
            measured_coords,
            z_min_mm=z_min_mm,
            tolerance_mm=tolerance_mm,
            z_span_mm=z_span_mm,
            homing_pull_off_mm=homing_pull_off_mm,
        )
        all_calibrations = compute_relative_instrument_calibrations(
            block_coordinates=block_coordinates,
            reference_instrument=reference_instrument,
            lowest_instrument=lowest_instrument,
        )
        all_calibrations.update(non_contact_calibrations)
        camera_source = next(
            (
                name
                for name in instruments
                if _instrument_type(raw_config, name) == "camera"
                and name in all_calibrations
            ),
            None,
        )
        for name in instruments:
            if name not in follow_camera_instruments:
                continue
            if camera_source is None:
                output(
                    f"Skipping {name}: no calibrated camera to copy from; "
                    "existing offsets are kept."
                )
                continue
            all_calibrations[name] = dict(all_calibrations[camera_source])
            output(f"{name} reuses the camera calibration from {camera_source}.")
        instrument_calibrations = {
            instrument: all_calibrations[instrument]
            for instrument in instruments
            if instrument in all_calibrations
        }
        for instrument, calibration in instrument_calibrations.items():
            output(
                f"Computed {instrument}: "
                f"offset_x={calibration['offset_x']:.3f}, "
                f"offset_y={calibration['offset_y']:.3f}, "
                f"depth={calibration['depth']:.3f}"
            )

        yaml_text = _updated_yaml_text(
            raw_config,
            measured_coords=measured_coords,
            instrument_calibrations=instrument_calibrations,
            max_travel=max_travel,
            z_min_mm=z_min_mm,
            z_max_mm=z_max_mm,
            calibration_block_height_mm=z_reference_height,
            homing_pull_off_mm=homing_pull_off_mm,
            origin_policy=origin_policy.value,
        )
        _print_yaml_block(
            title="Full calibrated multi-instrument gantry YAML to copy/paste:",
            yaml_text=yaml_text,
            output=output,
        )
        written_yaml_path = _maybe_write_gantry_yaml(
            yaml_text=yaml_text,
            output_path=output_gantry_path,
            write_requested=write_gantry_yaml,
            input_reader=input_reader,
            output=output,
            backup_existing_output=backup_existing_output,
        )
        if skip_soft_limit_config:
            output("Skipping GRBL soft-limit programming by request.")
        else:
            try:
                gantry.configure_soft_limits_from_spans(
                    max_travel_x=max_travel["max_travel_x"],
                    max_travel_y=max_travel["max_travel_y"],
                    max_travel_z=max_travel["max_travel_z"],
                    status_report=0,
                    homing_pull_off=homing_pull_off_mm,
                    hard_limits=_configured_grbl_setting(raw_config, "hard_limits"),
                    tolerance_mm=tolerance_mm,
                )
            except Exception:
                if written_yaml_path is not None:
                    output(
                        "Soft-limit programming failed after the calibrated YAML "
                        f"was written to: {written_yaml_path}"
                    )
                else:
                    output(
                        "Soft-limit programming failed after the calibrated YAML "
                        "was printed above; copy those values before retrying."
                    )
                raise
            restore_soft_limits_after_calibration = False

        return MultiInstrumentCalibrationResult(
            measured_working_volume=(
                float(measured_coords["x"]),
                float(measured_coords["y"]),
                z_max_mm,
            ),
            xy_bounds_after_origin=_coords_tuple(xy_bounds_coords),
            xy_origin_verification=_coords_tuple(xy_origin_coords),
            z_origin_verification=_coords_tuple(z_origin_coords),
            instrument_calibrations=instrument_calibrations,
            grbl_max_travel=(
                max_travel["max_travel_x"],
                max_travel["max_travel_y"],
                max_travel["max_travel_z"],
            ),
            reference_instrument=reference_instrument,
            lowest_instrument=lowest_instrument,
            block_reference_coordinates={
                name: _coords_tuple(coords)
                for name, coords in block_coordinates.items()
            },
        )
    finally:
        try:
            if restore_soft_limits_after_calibration:
                restore_soft_limits_after_calibration = False
                _restore_soft_limits_after_origin_jog(gantry, output=output)
        finally:
            try:
                if restore_hard_limits_after_calibration:
                    restore_hard_limits_after_calibration = False
                    _restore_hard_limits_after_origin_jog(gantry, output=output)
            finally:
                _cancel_jog_if_available(gantry, output=output)
                _set_serial_timeout_if_available(gantry, 0.05)
                output("Disconnecting...")
                gantry.disconnect()


def _prompt_instrument_name(
    label: str,
    available: Sequence[str],
    *,
    raw_config: dict[str, Any] | None = None,
    input_reader: Callable[[str], str],
    output: Callable[[str], None],
) -> str:
    instruments = raw_config.get("instruments", {}) if isinstance(raw_config, dict) else {}
    output("Available instruments:")
    for index, name in enumerate(available, start=1):
        instrument_config = instruments.get(name, {}) if isinstance(instruments, dict) else {}
        instrument_type = instrument_config.get("type") if isinstance(instrument_config, dict) else None
        suffix = f" ({instrument_type})" if instrument_type else ""
        output(f"  {index}. {name}{suffix}")
    while True:
        raw = input_reader(f"{label}: ").strip()
        if not raw:
            output(f"Pick which numbered tool to use: enter 1 to {len(available)}.")
            continue
        try:
            selected_index = int(raw)
        except ValueError:
            output(f"Enter a number from 1 to {len(available)}.")
            continue
        if not 1 <= selected_index <= len(available):
            output(f"Enter a number from 1 to {len(available)}.")
            continue
        selected = available[selected_index - 1]
        confirm = input_reader(f"You selected #{selected_index} {selected}. Continue? [y/N]: ").strip().lower()
        if confirm in {"y", "yes"}:
            return selected
        output("Selection cancelled; pick the numbered tool again.")
