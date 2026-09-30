"""Single-instrument gantry calibration flow.

Internal implementation used by the sole user-facing entrypoint:
``packages/core/src/cubos/tools/calibrate_gantry.py``.
"""

from __future__ import annotations

import copy
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))

from cubos.gantry import Gantry, load_gantry_from_yaml  # noqa: E402
from cubos.gantry.errors import (  # noqa: E402
    CommandExecutionError,
    MillConnectionError,
    StatusReturnError,
)
from cubos.gantry.limit_recovery import (  # noqa: E402
    looks_like_limit_alarm as _looks_like_limit_alarm,
    probe_for_limit_status_after_jog as _probe_for_limit_status_after_jog,
    recover_from_limit_alarm as _recover_from_limit_alarm,
)
from cubos.gantry.gantry_config import OriginPolicy  # noqa: E402
from cubos.gantry.origin import (  # noqa: E402
    DeckOriginCalibrationPlan,
    _CALIBRATION_COMMAND_SKELETON,
    build_deck_origin_calibration_plan,
    validate_working_volume_origin,
)
from cubos.tools.keyboard_input import flush_stdin, read_keypress_batch  # noqa: E402


@dataclass(frozen=True)
class DeckOriginCalibrationResult:
    """Result of one-instrument deck-origin calibration."""

    measured_working_volume: tuple[float, float, float]
    xy_origin_verification: tuple[float, float, float]
    z_reference_verification: tuple[float, float, float]
    z_min_mm: float
    factory_z_travel_mm: float
    z_reference_mode: str
    reachable_z_min_mm: float | None
    block_height_mm: float | None
    block_touch_wpos_z_mm: float | None
    home_to_block_travel_mm: float | None
    can_reach_deck_bottom: bool | None
    grbl_max_travel: tuple[float, float, float] | None
    instrument_name: str | None
    plan: DeckOriginCalibrationPlan


@dataclass(frozen=True)
class BlockZCalibration:
    """Deck-frame Z bounds inferred from home-to-block travel."""

    block_height_mm: float
    factory_z_travel_mm: float
    initial_home_z_mm: float
    block_touch_wpos_z_mm: float
    home_to_block_travel_mm: float
    remaining_below_block_mm: float
    can_reach_deck_bottom: bool
    z_min_mm: float
    expected_home_z_mm: float


KeyReader = Callable[[], tuple[str, int]]


CONTROLS_LEGEND = """
Jog controls after homing:
  RIGHT / LEFT       +X right / -X left
  UP / DOWN          +Y back-away / -Y front-toward-operator
  X / Z              +Z up / -Z down
  1 / 2 / 3 / 4 / 5 / 6 / 7
                      Set jog step to 0.1 / 1 / 5 / 10 / 25 / 50 / 100 mm
  SPACE              Cancel any active jog
  ENTER              Confirm the current calibration step
  Q                  Abort calibration
"""


def _load_raw_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError(f"Gantry config is empty or invalid: {path}")
    return config


def _coords_tuple(coords: dict[str, float]) -> tuple[float, float, float]:
    return (float(coords["x"]), float(coords["y"]), float(coords["z"]))


def _round_mm(value: float) -> float:
    return round(float(value), 3)


def _factory_z_travel_mm(raw_config: dict[str, Any]) -> float:
    cnc = raw_config.get("cnc")
    if not isinstance(cnc, dict) or "factory_z_travel_mm" not in cnc:
        raise ValueError(
            "Gantry YAML must seed cnc.factory_z_travel_mm before calibration; "
            "calibration uses it only to decide whether deck bottom is reachable."
        )
    try:
        value = float(cnc["factory_z_travel_mm"])
    except (TypeError, ValueError) as exc:
        raise ValueError("cnc.factory_z_travel_mm must be numeric.") from exc
    if value <= 0:
        raise ValueError("cnc.factory_z_travel_mm must be > 0.")
    return _round_mm(value)


def _calibration_block_height_mm(
    raw_config: dict[str, Any],
    *,
    explicit_block_height_mm: float | None,
    max_height_mm: float | None = None,
    input_reader: Callable[[str], str] | None = None,
    output: Callable[[str], None] | None = None,
) -> float:
    if explicit_block_height_mm is not None:
        value = float(explicit_block_height_mm)
    else:
        if input_reader is not None and output is not None:
            return _round_mm(
                _prompt_block_height(
                    input_reader=input_reader,
                    output=output,
                    max_height_mm=max_height_mm,
                )
            )
        cnc = raw_config.get("cnc")
        if not isinstance(cnc, dict) or "calibration_block_height_mm" not in cnc:
            raise ValueError(
                "Gantry YAML must define cnc.calibration_block_height_mm "
                "for block Z calibration."
            )
        try:
            value = float(cnc["calibration_block_height_mm"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "cnc.calibration_block_height_mm must be numeric."
            ) from exc
    if value <= 0:
        raise ValueError("cnc.calibration_block_height_mm must be > 0.")
    if max_height_mm is not None and value > max_height_mm:
        raise ValueError(
            "cnc.calibration_block_height_mm must be <= "
            f"cnc.factory_z_travel_mm ({max_height_mm:g} mm)."
        )
    return _round_mm(value)


def _calculate_block_z_calibration(
    *,
    initial_home_z_mm: float,
    block_touch_wpos_z_mm: float,
    block_height_mm: float,
    factory_z_travel_mm: float,
    tolerance_mm: float,
) -> BlockZCalibration:
    for label, value in (
        ("initial home Z", initial_home_z_mm),
        ("block touch WPos Z", block_touch_wpos_z_mm),
        ("block height", block_height_mm),
        ("factory Z travel", factory_z_travel_mm),
    ):
        if not isinstance(value, (int, float)):
            raise RuntimeError(f"{label} must be numeric.")
    if block_height_mm <= 0:
        raise ValueError("block height must be > 0 in block mode.")
    if factory_z_travel_mm <= 0:
        raise ValueError("factory_z_travel_mm must be > 0.")

    travel_to_block = _round_mm(initial_home_z_mm - block_touch_wpos_z_mm)
    if travel_to_block <= tolerance_mm:
        raise RuntimeError(
            "Block touch must be below the initial homed Z position: "
            f"home={initial_home_z_mm:.4f}, touch={block_touch_wpos_z_mm:.4f}."
        )
    if travel_to_block > factory_z_travel_mm + tolerance_mm:
        raise RuntimeError(
            "Home-to-block travel exceeds the configured factory Z travel: "
            f"travel={travel_to_block:.4f}, "
            f"factory_z_travel_mm={factory_z_travel_mm:.4f}."
        )

    remaining_below_block = _round_mm(factory_z_travel_mm - travel_to_block)
    can_reach_deck_bottom = remaining_below_block + tolerance_mm >= block_height_mm
    z_min_mm = (
        0.0
        if can_reach_deck_bottom
        else _round_mm(block_height_mm - remaining_below_block)
    )
    expected_home_z_mm = _round_mm(block_height_mm + travel_to_block)
    return BlockZCalibration(
        block_height_mm=_round_mm(block_height_mm),
        factory_z_travel_mm=_round_mm(factory_z_travel_mm),
        initial_home_z_mm=_round_mm(initial_home_z_mm),
        block_touch_wpos_z_mm=_round_mm(block_touch_wpos_z_mm),
        home_to_block_travel_mm=travel_to_block,
        remaining_below_block_mm=remaining_below_block,
        can_reach_deck_bottom=can_reach_deck_bottom,
        z_min_mm=z_min_mm,
        expected_home_z_mm=expected_home_z_mm,
    )


def _calculate_grbl_max_travel(
    measured_coords: dict[str, float],
    *,
    z_min_mm: float,
    tolerance_mm: float,
    z_span_mm: float | None = None,
    homing_pull_off_mm: float = 0.0,
) -> dict[str, float]:
    pull_off = _round_mm(float(homing_pull_off_mm))
    if pull_off < 0:
        raise RuntimeError("GRBL homing pull-off must be non-negative.")
    x_span = _round_mm(float(measured_coords["x"]) + pull_off)
    y_span = _round_mm(float(measured_coords["y"]) + pull_off)
    z_span = (
        _round_mm(z_span_mm)
        if z_span_mm is not None
        else _round_mm(float(measured_coords["z"]) - float(z_min_mm))
    )
    z_span = _round_mm(z_span + pull_off)
    spans = {
        "max_travel_x": x_span,
        "max_travel_y": y_span,
        "max_travel_z": z_span,
    }
    invalid = [f"{key}={value}" for key, value in spans.items() if value <= tolerance_mm]
    if invalid:
        raise RuntimeError(
            "Measured travel span is not positive enough for GRBL soft limits: "
            + ", ".join(invalid)
        )
    return spans


def _assert_near_xyz(
    coords: dict[str, float],
    *,
    expected: dict[str, float],
    tolerance_mm: float,
    label: str,
) -> None:
    misses = [
        f"{axis}: got {float(coords[axis]):.4f}, expected {float(expected[axis]):.4f}"
        for axis in ("x", "y", "z")
        if abs(float(coords[axis]) - float(expected[axis])) > tolerance_mm
    ]
    if misses:
        raise RuntimeError(
            f"{label} did not verify within {tolerance_mm} mm: "
            + "; ".join(misses)
        )


def _updated_gantry_yaml_text(
    raw_config: dict[str, Any],
    *,
    measured_coords: dict[str, float],
    z_min_mm: float,
    z_max_mm: float,
    max_travel: dict[str, float] | None = None,
    homing_pull_off_mm: float | None = None,
    calibration_block_height_mm: float | None = None,
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
    if max_travel is not None:
        updated["grbl_settings"] = _build_gantry_grbl_settings(
            gantry_raw=raw_config,
            max_travel=max_travel,
            homing_pull_off_mm=homing_pull_off_mm,
        )
    if calibration_block_height_mm is not None:
        cnc = updated.setdefault("cnc", {})
        if not isinstance(cnc, dict):
            raise ValueError("Input gantry YAML cnc section must be a mapping.")
        cnc["calibration_block_height_mm"] = _round_mm(calibration_block_height_mm)
    return yaml.safe_dump(updated, sort_keys=False)


def _build_gantry_grbl_settings(
    *,
    gantry_raw: dict[str, Any],
    max_travel: dict[str, float],
    homing_pull_off_mm: float | None = None,
) -> dict[str, Any]:
    settings = dict(gantry_raw.get("grbl_settings") or {})
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


def _configured_homing_pull_off(raw_config: dict[str, Any]) -> float | None:
    settings = raw_config.get("grbl_settings") or {}
    if not isinstance(settings, dict):
        return None
    value = settings.get("homing_pull_off")
    if value is None:
        return None
    try:
        pull_off = _round_mm(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"grbl_settings.homing_pull_off must be numeric; got {value!r}"
        ) from exc
    if pull_off < 0:
        raise ValueError("grbl_settings.homing_pull_off must be non-negative.")
    return pull_off


def _configured_grbl_setting(raw_config: dict[str, Any], field_name: str) -> Any:
    settings = raw_config.get("grbl_settings") or {}
    if not isinstance(settings, dict):
        return None
    return settings.get(field_name)


def _apply_calibration_grbl_baseline(
    gantry: Gantry,
    raw_config: dict[str, Any],
    *,
    output: Callable[[str], None],
) -> None:
    output("Setting GRBL WPos reporting ($10=0) before calibration homing...")
    try:
        gantry.set_grbl_setting("$10", 0)
    except (CommandExecutionError, StatusReturnError) as exc:
        raise RuntimeError(
            "Failed to set GRBL WPos reporting mode ($10=0) before calibration. "
            f"Calibration cannot proceed safely without WPos coordinates: {exc}"
        ) from exc
    homing_pull_off = _configured_homing_pull_off(raw_config)
    if homing_pull_off is not None:
        output(
            f"Setting GRBL homing pull-off ($27={homing_pull_off:g}) "
            "before calibration homing..."
        )
        gantry.set_grbl_setting("$27", homing_pull_off)


def _print_yaml_block(
    *,
    title: str,
    yaml_text: str,
    output: Callable[[str], None],
) -> None:
    output("")
    output(title)
    output("```yaml")
    for line in yaml_text.rstrip().splitlines():
        output(line)
    output("```")


def _maybe_write_gantry_yaml(
    *,
    yaml_text: str,
    output_path: Path | None,
    write_requested: bool,
    input_reader: Callable[[str], str],
    output: Callable[[str], None],
    backup_existing_output: bool = False,
) -> Path | None:
    if output_path is None and not write_requested:
        return None
    explicit_output_path = output_path is not None
    if output_path is None:
        raw = input_reader("Output gantry YAML filename: ").strip()
        if not raw:
            output("No gantry YAML filename supplied; skipping write.")
            return None
        output_path = Path(raw)
    if not explicit_output_path:
        confirm = input_reader(
            f"Write updated gantry YAML to {output_path}? [y/N]: "
        ).strip().lower()
        if confirm not in ("y", "yes"):
            output("Skipping gantry YAML write.")
            return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if backup_existing_output and output_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = output_path.with_name(f"{output_path.name}.{timestamp}.bak")
        shutil.copy2(output_path, backup_path)
        output(f"Backed up existing gantry YAML to: {backup_path}")
    output_path.write_text(yaml_text, encoding="utf-8")
    output(f"Wrote updated gantry YAML: {output_path}")
    return output_path


def _assert_near_xy_origin(
    coords: dict[str, float],
    *,
    tolerance_mm: float,
) -> None:
    expected = {
        "x": 0.0,
        "y": 0.0,
    }
    misses = [
        f"{axis}: got {float(coords[axis]):.4f}, expected {expected[axis]:.4f}"
        for axis in ("x", "y")
        if abs(float(coords[axis]) - expected[axis]) > tolerance_mm
    ]
    if misses:
        raise RuntimeError(
            "Deck-origin XY reference did not verify within "
            f"{tolerance_mm} mm: " + "; ".join(misses)
        )


def _assert_near_z_reference(
    coords: dict[str, float],
    *,
    z_min_mm: float,
    tolerance_mm: float,
) -> None:
    expected = {"z": z_min_mm}
    misses = [
        f"{axis}: got {float(coords[axis]):.4f}, expected {expected[axis]:.4f}"
        for axis in ("z",)
        if abs(float(coords[axis]) - expected[axis]) > tolerance_mm
    ]
    if misses:
        raise RuntimeError(
            "Deck-origin Z reference did not verify within "
            f"{tolerance_mm} mm: " + "; ".join(misses)
        )


def _assert_positive_measured_volume(
    coords: dict[str, float],
    *,
    tolerance_mm: float,
) -> None:
    misses = [
        f"{axis}: got {float(coords[axis]):.4f}"
        for axis in ("x", "y", "z")
        if float(coords[axis]) <= tolerance_mm
    ]
    if misses:
        raise RuntimeError(
            "Measured homed WPos did not look like positive working-volume "
            "maxima: " + "; ".join(misses)
        )


def _print_config_patch(
    coords: dict[str, float],
    *,
    z_reference_coords: dict[str, float],
    z_min_mm: float,
    z_max_mm: float,
    factory_z_travel_mm: float,
    z_reference_mode: str,
    instrument_name: str | None,
    block_height_mm: float | None,
    block_touch_wpos_z_mm: float | None,
    reachable_z_min_mm: float | None,
    home_to_block_travel_mm: float | None,
    remaining_below_block_mm: float | None,
    can_reach_deck_bottom: bool | None,
    expected_home_z_mm: float | None,
    output: Callable[[str], None],
    origin_policy: str = "deck_origin",
) -> None:
    x_max, y_max, measured_home_z = _coords_tuple(coords)
    output("")
    if origin_policy == OriginPolicy.HOME_ORIGIN.value:
        output("Calibrated working volume from calibrated origin (home-origin frame):")
        output(f"  X: {-x_max:.3f} to 0.000 mm")
        output(f"  Y: {-y_max:.3f} to 0.000 mm")
        output(f"  Z: {z_min_mm - z_max_mm:.3f} to 0.000 mm")
    else:
        output("Calibrated working volume from calibrated origin:")
        output(f"  X: 0.000 to {x_max:.3f} mm")
        output(f"  Y: 0.000 to {y_max:.3f} mm")
        output(f"  Z: {z_min_mm:.3f} to {z_max_mm:.3f} mm")
    output(f"  Factory Z travel safety bound: {factory_z_travel_mm:.3f} mm")
    output(f"  Homed Z readback after calibration: {measured_home_z:.3f} mm")
    output("")
    output("Update the gantry YAML working_volume to:")
    output("  working_volume:")
    if origin_policy == OriginPolicy.HOME_ORIGIN.value:
        output(f"    x_min: {-x_max:.3f}")
        output("    x_max: 0.0")
        output(f"    y_min: {-y_max:.3f}")
        output("    y_max: 0.0")
        output(f"    z_min: {z_min_mm - z_max_mm:.3f}")
        output("    z_max: 0.0")
    else:
        output("    x_min: 0.0")
        output(f"    x_max: {x_max:.3f}")
        output("    y_min: 0.0")
        output(f"    y_max: {y_max:.3f}")
        output(f"    z_min: {z_min_mm:.3f}")
        output(f"    z_max: {z_max_mm:.3f}")
    output("")
    output("Keep the out-of-box cnc.factory_z_travel_mm unchanged:")
    output(f"  factory_z_travel_mm: {factory_z_travel_mm:.3f}")
    output("")
    output("Z reference point after XY origining:")
    output(
        "  WPos "
        f"X={z_reference_coords['x']:.3f} "
        f"Y={z_reference_coords['y']:.3f} "
        f"Z={z_reference_coords['z']:.3f}"
    )
    output(f"  mode: {z_reference_mode}")
    if block_height_mm is not None and block_touch_wpos_z_mm is not None:
        output("")
        output("Calibration block home-to-block calculation:")
        output(f"  block_height: {block_height_mm:.3f} mm")
        output(f"  block_touch_wpos_z: {block_touch_wpos_z_mm:.3f} mm")
        if home_to_block_travel_mm is not None:
            output(f"  home_to_block_travel: {home_to_block_travel_mm:.3f} mm")
        if remaining_below_block_mm is not None:
            output(
                "  remaining_factory_travel_below_block: "
                f"{remaining_below_block_mm:.3f} mm"
            )
        if can_reach_deck_bottom is not None:
            output(f"  can_reach_deck_bottom: {str(can_reach_deck_bottom).lower()}")
        if expected_home_z_mm is not None:
            output(f"  expected_home_z_from_block: {expected_home_z_mm:.3f} mm")
        if reachable_z_min_mm is not None:
            output(
                "  lowest_reachable_height_above_deck: "
                f"{reachable_z_min_mm:.3f} mm"
            )
    if reachable_z_min_mm is not None and reachable_z_min_mm > 0:
        reach_name = instrument_name or "reference_tcp"
        output("")
        output(f"  {reach_name}_reachable_z_min: {reachable_z_min_mm:.3f} mm")


def _print_dry_run(
    gantry_path: Path,
    plan: DeckOriginCalibrationPlan,
    *,
    tip_gap_mm: float | None,
    z_reference_mode: str,
    instrument_name: str | None,
    output: Callable[[str], None],
) -> None:
    output(f"Loaded deck-origin gantry config: {gantry_path}")
    if instrument_name:
        output(f"Instrument/TCP: {instrument_name}")
    output(f"Z reference mode: {z_reference_mode}")
    output("Dry run only. Physical calibration flow:")
    commands = _commands_for_z_min(
        plan,
        tip_gap_mm,
        z_reference_mode=z_reference_mode,
    )
    for command in commands:
        output(f"  {command}")


def _commands_for_z_min(
    plan: DeckOriginCalibrationPlan,
    tip_gap_mm: float | None,
    *,
    z_reference_mode: str = "ruler-gap",
) -> tuple[str, ...]:
    z_value = "0"
    confirmation = "<confirm true deck-bottom contact>"
    if z_reference_mode in ("prompt", "ruler-gap"):
        gap_value = "<tip_gap_mm>" if tip_gap_mm is None else f"{tip_gap_mm:g}"
        confirmation = (
            "<confirm bottom contact or enter ruler-measured TCP gap; "
            f"reach metadata={gap_value}>"
        )
    return tuple(
        command.replace("<z_min_mm>", z_value).replace(
            "<confirm deck-bottom contact or enter ruler-measured TCP gap>",
            confirmation,
        )
        for command in plan.commands
    )


def _prompt_tip_gap_mm(
    *,
    input_reader: Callable[[str], str],
    output: Callable[[str], None],
) -> float:
    output("")
    output(
        "This TCP is not touching true deck bottom at its lower reach point."
    )
    output(
        "Measure the vertical gap from the deck surface to the TCP with a "
        "ruler, then enter that gap in millimeters."
    )
    while True:
        raw = input_reader("Deck-to-TCP gap in mm: ").strip()
        try:
            value = float(raw)
        except ValueError:
            output("Enter a numeric gap in millimeters.")
            continue
        if value <= 0:
            output("Deck-to-TCP gap must be > 0 mm. Use bottom mode for Z=0.")
            continue
        return value


def _prompt_block_height(
    *,
    input_reader: Callable[[str], str],
    output: Callable[[str], None],
    max_height_mm: float | None = None,
) -> float:
    output("")
    output("Z reference: touch the top of a surface with a known height above the deck —")
    output("the calibration block, or a deck feature such as the top of a well plate.")
    while True:
        raw = input_reader("Reference height above the deck in mm: ").strip()
        try:
            value = float(raw)
        except ValueError:
            output("Enter a numeric reference height in millimeters.")
            continue
        if value <= 0:
            output("Reference height must be > 0 mm.")
            continue
        if max_height_mm is not None and value > max_height_mm:
            output(
                "Reference height must be <= configured factory Z travel "
                f"({max_height_mm:g} mm)."
            )
            continue
        return value


def _prompt_z_reference_mode(
    *,
    input_reader: Callable[[str], str],
    output: Callable[[str], None],
) -> str:
    output("")
    output("Z grounding mode:")
    output("  y = this TCP is touching true deck bottom, so set Z=0 here")
    output("  n = no/unsure; measure the deck-to-TCP gap with a ruler")
    while True:
        raw = input_reader(
            "Is the TCP touching true deck bottom at the current pose? [y/N]: "
        ).strip().lower()
        if raw in ("", "n", "no", "u", "unsure"):
            return "ruler-gap"
        if raw in ("y", "yes"):
            return "bottom"
        output("Enter y for true-bottom contact, or n/Enter for ruler-gap mode.")


def _set_serial_timeout_if_available(
    gantry: Gantry,
    timeout_s: float,
) -> None:
    setter = getattr(gantry, "set_serial_timeout", None)
    if callable(setter):
        setter(timeout_s)


def _wait_until_idle_if_available(
    gantry: Gantry,
    *,
    timeout_s: float = 10.0,
    poll_interval_s: float = 0.1,
) -> None:
    status_reader = getattr(gantry, "get_status", None)
    if not callable(status_reader):
        return

    deadline = time.monotonic() + timeout_s
    last_status = ""
    while time.monotonic() < deadline:
        last_status = str(status_reader())
        if "idle" in last_status.lower():
            return
        time.sleep(poll_interval_s)

    raise RuntimeError(
        "Timed out waiting for gantry to become idle before coordinate read; "
        f"last status: {last_status}"
    )


def _cancel_jog_if_available(
    gantry: Gantry,
    *,
    output: Callable[[str], None],
) -> None:
    cancel = getattr(gantry, "jog_cancel", None)
    if not callable(cancel):
        return
    try:
        cancel()
    except Exception as exc:
        output(f"Warning: failed to cancel active jog before shutdown: {exc}")


def _looks_like_soft_limit_jog_rejection(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "error:15",
            "travel exceeded",
            "jog target exceeds machine travel",
        )
    )


def _read_soft_limits_enabled_if_available(
    gantry: Gantry,
    *,
    output: Callable[[str], None],
) -> bool | None:
    reader = getattr(gantry, "soft_limits_enabled", None)
    if not callable(reader):
        return None
    try:
        return reader()
    except MillConnectionError:
        raise
    except (CommandExecutionError, StatusReturnError, ValueError) as exc:
        output(f"Could not read GRBL soft-limit state before jogging: {exc}")
        output("Continuing; any GRBL error:15 jog rejection will be handled in-place.")
        return None


def _set_soft_limits_enabled_if_available(
    gantry: Gantry,
    enabled: bool,
) -> bool:
    setter = getattr(gantry, "set_soft_limits_enabled", None)
    if not callable(setter):
        return False
    setter(enabled)
    return True


def _temporarily_disable_soft_limits_for_origin_jog(
    gantry: Gantry,
    *,
    output: Callable[[str], None],
) -> bool:
    enabled = _read_soft_limits_enabled_if_available(gantry, output=output)
    if enabled is not True:
        return False
    output("Temporarily disabling GRBL soft limits ($20=0) for this jog.")
    output("Jog cautiously.")
    if not _set_soft_limits_enabled_if_available(gantry, False):
        output("No GRBL setting writer is available; leaving soft limits unchanged.")
        return False
    return True


def _restore_soft_limits_after_origin_jog(
    gantry: Gantry,
    *,
    output: Callable[[str], None],
) -> None:
    output("Restoring GRBL soft limits ($20=1) after interactive origin jog.")
    if not _set_soft_limits_enabled_if_available(gantry, True):
        raise MillConnectionError(
            "Cannot restore GRBL soft limits because this gantry object has no "
            "setting writer."
        )


def _read_hard_limits_enabled_if_available(
    gantry: Gantry,
    *,
    output: Callable[[str], None],
) -> bool | None:
    reader = getattr(gantry, "hard_limits_enabled", None)
    if not callable(reader):
        return None
    try:
        return reader()
    except MillConnectionError:
        raise
    except (CommandExecutionError, StatusReturnError, ValueError) as exc:
        output(f"Could not read GRBL hard-limit state before jogging: {exc}")
        output("Continuing without hard-limit enforcement for this jog.")
        return None


def _set_hard_limits_enabled_if_available(
    gantry: Gantry,
    enabled: bool,
) -> bool:
    setter = getattr(gantry, "set_hard_limits_enabled", None)
    if not callable(setter):
        return False
    setter(enabled)
    return True


def _temporarily_enable_hard_limits_for_origin_jog(
    gantry: Gantry,
    *,
    output: Callable[[str], None],
) -> bool:
    """Enable $21 for the origin jog when the controller has it off.

    With soft limits disabled above, hard limits are the only motion
    backstop — without them a jog past a travel end grinds and silently
    skips steps, corrupting the calibration being taken. Returns True when
    $21 was flipped on (the caller must restore it afterwards).
    """
    enabled = _read_hard_limits_enabled_if_available(gantry, output=output)
    if enabled is not False:
        return False
    output("Enabling GRBL hard limits ($21=1) for this jog.")
    if not _set_hard_limits_enabled_if_available(gantry, True):
        output("No GRBL setting writer is available; leaving hard limits unchanged.")
        return False
    return True


def _restore_hard_limits_after_origin_jog(
    gantry: Gantry,
    *,
    output: Callable[[str], None],
) -> None:
    output("Restoring pre-calibration GRBL hard limits ($21=0) after origin jog.")
    if not _set_hard_limits_enabled_if_available(gantry, False):
        output(
            "Could not restore $21=0 (no setting writer); hard limits stay "
            "enabled — the safer state."
        )


def _interactive_jog_to_reference(
    gantry: Gantry,
    *,
    target_description: str,
    confirmation_description: str,
    key_reader: KeyReader,
    stdin_flusher: Callable[[], None],
    output: Callable[[str], None],
    feed_rate: float,
    initial_step_mm: float,
    limit_pull_off_mm: float,
) -> dict[str, float]:
    step_mm = initial_step_mm
    stdin_flusher()
    output(CONTROLS_LEGEND)
    output(target_description)
    output(confirmation_description)

    while True:
        try:
            key, count = key_reader()
        except KeyboardInterrupt:
            _cancel_jog_if_available(gantry, output=output)
            raise
        key = key.upper()
        count = max(1, int(count))
        distance = step_mm * count

        if key == "Q":
            _cancel_jog_if_available(gantry, output=output)
            raise KeyboardInterrupt
        if key in ("\r", "\n", "ENTER"):
            _wait_until_idle_if_available(gantry)
            coords = gantry.get_coordinates()
            output(
                "Confirming current reported WPos "
                f"X={coords['x']:.3f} Y={coords['y']:.3f} Z={coords['z']:.3f}"
            )
            return coords
        if key in (" ", "\x1b", "ESC"):
            gantry.jog_cancel()
            output("Jog canceled.")
            continue
        if key == "1":
            step_mm = 0.1
            output("Jog step set to 0.1 mm.")
            continue
        if key == "2":
            step_mm = 1.0
            output("Jog step set to 1.0 mm.")
            continue
        if key == "3":
            step_mm = 5.0
            output("Jog step set to 5.0 mm.")
            continue
        if key == "4":
            step_mm = 10.0
            output("Jog step set to 10.0 mm.")
            continue
        if key == "5":
            step_mm = 25.0
            output("Jog step set to 25.0 mm.")
            continue
        if key == "6":
            step_mm = 50.0
            output("Jog step set to 50.0 mm.")
            continue
        if key == "7":
            step_mm = 100.0
            output("Jog step set to 100.0 mm.")
            continue

        delta = {"x": 0.0, "y": 0.0, "z": 0.0}
        if key == "LEFT":
            delta["x"] = -distance
        elif key == "RIGHT":
            delta["x"] = distance
        elif key == "DOWN":
            delta["y"] = -distance
        elif key == "UP":
            delta["y"] = distance
        elif key == "Z":
            delta["z"] = -distance
        elif key == "X":
            delta["z"] = distance
        else:
            output("Unrecognized key. " + "Use the listed jog controls.")
            output(CONTROLS_LEGEND)
            continue

        coords = None
        try:
            gantry.jog(feed_rate=feed_rate, **delta)
            _wait_until_idle_if_available(gantry)
            coords = gantry.get_coordinates()
        except MillConnectionError:
            raise
        except (CommandExecutionError, StatusReturnError) as exc:
            if _looks_like_soft_limit_jog_rejection(exc):
                output(
                    "GRBL rejected that jog because the target exceeds the "
                    "current soft-limit travel. The jog was ignored; reduce "
                    "the step, choose another direction, or press ENTER if "
                    "this is the intended safe origin point."
                )
                try:
                    _wait_until_idle_if_available(gantry)
                    coords = gantry.get_coordinates()
                except MillConnectionError:
                    raise
                except (CommandExecutionError, StatusReturnError) as read_exc:
                    output(f"WPos readback after rejected jog failed: {read_exc}")
                    output("Aborting calibration; gantry position is unknown.")
                    raise
            elif not _looks_like_limit_alarm(exc):
                output(f"Jog command rejected by controller: {exc}")
                output("Aborting calibration; gantry position is unknown.")
                raise
            else:
                _recover_from_limit_alarm(
                    gantry,
                    delta,
                    pull_off_mm=limit_pull_off_mm,
                    feed_rate=feed_rate,
                    output=output,
                )
                coords = None

        # Probe runs only when the jog succeeded and coords were obtained.
        # Separated so a probe-detected alarm produces a distinct message — the alarm
        # may predate the jog, so calling it "detected while jogging" would mislead.
        if coords is not None:
            try:
                _probe_for_limit_status_after_jog(gantry)
            except MillConnectionError:
                raise
            except (CommandExecutionError, StatusReturnError) as probe_exc:
                if _looks_like_limit_alarm(probe_exc):
                    output(
                        "Post-jog status probe detected a limit/alarm state "
                        "(the alarm may predate this jog). "
                        "Initiating pull-off opposite the last jog direction."
                    )
                    _recover_from_limit_alarm(
                        gantry,
                        delta,
                        pull_off_mm=limit_pull_off_mm,
                        feed_rate=feed_rate,
                        output=output,
                    )
                    coords = None
                else:
                    output(f"Post-jog status probe failed: {probe_exc}")
                    output("Aborting calibration; gantry position is unknown.")
                    raise

        if coords is None:
            continue
        output(
            "WPos "
            f"X={coords['x']:.3f} Y={coords['y']:.3f} Z={coords['z']:.3f} "
            f"(step {step_mm:g} mm)"
        )


def _interactive_jog_to_xy_origin(
    gantry: Gantry,
    *,
    key_reader: KeyReader,
    stdin_flusher: Callable[[], None],
    output: Callable[[str], None],
    feed_rate: float,
    initial_step_mm: float,
    limit_pull_off_mm: float,
    z_reference_mode: str,
    origin_policy: str = "deck_origin",
) -> dict[str, float]:
    if z_reference_mode == "block":
        confirmation_description = (
            "Press ENTER only when the current X/Y should become WPos X=0, "
            "Y=0 and the TCP is touching the top of the known-height reference. "
            "After confirmation, the script will set Z from that reference height."
        )
    else:
        confirmation_description = (
            "Press ENTER only when the current X/Y should become WPos X=0, "
            "Y=0. After confirmation, the script will set Z from either true "
            "deck-bottom contact or a ruler-measured deck-to-TCP gap."
        )
    if origin_policy == OriginPolicy.HOME_ORIGIN.value:
        target_description = (
            "Step 1/1: jog the one reference TCP as far as appropriate toward "
            "the physical back-right-top XY origin and its lowest safe reachable Z."
        )
    else:
        target_description = (
            "Step 1/1: jog the one reference TCP as far as appropriate toward "
            "the physical front-left XY origin and its lowest safe reachable Z."
        )
    return _interactive_jog_to_reference(
        gantry,
        target_description=target_description,
        confirmation_description=confirmation_description,
        key_reader=key_reader,
        stdin_flusher=stdin_flusher,
        output=output,
        feed_rate=feed_rate,
        initial_step_mm=initial_step_mm,
        limit_pull_off_mm=limit_pull_off_mm,
    )


def run_calibration(
    gantry_path: Path,
    *,
    dry_run: bool = False,
    tolerance_mm: float = 0.25,
    jog_step_mm: float = 1.0,
    jog_feed_rate: float = 2500.0,
    limit_pull_off_mm: float = 5.0,
    tip_gap_mm: float | None = None,
    z_reference_mode: str = "bottom",
    instrument_name: str | None = None,
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
) -> DeckOriginCalibrationResult | DeckOriginCalibrationPlan:
    """Calibrate one reference TCP to the CubOS physical origin corner.

    Supports both ``origin_policy`` values on the loaded gantry config:
    ``deck_origin`` (default, front-left-bottom) and ``home_origin``
    (back-right-top, the homed corner).
    """
    gantry_path = gantry_path.resolve()
    gantry_config = load_gantry_from_yaml(gantry_path)
    validate_working_volume_origin(gantry_config)
    origin_policy = OriginPolicy(gantry_config.origin_policy)
    raw_config = _load_raw_config(gantry_path)
    factory_z_travel_mm = _factory_z_travel_mm(raw_config)
    if output_gantry_path is not None:
        output_gantry_path = output_gantry_path.resolve()
    if origin_policy is OriginPolicy.HOME_ORIGIN:
        # validate_working_volume_origin() above already gated the shape;
        # build_deck_origin_calibration_plan() is deck-origin-only (it
        # re-validates deck minima internally), so build the equivalent
        # plan directly for home_origin instead of calling it.
        plan = DeckOriginCalibrationPlan(
            origin_wpos=(0.0, 0.0, 0.0),
            commands=_CALIBRATION_COMMAND_SKELETON,
        )
    else:
        plan = build_deck_origin_calibration_plan(gantry_config)
    if z_reference_mode not in ("prompt", "bottom", "ruler-gap", "block"):
        raise ValueError("z_reference_mode must be one of: prompt, bottom, ruler-gap, block")

    if dry_run:
        _print_dry_run(
            gantry_path,
            plan,
            tip_gap_mm=tip_gap_mm,
            z_reference_mode=z_reference_mode,
            instrument_name=instrument_name,
            output=output,
        )
        return plan

    output(f"Loaded {origin_policy.value} gantry config: {gantry_path}")
    output("Preflight:")
    output("  - Attach exactly one reference instrument/TCP for this calibration.")
    if origin_policy is OriginPolicy.HOME_ORIGIN:
        output("  - Place a calibration reference at the back-right-top origin point:")
    else:
        output("  - Place a calibration reference at the front-left origin point:")
    output("    the calibration block, or a deck feature such as a plate's corner-most well.")
    output("  - Jog the instrument tip/probe to touch the top of the reference at that point.")
    output("  - This will set X=0 and Y=0 at that pose, then set Z from the selected reference mode.")
    if instrument_name:
        output(f"  - Instrument/TCP label for reach output: {instrument_name}")
    output("")

    gantry_runtime_config = copy.deepcopy(raw_config)
    gantry_runtime_config.pop("grbl_settings", None)
    gantry = gantry_factory(config=gantry_runtime_config)
    restore_soft_limits_after_origin_jog = False
    restore_hard_limits_after_origin_jog = False
    try:
        output("Connecting to gantry...")
        gantry.connect()

        _apply_calibration_grbl_baseline(gantry, raw_config, output=output)
        output("Homing to normalized back-right-top corner...")
        _set_serial_timeout_if_available(gantry, homing_serial_timeout_s)
        gantry.home()
        _set_serial_timeout_if_available(gantry, jog_serial_timeout_s)
        output("Forcing GRBL WPos status reporting ($10=0) and G90...")
        gantry.enforce_work_position_reporting()
        output("Activating G54 work coordinate system...")
        gantry.activate_work_coordinate_system("G54")
        output("Clearing transient G92 offsets before origin calibration...")
        gantry.clear_g92_offsets()
        initial_home_z_mm: float | None = None
        if z_reference_mode in ("prompt", "block"):
            initial_home_coords = dict(gantry.get_coordinates())
            initial_home_z_mm = float(initial_home_coords["z"])
        stdin_flusher()

        restore_soft_limits_after_origin_jog = (
            _temporarily_disable_soft_limits_for_origin_jog(
                gantry,
                output=output,
            )
        )
        restore_hard_limits_after_origin_jog = (
            _temporarily_enable_hard_limits_for_origin_jog(
                gantry,
                output=output,
            )
        )
        try:
            _interactive_jog_to_xy_origin(
                gantry,
                key_reader=key_reader,
                stdin_flusher=stdin_flusher,
                output=output,
                feed_rate=jog_feed_rate,
                initial_step_mm=jog_step_mm,
                limit_pull_off_mm=limit_pull_off_mm,
                z_reference_mode=z_reference_mode,
                origin_policy=origin_policy.value,
            )
        finally:
            # Re-enable soft limits before dropping the hard-limit backstop.
            if restore_soft_limits_after_origin_jog:
                restore_soft_limits_after_origin_jog = False
                _restore_soft_limits_after_origin_jog(gantry, output=output)
            if restore_hard_limits_after_origin_jog:
                restore_hard_limits_after_origin_jog = False
                _restore_hard_limits_after_origin_jog(gantry, output=output)

        output("Setting current physical pose to WPos X=0, Y=0...")
        gantry.set_work_coordinates(x=0.0, y=0.0)
        xy_origin_coords = dict(gantry.get_coordinates())
        _assert_near_xy_origin(
            xy_origin_coords,
            tolerance_mm=tolerance_mm,
        )
        output(
            "Verified XY origin WPos: "
            f"X={xy_origin_coords['x']:.3f} "
            f"Y={xy_origin_coords['y']:.3f} "
            f"Z={xy_origin_coords['z']:.3f}"
        )

        if z_reference_mode == "prompt":
            z_reference_mode = _prompt_z_reference_mode(
                input_reader=input_reader,
                output=output,
            )
        block_height_mm: float | None = None
        block_touch_wpos_z_mm: float | None = None
        home_to_block_travel_mm: float | None = None
        remaining_below_block_mm: float | None = None
        can_reach_deck_bottom: bool | None = None
        expected_home_z_mm: float | None = None
        if z_reference_mode == "bottom":
            if tip_gap_mm is not None and tip_gap_mm != 0:
                raise ValueError("Bottom Z mode cannot use a non-zero tip gap.")
            z_min_mm = 0.0
            z_reference_wpos_mm = z_min_mm
            reachable_z_min_mm = z_min_mm
        elif z_reference_mode == "block":
            block_height_mm = _calibration_block_height_mm(
                raw_config,
                explicit_block_height_mm=tip_gap_mm,
                max_height_mm=factory_z_travel_mm,
                input_reader=input_reader,
                output=output,
            )
            block_touch_wpos_z_mm = float(xy_origin_coords["z"])
            if initial_home_z_mm is None:
                raise RuntimeError(
                    "Initial homed Z was not recorded before block calibration."
                )
            block_calibration = _calculate_block_z_calibration(
                initial_home_z_mm=initial_home_z_mm,
                block_touch_wpos_z_mm=block_touch_wpos_z_mm,
                block_height_mm=block_height_mm,
                factory_z_travel_mm=factory_z_travel_mm,
                tolerance_mm=tolerance_mm,
            )
            z_min_mm = block_calibration.z_min_mm
            z_reference_wpos_mm = block_height_mm
            reachable_z_min_mm = block_calibration.z_min_mm
            home_to_block_travel_mm = block_calibration.home_to_block_travel_mm
            remaining_below_block_mm = block_calibration.remaining_below_block_mm
            can_reach_deck_bottom = block_calibration.can_reach_deck_bottom
            expected_home_z_mm = block_calibration.expected_home_z_mm
        elif z_reference_mode == "ruler-gap":
            if tip_gap_mm is None:
                tip_gap_mm = _prompt_tip_gap_mm(
                    input_reader=input_reader,
                    output=output,
                )
            if tip_gap_mm <= 0:
                raise ValueError("tip_gap_mm must be > 0 in ruler-gap mode.")
            z_min_mm = 0.0
            z_reference_wpos_mm = z_min_mm
            reachable_z_min_mm = _round_mm(tip_gap_mm)
        else:
            raise ValueError(
                f"Unrecognised z_reference_mode after resolution: {z_reference_mode!r}. "
                "Expected one of: bottom, block, ruler-gap."
            )
        output(f"Setting current physical pose to WPos Z={z_reference_wpos_mm:g}...")
        gantry.set_work_coordinates(z=z_reference_wpos_mm)
        z_reference_coords = dict(gantry.get_coordinates())
        _assert_near_z_reference(
            z_reference_coords,
            z_min_mm=z_reference_wpos_mm,
            tolerance_mm=tolerance_mm,
        )
        output(
            "Verified Z reference WPos: "
            f"X={z_reference_coords['x']:.3f} "
            f"Y={z_reference_coords['y']:.3f} "
            f"Z={z_reference_coords['z']:.3f}"
        )

        output("Re-homing to measure physical working-volume maxima...")
        _set_serial_timeout_if_available(gantry, homing_serial_timeout_s)
        gantry.home()
        _set_serial_timeout_if_available(gantry, jog_serial_timeout_s)
        measured_coords = gantry.get_coordinates()
        homing_pull_off_mm = gantry.homing_pull_off_mm()
        _assert_positive_measured_volume(
            measured_coords,
            tolerance_mm=tolerance_mm,
        )
        z_max_mm = _round_mm(float(measured_coords["z"]))
        z_span_mm = _round_mm(z_max_mm - z_min_mm)
        max_travel = _calculate_grbl_max_travel(
            measured_coords,
            z_min_mm=z_min_mm,
            tolerance_mm=tolerance_mm,
            z_span_mm=z_span_mm,
            homing_pull_off_mm=homing_pull_off_mm,
        )
        _print_config_patch(
            measured_coords,
            z_reference_coords=z_reference_coords,
            z_min_mm=z_min_mm,
            z_max_mm=z_max_mm,
            factory_z_travel_mm=factory_z_travel_mm,
            z_reference_mode=z_reference_mode,
            instrument_name=instrument_name,
            block_height_mm=block_height_mm,
            block_touch_wpos_z_mm=block_touch_wpos_z_mm,
            reachable_z_min_mm=reachable_z_min_mm,
            home_to_block_travel_mm=home_to_block_travel_mm,
            remaining_below_block_mm=remaining_below_block_mm,
            can_reach_deck_bottom=can_reach_deck_bottom,
            expected_home_z_mm=expected_home_z_mm,
            output=output,
            origin_policy=origin_policy.value,
        )
        gantry_yaml_text = _updated_gantry_yaml_text(
            raw_config,
            measured_coords=measured_coords,
            z_min_mm=z_min_mm,
            z_max_mm=z_max_mm,
            max_travel=max_travel,
            homing_pull_off_mm=homing_pull_off_mm,
            calibration_block_height_mm=block_height_mm,
            origin_policy=origin_policy.value,
        )
        _print_yaml_block(
            title="Full gantry YAML to copy/paste:",
            yaml_text=gantry_yaml_text,
            output=output,
        )
        written_yaml_path = _maybe_write_gantry_yaml(
            yaml_text=gantry_yaml_text,
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

        return DeckOriginCalibrationResult(
            measured_working_volume=(
                float(measured_coords["x"]),
                float(measured_coords["y"]),
                z_max_mm,
            ),
            xy_origin_verification=_coords_tuple(xy_origin_coords),
            z_reference_verification=_coords_tuple(z_reference_coords),
            z_min_mm=z_min_mm,
            factory_z_travel_mm=factory_z_travel_mm,
            z_reference_mode=z_reference_mode,
            reachable_z_min_mm=reachable_z_min_mm,
            block_height_mm=block_height_mm,
            block_touch_wpos_z_mm=block_touch_wpos_z_mm,
            home_to_block_travel_mm=home_to_block_travel_mm,
            can_reach_deck_bottom=can_reach_deck_bottom,
            grbl_max_travel=(
                max_travel["max_travel_x"],
                max_travel["max_travel_y"],
                max_travel["max_travel_z"],
            ),
            instrument_name=instrument_name,
            plan=plan,
        )
    finally:
        try:
            if restore_soft_limits_after_origin_jog:
                restore_soft_limits_after_origin_jog = False
                _restore_soft_limits_after_origin_jog(gantry, output=output)
        finally:
            try:
                if restore_hard_limits_after_origin_jog:
                    restore_hard_limits_after_origin_jog = False
                    _restore_hard_limits_after_origin_jog(gantry, output=output)
            finally:
                _cancel_jog_if_available(gantry, output=output)
                _set_serial_timeout_if_available(gantry, 0.05)
                output("Disconnecting...")
                gantry.disconnect()
