"""Protocol commands: camera capture and the composed well-imaging sequence.

``capture`` grabs one frame wherever the gantry currently is; ``image_well``
packages the common case: move over a well, light it, capture, lights off,
retract. Both are built from generic gantry primitives plus the
vendor-agnostic camera/lighting interfaces. Inside ``image_well``, capture
and lighting failures log and continue (an image is never worth failing a
run over) while motion failures still raise. Saved image paths persist
through the data store's ``camera_measurements`` table; files land under
``~/.cubos/images`` (override with ``CUBOS_IMAGES_DIR``).
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from cubos.instruments.camera.exceptions import CameraError
from cubos.instruments.camera.interface import CameraInstrument
from cubos.instruments.lighting.exceptions import LightingError
from cubos.instruments.lighting.interface import LightingInstrument

from ..errors import ProtocolExecutionError
from ..registry import protocol_command
from . import _summaries
from ._movement import _assert_finite_number
from .lights import _get_lighting

if TYPE_CHECKING:
    from ..runtime import ProtocolContext

IMAGES_DIR_ENV = "CUBOS_IMAGES_DIR"

# Curvature-mode defaults: 11 planes descending 0.2 mm per step from the
# imaging height, contact (red+blue) lights at 50%.
_CURVATURE_DEFAULT_Z_STEPS = 11
_CURVATURE_DEFAULT_Z_STEP_MM = 0.2
_CURVATURE_DEFAULT_CHANNEL = "contact"
_CURVATURE_DEFAULT_BRIGHTNESS = 50

# Settle after motion before lighting/capturing.
_SETTLE_S = 0.2


def default_images_dir() -> Path:
    """Return the root directory for protocol capture images."""
    override = os.environ.get(IMAGES_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cubos" / "images"


def _get_camera(context: "ProtocolContext", instrument: str) -> CameraInstrument:
    try:
        camera = context.gantry.instruments[instrument]
    except KeyError as exc:
        raise ProtocolExecutionError(
            f"No instrument {instrument!r} registered on the gantry."
        ) from exc
    if not isinstance(camera, CameraInstrument):
        raise ProtocolExecutionError(
            f"Instrument {instrument!r} is a {type(camera).__name__}, not a "
            "CameraInstrument. capture/image_well require a `camera` type "
            "instrument."
        )
    return camera


def _safe_filename_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9.=_-]+", "-", value.strip())
    return cleaned.strip("-") or "image"


def build_image_path(
    context: "ProtocolContext",
    label: str | None,
    instrument: str,
) -> Path:
    """Build a collision-safe image path under the images root.

    Layout: ``<root>/campaign_<id>/<label>_<YYYYmmdd-HHMMSS>.tiff`` (an
    ``adhoc`` directory when the run has no campaign), with a numeric
    suffix when the same second produces multiple captures.
    """
    root = default_images_dir()
    group = (
        f"campaign_{context.campaign_id}"
        if context.campaign_id is not None
        else "adhoc"
    )
    stem = _safe_filename_part(label or instrument)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    directory = root / group
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}_{stamp}.tiff"
    counter = 1
    while path.exists():
        path = directory / f"{stem}_{stamp}_{counter:03d}.tiff"
        counter += 1
    return path


def _persist_image(
    context: "ProtocolContext",
    position: str | None,
    image_path: str,
) -> None:
    """Record *image_path* against the run's campaign, best-effort.

    Needs an active data store + campaign and a deck ``position`` to
    attribute the image to; persistence failures log rather than fail the
    run — the file on disk is never lost.
    """
    if context.data_store is None or context.campaign_id is None:
        return
    if position is None:
        context.logger.info(
            "capture: no position given; image %s saved but not recorded "
            "against labware.", image_path,
        )
        return
    try:
        target = context.deck.resolve_labware_target(position)
        context.data_store.log_experiment_measurement(
            campaign_id=context.campaign_id,
            labware_key=target.labware_key,
            labware_name=target.labware_name,
            well_id=target.location_id,
            contents_json=None,
            result=image_path,
        )
    except Exception as exc:
        context.logger.warning(
            "Failed to record image %s for position %s: %s",
            image_path, position, exc, exc_info=True,
        )


@protocol_command("capture", summary=_summaries.capture)
def capture(
    context: "ProtocolContext",
    instrument: str,
    label: str | None = None,
    position: str | None = None,
) -> str:
    """Capture an image where the gantry currently is; return the file path.

    No motion of its own — compose with ``move`` and ``set_lights`` in the
    protocol YAML. ``position`` (a deck target like ``plate_1.A1``) is
    optional and only used to attribute the image to labware in the data
    store; it does not move the camera.

    Args:
        context:    Runtime context (instrumented gantry, deck, logger).
        instrument: Name of the camera instrument registered on the gantry.
        label:      Optional filename label for the saved image.
        position:   Optional deck target the image belongs to (persistence
                    attribution only).
    """
    camera = _get_camera(context, instrument)
    path = build_image_path(context, label, instrument)
    try:
        saved = camera.capture(save_path=str(path))
    except CameraError as exc:
        raise ProtocolExecutionError(f"capture: {exc}") from exc
    context.logger.info("capture: %s -> %s", instrument, saved)
    _persist_image(context, position, saved)
    return saved


def _resolve_lighting(
    context: "ProtocolContext", lights: str | None,
) -> LightingInstrument | None:
    """Resolve the lighting instrument, defaulting to the gantry's only one."""
    if lights == "none":
        return None
    if lights is not None:
        return _get_lighting(context, lights)
    found = [
        instrument
        for instrument in context.gantry.instruments.values()
        if isinstance(instrument, LightingInstrument)
    ]
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        names = sorted(
            name
            for name, instrument in context.gantry.instruments.items()
            if isinstance(instrument, LightingInstrument)
        )
        raise ProtocolExecutionError(
            "image_well: multiple lighting instruments on the gantry "
            f"({', '.join(names)}); pass `lights` to pick one."
        )
    return None


@protocol_command("image_well", summary=_summaries.image_well)
def image_well(
    context: "ProtocolContext",
    camera: str,
    well: str,
    image_height: float,
    lights: str | None = None,
    light: str | None = None,
    label: str | None = None,
    mode: str = "standard",
    brightness: int | None = None,
    z_steps: int = _CURVATURE_DEFAULT_Z_STEPS,
    z_step_mm: float = _CURVATURE_DEFAULT_Z_STEP_MM,
) -> list[str]:
    """Move the camera over *well*, light it, capture, lights off, retract.

    * ``standard`` — one shot: descend to ``well.z + image_height``.
    * ``curvature`` — Z-stack: descend ``z_step_mm`` per plane for
      ``z_steps`` planes; images are labeled ``{label}_z={absolute_z}mm_b{brightness}``,
      where ``absolute_z`` is the carriage WPos Z at that plane (well Z +
      offset + camera depth), matching the gantry readout.

    Lighting is part of the shot, not a separate step: ``light`` picks
    ``"off"`` (ambient), a lighting channel (e.g. ``"white"`` or
    ``"contact"`` — the red+blue LEDs), or, when omitted, the mode's
    default (standard→white, curvature→contact). ``brightness`` is 0–100
    (0 = off); it is snapped to the nearest level the lighting hardware
    actually supports for that channel.

    ``image_height`` is a labware-relative offset (mm above the well's
    surface Z), like ``measure``'s ``measurement_height``. ``lights``
    names the lighting *instrument* and defaults to the gantry's lighting
    instrument when it has exactly one. Capture and lighting failures log
    and continue; motion failures raise. Lights-off and the retract to
    ``safe_z`` run even on failure. Returns the saved image paths.
    """
    camera_instr = _get_camera(context, camera)
    lighting = _resolve_lighting(context, lights)
    if mode not in ("standard", "curvature"):
        raise ProtocolExecutionError(
            f"image_well: unknown mode {mode!r}; expected 'standard' or "
            "'curvature'."
        )
    try:
        _assert_finite_number(
            image_height, field_name="image_height", source="image_well",
        )
    except ValueError as exc:
        raise ProtocolExecutionError(str(exc)) from exc
    if mode == "curvature":
        if z_steps < 1:
            raise ProtocolExecutionError("image_well: z_steps must be >= 1.")
        if z_step_mm < 0:
            raise ProtocolExecutionError("image_well: z_step_mm must be >= 0.")

    if brightness is not None and not (0 <= brightness <= 100):
        raise ProtocolExecutionError(
            f"image_well: brightness must be between 0 and 100, got {brightness}."
        )
    if mode == "standard":
        default_channel, default_level = "white", 5
    else:
        default_channel = _CURVATURE_DEFAULT_CHANNEL
        default_level = _CURVATURE_DEFAULT_BRIGHTNESS
    channel = default_channel if light in (None, "off") else light
    level = brightness if brightness is not None else default_level
    lights_off = light == "off" or level == 0
    if not lights_off and lighting is not None:
        supported = lighting.channels.get(channel)
        if supported is None:
            raise ProtocolExecutionError(
                f"image_well: unknown light {channel!r}; available channels: "
                f"{', '.join(sorted(lighting.channels))} (or 'off')."
            )
        if level not in supported:
            snapped = min(supported, key=lambda value: abs(value - level))
            context.logger.info(
                "image_well: brightness %d is not a supported %s level; "
                "using nearest supported %d", level, channel, snapped,
            )
            level = snapped
    if lights_off:
        level = 0

    try:
        coord = context.deck.resolve_coordinate(well)
    except Exception as exc:
        raise ProtocolExecutionError(
            f"image_well: cannot resolve well {well!r}: {exc}"
        ) from exc

    saved_paths: list[str] = []
    base_label = label or f"{well}"

    def _capture_one(shot_label: str) -> None:
        path = build_image_path(context, shot_label, camera)
        try:
            saved = camera_instr.capture(save_path=str(path))
        except CameraError as exc:
            context.logger.warning(
                "image_well: capture %r failed (continuing): %s",
                shot_label, exc,
            )
            return
        saved_paths.append(saved)
        _persist_image(context, well, saved)

    def _lights(action: str, *args) -> bool:
        if lighting is None:
            return True
        try:
            getattr(lighting, action)(*args)
            return True
        except LightingError as exc:
            context.logger.warning(
                "image_well: lighting %s failed (continuing): %s", action, exc,
            )
            return False

    try:
        context.gantry.move_to_labware(camera, coord)
        planes = (
            [image_height]
            if mode == "standard"
            else [
                image_height - index * z_step_mm for index in range(z_steps)
            ]
        )
        for plane in planes:
            context.gantry.move(
                camera, (coord.x, coord.y, coord.z + plane),
            )
            time.sleep(_SETTLE_S)
            if mode == "standard":
                shot_label = base_label
            else:
                # Label with the carriage WPos Z the operator sees on the
                # gantry readout, not the deck-frame focal plane.
                wpos_z = coord.z + plane + context.gantry.instruments[camera].effective_depth
                z_text = f"{wpos_z:.3f}".replace(".", "-")
                shot_label = f"{base_label}_z={z_text}mm_b{level}"
            if lights_off:
                _capture_one(shot_label)
            elif _lights("set_channel", channel, level):
                _capture_one(shot_label)
            _lights("all_off")
    finally:
        _lights("all_off")
        safe_z = context.gantry.safe_z
        if safe_z is not None:
            try:
                context.gantry.move(
                    camera, (coord.x, coord.y, safe_z), travel_z=safe_z,
                )
            except Exception as exc:
                context.logger.error(
                    "image_well: retract to safe_z failed; manual hardware "
                    "check required: %s", exc, exc_info=True,
                )

    context.logger.info(
        "image_well: %s captured %d image(s) at %s",
        camera, len(saved_paths), well,
    )
    return saved_paths
