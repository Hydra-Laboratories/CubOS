"""Protocol commands for pipette operations."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, List, Optional

from cubos.deck.labware.tip_rack import (
    TipRackResolutionError,
    resolve_tip_rack_slot,
)
from cubos.deck.labware.well_plate import WellPlate
from cubos.instruments.pipette.liquid_class import IDENTITY_CORRECTION

from ..errors import ProtocolExecutionError
from ..registry import protocol_command
from . import _summaries
from ._cap_preflight import require_uncapped as _require_uncapped
from ._liquid_selection import (
    LiquidSelectionError,
    select_stock_container,
    select_waste_container,
    target_position,
)
from ._liquid_transfer import (
    LiquidTransferPreflightError,
    derive_liquid_relative_height,
    pipette_capacity,
    plan_strokes,
    validate_dead_volume,
    validate_destination_overflow,
    vial_for_target,
)
from ._movement import engage_at_labware

logger = logging.getLogger(__name__)

# Reason text for steps the durable fluid/tip journal reports as already
# applied on a resumed run. Deliberately distinct from "never reached": the
# operator needs to read a resumed run as having done this work, just not in
# this process.
_ALREADY_APPLIED = "already applied on a previous run"

if TYPE_CHECKING:
    from ..runtime import ProtocolContext


def _get_pipette(context: ProtocolContext):
    """Return the pipette instrument or raise ProtocolExecutionError."""
    if "pipette" not in context.gantry.instruments:
        raise ProtocolExecutionError(
            "No pipette registered on the gantry. "
            "Add one under the gantry YAML top-level `instruments` key."
        )
    return context.gantry.instruments["pipette"]


def _engage(
    context: ProtocolContext,
    position: str,
    *,
    command_label: str,
    height: float = 0.0,
) -> tuple[float, float, float]:
    """Wrap ``engage_at_labware`` so configuration errors surface as
    ``ProtocolExecutionError`` instead of bare ``ValueError``s — matching
    how ``measure`` and ``scan`` handle their command boundary.

    Pipette commands default to the resolved labware coordinate
    (``height=0``). ``height`` is a labware-relative
    offset in the same convention as ``measurement_height`` for measure/scan.
    Returns the engaged tip ``(x, y, z)``."""
    try:
        x, y, _, action_z = engage_at_labware(
            context, "pipette", position,
            measurement_height=height, command_label=command_label,
        )
        return x, y, action_z
    except ValueError as exc:
        raise ProtocolExecutionError(str(exc)) from exc


def _tracked_fluid_state(context: ProtocolContext) -> bool:
    """Return whether durable tracking is active, rejecting partial context.

    A normal campaign may have a data store without a fluid state. Once a
    ``fluid_state_id`` is present, however, both the store and campaign link
    are required before any motion or liquid actuation.
    """
    if context.fluid_state_id is None:
        return False
    missing = []
    if context.data_store is None:
        missing.append("data_store")
    if context.campaign_id is None:
        missing.append("campaign_id")
    if missing:
        raise ProtocolExecutionError(
            "Durable fluid tracking context is incomplete: fluid_state_id is "
            f"set but {', '.join(missing)} is missing. No motion was attempted."
        )
    return True


def _parse_position(position: str) -> tuple[str, Optional[str]]:
    """Compatibility helper that preserves nested labware paths.

    Runtime tracking resolves targets through :class:`deck.deck.Deck`; this
    helper remains for callers that only need the string split.
    """
    if "." not in position:
        return position, None
    labware_key, location_id = position.rsplit(".", 1)
    return labware_key, location_id


def _resolve_fluid_target(context: ProtocolContext, position: str):
    try:
        return context.deck.resolve_labware_target(position)
    except (KeyError, ValueError) as exc:
        raise ProtocolExecutionError(
            f"Cannot resolve tracked fluid target {position!r}: {exc}"
        ) from exc


def _begin_tracked_transfer(
    context: ProtocolContext,
    *,
    source: str,
    destination: str,
    source_target: Any,
    destination_target: Any,
    volume_ul: float,
) -> tuple[str, bool]:
    """Preflight and journal a tracked transfer before liquid actuation.

    *source_target*/*destination_target* are already-resolved
    ``DeckLabwareTarget``s (resolved once per ``transfer()`` call and reused
    across every stroke) rather than re-resolved from the raw strings here,
    so a multi-stroke transfer only ever hits ``context.deck.resolve_labware_
    target`` twice regardless of stroke count.

    Returns the operation key and whether hardware should execute. ``False``
    means this exact campaign step was already applied and must not be replayed.
    """
    try:
        operation_key = context.fluid_operation_key("transfer")
        should_execute = context.data_store.begin_fluid_transfer(
            context.fluid_state_id,
            operation_key,
            source_target.labware_key,
            source_target.location_id,
            destination_target.labware_key,
            destination_target.location_id,
            volume_ul,
            campaign_id=context.campaign_id,
        )
    except Exception as exc:
        raise ProtocolExecutionError(
            f"Fluid-state preflight failed for {source!r} -> "
            f"{destination!r}: {type(exc).__name__}: {exc}"
        ) from exc
    return operation_key, should_execute


def _begin_tracked_mix(
    context: ProtocolContext,
    *,
    position: str,
    volume_ul: float,
    cycles: int,
    speed: float,
    height: float,
) -> tuple[str, bool]:
    target = _resolve_fluid_target(context, position)
    try:
        operation_key = context.fluid_operation_key("mix")
        should_execute = context.data_store.begin_fluid_mix(
            context.fluid_state_id,
            operation_key,
            target,
            volume_ul,
            cycles,
            speed,
            height,
            campaign_id=context.campaign_id,
        )
    except Exception as exc:
        raise ProtocolExecutionError(
            f"Fluid-state mix preflight failed for {position!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return operation_key, should_execute


def _record_transfer_to_store(
    context: ProtocolContext,
    source: str,
    destination: str,
    volume_ul: float,
) -> None:
    """Persist an ordinary campaign transfer using canonical deck identity."""
    if context.data_store is not None and context.campaign_id is not None:
        try:
            source_target = context.deck.resolve_labware_target(source)
            destination_target = context.deck.resolve_labware_target(destination)
            context.data_store.record_transfer(
                context.campaign_id,
                source_target.labware_key,
                source_target.location_id,
                destination_target.labware_key,
                destination_target.location_id,
                volume_ul,
            )
        except Exception as exc:
            logger.warning(
                "Failed to record transfer from %s to %s: %s",
                source, destination, exc,
                exc_info=True,
            )


def _mark_transfer_uncertain(
    context: ProtocolContext,
    operation_key: str,
    exc: BaseException,
) -> None:
    """Best-effort marker for physical actions whose outcome needs review."""
    try:
        context.data_store.mark_fluid_reconciliation_required(
            operation_key,
            f"{type(exc).__name__}: {exc}",
        )
    except Exception:
        logger.exception(
            "Failed to mark fluid operation %s reconciliation-required",
            operation_key,
        )


def _mark_transfer_stroke_uncertain(
    context: ProtocolContext,
    operation_key: str,
    *,
    stroke_index: int,
    stroke_count: int,
    exc: BaseException,
) -> None:
    """Best-effort marker carrying which stroke of a split transfer failed."""
    try:
        context.data_store.mark_fluid_reconciliation_required(
            operation_key,
            f"stroke {stroke_index + 1}/{stroke_count}: "
            f"{type(exc).__name__}: {exc}",
        )
    except Exception:
        logger.exception(
            "Failed to mark fluid operation %s reconciliation-required "
            "(stroke %d/%d)",
            operation_key, stroke_index + 1, stroke_count,
        )




def _tip_rack_key(position: str) -> str:
    """Return the deck path used to resolve *position*'s owning TipRack.

    Mirrors ``resolve_tip_rack_slot``'s own rsplit so the durable tip-state
    rack identity always matches what was used to resolve the rack.
    """
    return position.rsplit(".", 1)[0] if "." in position else position


def _mark_tip_uncertain(
    context: ProtocolContext,
    operation_key: str,
    exc: BaseException,
) -> None:
    """Best-effort marker for physical tip actions whose outcome needs review."""
    try:
        context.data_store.mark_tip_reconciliation_required(
            operation_key,
            f"{type(exc).__name__}: {exc}",
        )
    except Exception:
        logger.exception(
            "Failed to mark tip operation %s reconciliation-required",
            operation_key,
        )


@protocol_command("aspirate", summary=_summaries.aspirate)
def aspirate(
    context: ProtocolContext,
    position: str,
    volume_ul: float,
    speed: float = 50.0,
    height: float = 0.0,
) -> Any:
    """Move pipette to *position*, then aspirate."""
    if _tracked_fluid_state(context):
        raise ProtocolExecutionError(
            "Standalone aspirate is disabled while durable fluid tracking is "
            "active because it cannot conserve well/vial state without a "
            "known destination. Use transfer or serial_transfer."
        )
    pipette = _get_pipette(context)
    _engage(context, position, command_label="aspirate", height=height)
    return pipette.aspirate(volume_ul, speed)


def dispense(
    context: ProtocolContext,
    position: str,
    volume_ul: float,
    speed: float = 50.0,
    height: float = 0.0,
) -> Any:
    """Move pipette to *position*, then dispense.

    Not exposed as a YAML protocol command — use ``transfer`` instead,
    which correctly tracks source labware for DB logging.
    """
    if _tracked_fluid_state(context):
        raise ProtocolExecutionError(
            "Standalone dispense is disabled while durable fluid tracking is "
            "active because its source is unknown. Use transfer or serial_transfer."
        )
    pipette = _get_pipette(context)
    _engage(context, position, command_label="dispense", height=height)
    return pipette.dispense(volume_ul, speed)


@protocol_command("blowout", summary=_summaries.blowout)
def blowout(
    context: ProtocolContext,
    position: str,
    speed: float = 50.0,
    height: float = 0.0,
) -> None:
    """Move pipette to *position*, then blowout."""
    pipette = _get_pipette(context)
    _engage(context, position, command_label="blowout", height=height)
    pipette.blowout(speed)


@protocol_command("mix", summary=_summaries.mix)
def mix(
    context: ProtocolContext,
    position: str,
    volume_ul: float,
    cycles: int = 3,
    speed: float = 50.0,
    height: float = 0.0,
) -> Any:
    """Move pipette to *position* at *height*, then mix ``cycles`` times
    between that height and 1 mm above it."""
    tracked = _tracked_fluid_state(context)
    pipette = _get_pipette(context)
    operation_key = None
    if tracked:
        operation_key, should_execute = _begin_tracked_mix(
            context,
            position=position,
            volume_ul=volume_ul,
            cycles=cycles,
            speed=speed,
            height=height,
        )
        if not should_execute:
            context.logger.info(
                "Skipping already-applied fluid operation %s", operation_key,
            )
            context.notify_step("step_skipped", reason=_ALREADY_APPLIED)
            return None
    try:
        tip = _engage(context, position, command_label="mix", height=height)
        result = pipette.mix(
            volume_ul, cycles, speed, gantry=context.gantry, position=tip,
        )
    except BaseException as exc:
        if operation_key is not None:
            _mark_transfer_uncertain(context, operation_key, exc)
        raise
    if operation_key is not None:
        try:
            context.data_store.complete_fluid_mix(operation_key)
        except Exception as exc:
            _mark_transfer_uncertain(context, operation_key, exc)
            raise ProtocolExecutionError(
                "Physical mix completed, but its fluid-state commit failed for "
                f"operation {operation_key!r}: {type(exc).__name__}: {exc}. "
                "Reconciliation is required."
            ) from exc
    return result


@protocol_command("pick_up_tip", summary=_summaries.pick_up_tip)
def pick_up_tip(
    context: ProtocolContext,
    position: str,
    speed: float = 50.0,
) -> None:
    """Move pipette to *position*, then pick up a tip.

    *position* may name a specific slot (``"tips.A1"``) or a whole rack
    (``"tips"``) for next-available selection using the rack's tip order.

    When durable fluid/tip tracking is active (``context.fluid_state_id``),
    the pickup is journaled with the same two-phase begin/complete pattern as
    fluid transfers: the slot is reserved *before* motion, committed to
    ``attached`` only after ``pipette.pick_up_tip`` succeeds, and marked
    ``reconciliation_required`` (blocking further liquid handling) if the
    physical outcome is uncertain. Re-running an already-applied step is a
    no-op that restores the pipette's tip extension without picking a second
    tip. Without tracking, tip-rack consumption is in-memory only: re-running
    a protocol cannot know which physical slots were emptied by an earlier
    run, so operators must refresh/replace racks or update the deck
    definition before reruns.
    """
    pipette = _get_pipette(context)
    try:
        rack, tip_id = resolve_tip_rack_slot(context.deck, position)
    except TipRackResolutionError as exc:
        raise ProtocolExecutionError(str(exc)) from exc
    rack_key = _tip_rack_key(position)

    tracked = _tracked_fluid_state(context)
    operation_key = None
    if tracked:
        operation_key = context.fluid_operation_key("pick_up_tip")
        try:
            should_execute, resolved_tip_id, extension_mm = (
                context.data_store.begin_pick_up_tip(
                    context.fluid_state_id,
                    operation_key,
                    rack_key,
                    tip_id,
                    rack.tip_length,
                    campaign_id=context.campaign_id,
                )
            )
        except Exception as exc:
            raise ProtocolExecutionError(
                f"Tip-state preflight failed for pick_up_tip at {position!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not should_execute:
            pipette.set_attached_tip_extension(extension_mm)
            context.logger.info(
                "Skipping already-applied tip operation %s", operation_key,
            )
            context.notify_step("step_skipped", reason=_ALREADY_APPLIED)
            return
        tip_id = resolved_tip_id
        position = f"{rack_key}.{tip_id}"
    else:
        if tip_id is None:
            tip_id = rack.next_available_tip()
            if tip_id is None:
                raise ProtocolExecutionError(
                    f"pick_up_tip rack {rack_key!r} has no available tips."
                )
            position = f"{rack_key}.{tip_id}"
        if not rack.is_tip_present(tip_id):
            raise ProtocolExecutionError(
                f"pick_up_tip target {position!r} is not available "
                "(slot is unknown or already consumed)."
            )

    try:
        _engage(context, position, command_label="pick_up_tip")
        pipette.pick_up_tip(speed)
    except BaseException as exc:
        if operation_key is not None:
            _mark_tip_uncertain(context, operation_key, exc)
        raise
    pipette.set_attached_tip_extension(rack.tip_length)
    rack.mark_tip_used(tip_id)
    if operation_key is not None:
        try:
            context.data_store.complete_pick_up_tip(operation_key)
        except Exception as exc:
            _mark_tip_uncertain(context, operation_key, exc)
            raise ProtocolExecutionError(
                "Physical tip pickup completed, but its tip-state commit "
                f"failed for operation {operation_key!r}: "
                f"{type(exc).__name__}: {exc}. Reconciliation is required."
            ) from exc


@protocol_command("transfer", summary=_summaries.transfer)
def transfer(
    context: ProtocolContext,
    source: str,
    destination: str,
    volume_ul: float,
    speed: float = 50.0,
    source_height: Optional[float] = None,
    destination_height: Optional[float] = None,
    liquid_class: Optional[str] = None,
    require_uncapped: Optional[List[str]] = None,
) -> None:
    """Aspirate from *source* and dispense into *destination*.

    ``require_uncapped`` (optional list of deck targets, typically
    ``source``/``destination`` themselves) is checked FIRST, before any
    other preflight or motion: every named target must be durably tracked
    ``uncapped`` (see ``cubos.data.cap_state``) or the command fails
    immediately with an error naming exactly which vial needs `decap`
    first. This is an explicit opt-in check only -- it never runs `decap`
    itself. A target with no durable cap state at all (not capper-managed)
    is not constrained by this check.

    Safety preflight runs entirely before any motion or durable journaling:
    rejects ``volume_ul <= 0``, volume below the configured pipette model's
    ``min_volume``, source draws that would cross its ``dead_volume_ul``
    floor, and destination fills that would exceed ``working_volume_ul``.
    Preflight and splitting only engage when *pipette* exposes a real
    ``PipetteConfig`` (see ``_liquid_transfer.pipette_capacity``); bare test
    doubles fall back to the single-stroke, unvalidated pre-Feature-04
    behavior.

    Volumes above the model's ``max_volume`` split deterministically into
    capacity-bounded strokes (``_liquid_transfer.plan_strokes``). Each
    stroke is its own durable fluid operation reusing the existing
    begin/complete journal (one ``operation_key`` per stroke, scoped via
    ``context.active_substep``): this makes a multi-stroke transfer a
    logically-one action whose per-stroke sub-state is independently
    recoverable -- a crash after stroke *N* leaves strokes ``< N`` applied
    and terminal, stroke *N* itself ``reconciliation_required`` (with a
    stroke-numbered detail message), and strokes ``> N`` never started; a
    rerun skips every already-applied stroke via the existing idempotent
    ``begin_fluid_transfer`` check and only executes what's left. No new
    schema/journal was needed for this -- it composes entirely from the
    tip_state/fluid_state two-phase begin/complete primitives.

    ``source_height``/``destination_height`` keep the existing
    labware-relative sign convention (0 = labware reference Z, negative =
    below). Leave them unset (``None``, the default) to opt into
    state-derived aspiration height: when durable fluid tracking is active
    and the target resolves to a ``Vial`` with known geometry, the engage Z
    tracks the current liquid surface (floored at the dead-volume/bottom
    clearance -- see ``_liquid_transfer.derive_liquid_relative_height``).
    Otherwise (untracked, non-vial, or missing geometry) falls back to the
    legacy default of ``0.0``, identical to pre-Feature-04 behavior. Passing
    an explicit numeric value (including ``0.0``) always wins.

    ``liquid_class`` selects a per-pipette-instrument volume correction
    (``cubos.instruments.pipette.liquid_class``; disabled/identity by
    default), applied to the *driver-commanded* stroke volume only --
    durable fluid-state updates always move the *requested* (uncorrected)
    volume, so tracked container state reflects what was asked for while
    hardware receives whatever correction is calibrated to actually deliver
    it.

    Every transfer runs the pipette's calibrated blow-out motion once,
    right after the *final* stroke's dispense -- clearing any fluid left in
    the tip after the transfer is otherwise complete, rather than after
    every stroke of a multi-stroke transfer. It does not change the
    tracked dispense volume; a blow-out failure is treated the same as a
    dispense failure (the stroke is marked ``reconciliation_required``).
    """
    _require_uncapped(context, require_uncapped, command_label="transfer")

    tracked = _tracked_fluid_state(context)
    pipette = _get_pipette(context)
    capacity = pipette_capacity(pipette)

    # Only resolve through the pipette's own correction_for() when it's a
    # real driver (capacity is not None, see pipette_capacity() gating) --
    # bare test doubles don't implement PipetteInstrument and would
    # otherwise hand back an unconfigured Mock instead of a correction.
    if capacity is not None:
        try:
            correction = pipette.correction_for(liquid_class)
        except Exception as exc:
            raise ProtocolExecutionError(
                f"transfer liquid_class resolution failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
    elif liquid_class is not None:
        raise ProtocolExecutionError(
            "transfer liquid_class was requested but the pipette instrument "
            "does not expose model/correction metadata (no PipetteConfig)."
        )
    else:
        correction = IDENTITY_CORRECTION

    # Correction participates in planning: the driver is commanded the
    # corrected stroke volume, so strokes are sized such that even the
    # corrected volume never exceeds the model max (see plan_strokes).
    try:
        stroke_volumes = plan_strokes(volume_ul, capacity, correction)
    except LiquidTransferPreflightError as exc:
        raise ProtocolExecutionError(f"transfer preflight failed: {exc}") from exc

    resolved_source_height = source_height
    resolved_destination_height = destination_height
    source_target = None
    destination_target = None

    if tracked:
        # Resolve exactly once and reuse across every stroke below -- a
        # multi-stroke transfer must not multiply deck-target resolution
        # calls per stroke.
        source_target = _resolve_fluid_target(context, source)
        destination_target = _resolve_fluid_target(context, destination)
        try:
            source_container = context.data_store.get_fluid_container(
                context.fluid_state_id,
                source_target.labware_key,
                source_target.location_id or "",
            )
            destination_container = context.data_store.get_fluid_container(
                context.fluid_state_id,
                destination_target.labware_key,
                destination_target.location_id or "",
            )
        except Exception as exc:
            raise ProtocolExecutionError(
                f"Fluid-state preflight failed reading container state for "
                f"{source!r} -> {destination!r}: {type(exc).__name__}: {exc}"
            ) from exc

        # A real DataStore always returns a plain dict snapshot; test
        # doubles that don't configure get_fluid_container() return an
        # unconfigured Mock. Mirror pipette_capacity()'s gating: dead-volume/
        # overflow preflight and state-derived height only activate against
        # a genuine snapshot, so unit tests exercising other behavior with
        # bare mocks are unaffected.
        if isinstance(source_container, Mapping) and isinstance(
            destination_container, Mapping,
        ):
            source_vial = vial_for_target(source_target)
            dead_volume_ul = (
                float(getattr(source_vial, "dead_volume_ul", 0.0) or 0.0)
                if source_vial is not None else 0.0
            )
            try:
                validate_dead_volume(
                    source_current_volume_ul=source_container["current_volume_ul"],
                    dead_volume_ul=dead_volume_ul,
                    requested_volume_ul=volume_ul,
                    source_label=source,
                )
                validate_destination_overflow(
                    destination_current_volume_ul=destination_container["current_volume_ul"],
                    working_volume_ul=destination_container["working_volume_ul"],
                    requested_volume_ul=volume_ul,
                    destination_label=destination,
                )
            except LiquidTransferPreflightError as exc:
                raise ProtocolExecutionError(
                    f"transfer preflight failed: {exc}"
                ) from exc

            if resolved_source_height is None and source_vial is not None:
                derived = derive_liquid_relative_height(
                    source_vial, source_container["current_volume_ul"],
                )
                if derived is not None:
                    resolved_source_height = derived

            if resolved_destination_height is None:
                destination_vial = vial_for_target(destination_target)
                if destination_vial is not None:
                    derived = derive_liquid_relative_height(
                        destination_vial,
                        destination_container["current_volume_ul"],
                    )
                    if derived is not None:
                        resolved_destination_height = derived

    if resolved_source_height is None:
        resolved_source_height = 0.0
    if resolved_destination_height is None:
        resolved_destination_height = 0.0

    stroke_count = len(stroke_volumes)
    multi_stroke = stroke_count > 1
    previous_substep = context.active_substep
    try:
        for stroke_index, stroke_volume in enumerate(stroke_volumes):
            if multi_stroke:
                suffix = f"stroke{stroke_index}"
                context.active_substep = (
                    f"{previous_substep}:{suffix}" if previous_substep else suffix
                )
            _execute_transfer_stroke(
                context,
                pipette,
                source=source,
                destination=destination,
                source_target=source_target,
                destination_target=destination_target,
                stroke_volume_ul=stroke_volume,
                speed=speed,
                source_height=resolved_source_height,
                destination_height=resolved_destination_height,
                correction=correction,
                tracked=tracked,
                stroke_index=stroke_index,
                stroke_count=stroke_count,
                blow_out=stroke_index == stroke_count - 1,
            )
    finally:
        context.active_substep = previous_substep


def _execute_transfer_stroke(
    context: ProtocolContext,
    pipette: Any,
    *,
    source: str,
    destination: str,
    source_target: Any,
    destination_target: Any,
    stroke_volume_ul: float,
    speed: float,
    source_height: float,
    destination_height: float,
    correction: Any,
    tracked: bool,
    stroke_index: int,
    stroke_count: int,
    blow_out: bool = False,
) -> None:
    """Journal, actuate, and commit exactly one transfer stroke.

    One durable fluid operation per stroke (see ``transfer``'s docstring for
    the resumability rationale). ``correction`` is applied only to the
    volume handed to the pipette driver; the durable state update always
    uses ``stroke_volume_ul`` (the requested, uncorrected amount).

    ``blow_out`` fires the pipette's blow-out motion right after this
    stroke's dispense -- callers pass this only for the final stroke of a
    transfer (see ``transfer``'s docstring).
    """
    operation_key = None
    if tracked:
        operation_key, should_execute = _begin_tracked_transfer(
            context,
            source=source,
            destination=destination,
            source_target=source_target,
            destination_target=destination_target,
            volume_ul=stroke_volume_ul,
        )
        if not should_execute:
            context.logger.info(
                "Skipping already-applied fluid operation %s (stroke %d/%d)",
                operation_key, stroke_index + 1, stroke_count,
            )
            context.notify_step(
                "step_skipped",
                reason=(
                    f"{_ALREADY_APPLIED} "
                    f"(stroke {stroke_index + 1}/{stroke_count})"
                ),
            )
            return

    commanded_volume_ul = correction.apply(stroke_volume_ul)
    try:
        _engage(
            context, source, command_label="transfer.aspirate",
            height=source_height,
        )
        pipette.aspirate(commanded_volume_ul, speed)
        _engage(
            context, destination, command_label="transfer.dispense",
            height=destination_height,
        )
        pipette.dispense(commanded_volume_ul, speed)
        if blow_out:
            pipette.blowout(speed)
    except BaseException as exc:
        if operation_key is not None:
            _mark_transfer_stroke_uncertain(
                context, operation_key,
                stroke_index=stroke_index, stroke_count=stroke_count, exc=exc,
            )
        raise

    if operation_key is not None:
        try:
            context.data_store.complete_fluid_transfer(operation_key)
        except Exception as exc:
            _mark_transfer_stroke_uncertain(
                context, operation_key,
                stroke_index=stroke_index, stroke_count=stroke_count, exc=exc,
            )
            raise ProtocolExecutionError(
                "Physical transfer completed, but its fluid-state commit "
                f"failed for operation {operation_key!r} (stroke "
                f"{stroke_index + 1}/{stroke_count}): "
                f"{type(exc).__name__}: {exc}. Reconciliation is required."
            ) from exc
    else:
        _record_transfer_to_store(context, source, destination, stroke_volume_ul)


@protocol_command("drop_tip", summary=_summaries.drop_tip)
def drop_tip(
    context: ProtocolContext,
    position: str,
    speed: float = 50.0,
) -> None:
    """Move pipette to *position*, then drop the tip.

    When durable fluid/tip tracking is active, the drop is journaled the
    same way as pick_up_tip: reserved before motion, committed to
    ``consumed`` only after ``pipette.drop_tip`` succeeds, and marked
    ``reconciliation_required`` (blocking further liquid handling) if the
    physical outcome is uncertain.
    """
    pipette = _get_pipette(context)
    tracked = _tracked_fluid_state(context)
    operation_key = None
    if tracked:
        operation_key = context.fluid_operation_key("drop_tip")
        try:
            should_execute, _rack_key, _slot_id = context.data_store.begin_drop_tip(
                context.fluid_state_id,
                operation_key,
                campaign_id=context.campaign_id,
            )
        except Exception as exc:
            raise ProtocolExecutionError(
                f"Tip-state preflight failed for drop_tip at {position!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not should_execute:
            pipette.clear_attached_tip_extension()
            context.logger.info(
                "Skipping already-applied tip operation %s", operation_key,
            )
            context.notify_step("step_skipped", reason=_ALREADY_APPLIED)
            return

    try:
        _engage(context, position, command_label="drop_tip")
        pipette.drop_tip(speed)
    except BaseException as exc:
        if operation_key is not None:
            _mark_tip_uncertain(context, operation_key, exc)
        raise
    pipette.clear_attached_tip_extension()
    if operation_key is not None:
        try:
            context.data_store.complete_drop_tip(operation_key)
        except Exception as exc:
            _mark_tip_uncertain(context, operation_key, exc)
            raise ProtocolExecutionError(
                "Physical tip drop completed, but its tip-state commit "
                f"failed for operation {operation_key!r}: "
                f"{type(exc).__name__}: {exc}. Reconciliation is required."
            ) from exc


# -- Compound helpers ----------------------------------------------------------


def _linspace(start: float, end: float, n: int) -> list:
    """Return *n* evenly spaced values from *start* to *end* inclusive."""
    if n == 1:
        return [start]
    step = (end - start) / (n - 1)
    return [start + i * step for i in range(n)]


def _wells_for_axis(plate: WellPlate, axis: str) -> list:
    """Return well IDs along *axis* in natural order.

    If *axis* is a letter (e.g. ``"A"``), returns the row (A1, A2, ...).
    If *axis* is a digit string (e.g. ``"3"``), returns the column (A3, B3, ...).
    """
    if axis.isalpha():
        wells = [w for w in plate.wells if w[0] == axis.upper()]
    else:
        wells = [w for w in plate.wells if w[1:] == axis]
    return sorted(wells, key=lambda w: (w[0], int(w[1:])))


@protocol_command("serial_transfer", summary=_summaries.serial_transfer)
def serial_transfer(
    context: ProtocolContext,
    source: str,
    plate: str,
    axis: str,
    volumes: Optional[List[float]] = None,
    volume_range: Optional[List[float]] = None,
    speed: float = 50.0,
    source_height: float = 0.0,
    destination_height: float = 0.0,
) -> None:
    """Transfer from *source* to each well along a row or column.

    Provide exactly one of *volumes* (explicit list) or *volume_range*
    ([min, max] linearly spaced across the axis).
    """
    _get_pipette(context)

    try:
        plate_obj = context.deck.resolve_labware(plate)
    except KeyError as exc:
        raise ProtocolExecutionError(str(exc)) from exc
    if not isinstance(plate_obj, WellPlate):
        raise ProtocolExecutionError(
            f"serial_transfer requires a WellPlate, but '{plate}' is "
            f"{type(plate_obj).__name__}."
        )

    well_ids = _wells_for_axis(plate_obj, axis)
    if not well_ids:
        raise ProtocolExecutionError(
            f"No wells found for axis '{axis}' on plate '{plate}'."
        )

    has_volumes = volumes is not None
    has_range = volume_range is not None
    if has_volumes == has_range:
        raise ProtocolExecutionError(
            "Provide exactly one of volumes or volume_range, not both or neither."
        )

    if has_range:
        volumes = _linspace(volume_range[0], volume_range[1], len(well_ids))

    if len(volumes) != len(well_ids):
        raise ProtocolExecutionError(
            f"volumes length ({len(volumes)}) does not match axis '{axis}' "
            f"well count ({len(well_ids)})."
        )

    previous_substep = context.active_substep
    try:
        for index, (well_id, vol) in enumerate(zip(well_ids, volumes)):
            destination = f"{plate}.{well_id}"
            context.active_substep = f"{index}:{destination}"
            transfer(context, source=source, destination=destination,
                     volume_ul=vol, speed=speed, source_height=source_height,
                     destination_height=destination_height)
    finally:
        context.active_substep = previous_substep


# -- Automatic selection + compound commands --------------------------------
#
# Everything below composes `transfer`/`mix` (never duplicates their
# preflight, splitting, or durable begin/complete journaling). Each compound
# command extends the `fluid_operation_key` substep scheme with a stable,
# documented suffix (see each command's docstring) so a crash mid-workflow
# resumes by skipping every already-applied substep -- exactly like
# `transfer`'s per-stroke substeps and `serial_transfer`'s per-well
# substeps, which this composes underneath.


@contextmanager
def _substep_scope(context: ProtocolContext, suffix: str) -> Iterator[None]:
    """Nest *suffix* under the current ``active_substep``, then restore it.

    Mirrors the manual save/restore ``transfer``/``serial_transfer`` already
    do around ``context.active_substep``, as a reusable context manager for
    the compound commands below.
    """
    previous = context.active_substep
    context.active_substep = f"{previous}:{suffix}" if previous else suffix
    started = time.monotonic()
    context.notify_step("step_started")
    try:
        yield
    except BaseException as exc:
        context.notify_step(
            "step_failed",
            duration_s=time.monotonic() - started,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    else:
        context.notify_step(
            "step_completed", duration_s=time.monotonic() - started,
        )
    finally:
        context.active_substep = previous


def _leg_already_applied(context: ProtocolContext) -> bool:
    """Return True when the single-stroke transfer for the current substep
    scope is already durably applied.

    ``transfer``'s own idempotent skip (``begin_fluid_transfer``) already
    makes replaying an applied operation a no-op -- but only *after*
    re-validating dead-volume/destination-overflow preflight against LIVE
    current container state (see ``transfer``'s docstring: "Safety
    preflight runs entirely before any motion or durable journaling"). A
    compound-command leg that intentionally drains a transient container
    back toward empty (``rinse_well``'s ``remove``) legitimately has less
    current volume on a full-workflow replay than a *fresh* call's
    preflight would require -- even though that specific operation was
    always going to be skipped. Checking applied status first, via the
    same durable journal ``transfer`` itself reads
    (``DataStore.get_fluid_snapshot``, a public read), lets a compound
    command skip the call -- and its preflight -- entirely for an
    already-committed leg.

    Only covers the common single-stroke case (the operation key with no
    ``:strokeN`` suffix). A multi-stroke leg (volume above the pipette
    model's max) replays through ``transfer``'s own internal per-stroke
    skip as before -- see this module's documented scope note on
    ``purge_pipette``/`rinse_well` for the residual gap this leaves for a
    multi-stroke leg on a self-draining transient container.
    """
    if context.data_store is None or context.fluid_state_id is None:
        return False
    key = context.fluid_operation_key("transfer")
    try:
        operations = context.data_store.get_fluid_snapshot(
            context.fluid_state_id,
        )["operations"]
    except Exception:
        return False
    return any(
        operation["operation_key"] == key and operation["status"] == "applied"
        for operation in operations
    )


def _transfer_or_skip(context: ProtocolContext, **kwargs: Any) -> None:
    """Call `transfer`, unless this exact leg is already durably applied.

    See `_leg_already_applied` for why compound commands need this
    pre-check rather than relying solely on `transfer`'s own idempotent
    skip.
    """
    if _leg_already_applied(context):
        context.notify_step("step_skipped", reason=_ALREADY_APPLIED)
        context.logger.info(
            "Skipping already-applied fluid operation %s",
            context.fluid_operation_key("transfer"),
        )
        return
    transfer(context, **kwargs)


def _current_volume_lookup(context: ProtocolContext):
    """Return a ``CurrentVolumeLookup`` backed by this context's DataStore."""
    def _lookup(target: Any) -> Optional[float]:
        try:
            snapshot = context.data_store.get_fluid_container(
                context.fluid_state_id,
                target.labware_key,
                target.location_id or "",
            )
        except Exception:
            return None
        return float(snapshot["current_volume_ul"])
    return _lookup


def _resolve_stock_source(
    context: ProtocolContext,
    *,
    source: Optional[str],
    solution: Optional[str],
    volume_ul: float,
    command_label: str,
) -> str:
    """Return a deck-target position naming the stock source to draw from.

    Exactly one of *source* (an explicit deck target) or *solution* (a
    canonical solution identity resolved via automatic
    ``role=stock`` selection, see ``_liquid_selection``) must be given.
    Automatic selection requires durable fluid tracking, since it needs a
    known current volume for every candidate.
    """
    if (source is None) == (solution is None):
        raise ProtocolExecutionError(
            f"{command_label} requires exactly one of `source` (explicit "
            "deck target) or `solution` (automatic stock selection)."
        )
    if source is not None:
        return source
    if not _tracked_fluid_state(context):
        raise ProtocolExecutionError(
            f"{command_label} automatic stock selection (`solution=`) "
            "requires durable fluid tracking (context.fluid_state_id)."
        )
    try:
        target = select_stock_container(
            context.deck, solution, volume_ul, _current_volume_lookup(context),
        )
    except LiquidSelectionError as exc:
        raise ProtocolExecutionError(
            f"{command_label} stock selection failed: {exc}"
        ) from exc
    position = target_position(target)
    # Recorded run artifact for the automatic choice: the resolved position
    # is logged here for operator-readable narration, and durably lands in
    # the fluid_operations journal row `transfer` (below) begins -- that
    # row's source_labware_key/source_location_id name exactly this
    # container, so the choice is part of the permanent operation record.
    context.logger.info(
        "%s automatic stock selection: solution=%r -> %s",
        command_label, solution, position,
    )
    return position


def _resolve_waste_target(
    context: ProtocolContext,
    *,
    waste: Optional[str],
    solution: Optional[str],
    volume_ul: float,
    command_label: str,
) -> str:
    """Return a deck-target position naming the waste container to fill.

    *waste* (explicit deck target) wins when given. Otherwise automatic
    ``role=waste`` selection runs, using *solution* only as an optional
    compatibility filter (``allowed_solutions``; accept-all when a
    candidate declares none). Automatic selection requires durable fluid
    tracking.
    """
    if waste is not None:
        return waste
    if not _tracked_fluid_state(context):
        raise ProtocolExecutionError(
            f"{command_label} automatic waste selection requires durable "
            "fluid tracking (context.fluid_state_id); pass `waste=` explicitly "
            "otherwise."
        )
    try:
        target = select_waste_container(
            context.deck, volume_ul, _current_volume_lookup(context),
            solution=solution,
        )
    except LiquidSelectionError as exc:
        raise ProtocolExecutionError(
            f"{command_label} waste selection failed: {exc}"
        ) from exc
    position = target_position(target)
    context.logger.info(
        "%s automatic waste selection: solution=%r -> %s",
        command_label, solution, position,
    )
    return position


@protocol_command("rinse_well", summary=_summaries.rinse_well)
def rinse_well(
    context: ProtocolContext,
    well: str,
    volume_ul: float,
    cycles: int = 3,
    source: Optional[str] = None,
    solution: Optional[str] = None,
    waste: Optional[str] = None,
    mix_repetitions: int = 0,
    mix_volume_ul: Optional[float] = None,
    speed: float = 50.0,
    source_height: Optional[float] = None,
    well_height: Optional[float] = None,
    waste_height: Optional[float] = None,
    require_uncapped: Optional[List[str]] = None,
) -> None:
    """Rinse *well* with a stock solution, ``cycles`` times.

    Each cycle: fill *well* from the stock source with *volume_ul*,
    optionally mix in place, then remove the same *volume_ul* to waste.
    Composes entirely from `transfer`/`mix` -- see this module's docstring
    for why that means every cycle inherits their preflight and durable
    recovery for free.

    The stock source is either *source* (explicit deck target) or
    *solution* (automatic ``role=stock`` selection) -- exactly one must be
    given. *waste* is optional; when omitted, automatic ``role=waste``
    selection runs (using *solution* as its compatibility filter).

    Substep keys (extends `fluid_operation_key`, 0-indexed cycles):
    ``rinse:cycle{N}:fill``, ``rinse:cycle{N}:mix`` (only when
    ``mix_repetitions > 0``), ``rinse:cycle{N}:remove``. A crash mid-rinse
    resumes by skipping every already-applied substep and cycle.
    """
    _require_uncapped(context, require_uncapped, command_label="rinse_well")
    if not isinstance(cycles, int) or isinstance(cycles, bool) or cycles <= 0:
        raise ProtocolExecutionError(
            f"rinse_well cycles must be a positive integer, got {cycles!r}."
        )
    for cycle in range(cycles):
        cycle_scope = f"rinse:cycle{cycle}"
        resolved_source = _resolve_stock_source(
            context, source=source, solution=solution, volume_ul=volume_ul,
            command_label="rinse_well",
        )
        with _substep_scope(context, f"{cycle_scope}:fill"):
            _transfer_or_skip(
                context, source=resolved_source, destination=well,
                volume_ul=volume_ul, speed=speed,
                source_height=source_height, destination_height=well_height,
            )
        if mix_repetitions > 0:
            with _substep_scope(context, f"{cycle_scope}:mix"):
                mix(
                    context, well, mix_volume_ul or volume_ul,
                    cycles=mix_repetitions, speed=speed,
                    height=well_height or 0.0,
                )
        resolved_waste = _resolve_waste_target(
            context, waste=waste, solution=solution, volume_ul=volume_ul,
            command_label="rinse_well",
        )
        with _substep_scope(context, f"{cycle_scope}:remove"):
            _transfer_or_skip(
                context, source=well, destination=resolved_waste,
                volume_ul=volume_ul, speed=speed,
                source_height=well_height, destination_height=waste_height,
            )


@protocol_command("flush_pipette", summary=_summaries.flush_pipette)
def flush_pipette(
    context: ProtocolContext,
    volume_ul: float,
    cycles: int = 1,
    source: Optional[str] = None,
    solution: Optional[str] = None,
    waste: Optional[str] = None,
    speed: float = 50.0,
    source_height: Optional[float] = None,
    waste_height: Optional[float] = None,
    require_uncapped: Optional[List[str]] = None,
) -> None:
    """Flush the pipette by drawing from stock and dispensing to waste, xN.

    Each cycle is exactly one `transfer` from the resolved stock source to
    the resolved waste container. Container resolution follows
    `rinse_well`'s rules: exactly one of *source*/*solution* for the stock
    side, *waste* optional (else automatic ``role=waste`` selection).

    Substep keys: ``flush:cycle{N}`` (0-indexed).
    """
    _require_uncapped(context, require_uncapped, command_label="flush_pipette")
    if not isinstance(cycles, int) or isinstance(cycles, bool) or cycles <= 0:
        raise ProtocolExecutionError(
            f"flush_pipette cycles must be a positive integer, got {cycles!r}."
        )
    for cycle in range(cycles):
        resolved_source = _resolve_stock_source(
            context, source=source, solution=solution, volume_ul=volume_ul,
            command_label="flush_pipette",
        )
        resolved_waste = _resolve_waste_target(
            context, waste=waste, solution=solution, volume_ul=volume_ul,
            command_label="flush_pipette",
        )
        with _substep_scope(context, f"flush:cycle{cycle}"):
            _transfer_or_skip(
                context, source=resolved_source, destination=resolved_waste,
                volume_ul=volume_ul, speed=speed,
                source_height=source_height, destination_height=waste_height,
            )


@protocol_command("purge_pipette", summary=_summaries.purge_pipette)
def purge_pipette(
    context: ProtocolContext,
    volume_ul: float,
    source: Optional[str] = None,
    solution: Optional[str] = None,
    waste: Optional[str] = None,
    speed: float = 50.0,
    source_height: Optional[float] = None,
    waste_height: Optional[float] = None,
    require_uncapped: Optional[List[str]] = None,
) -> None:
    """Empty the pipette's currently-loaded volume into waste.

    CubOS's durable fluid model has no volume tracked independently "inside
    the tip": `transfer` moves liquid atomically from a known source
    container to a known destination in one journaled operation (see
    `_liquid_transfer`/`fluid_state.begin_fluid_transfer`), so there is
    nothing held outside of an in-flight transfer to query. `purge_pipette`
    is accordingly a single ``source -> waste`` transfer -- *source* must
    name (explicitly, or via *solution*'s automatic stock selection) the
    container the currently-loaded liquid is attributed to. This is a
    deliberate, documented scope decision (see docs/protocol-yaml.md and
    this module's docstring), not an oversight: it is the same primitive
    `flush_pipette` uses per cycle, kept as a distinct single-action command
    for protocol-readability ("empty what's in the tip right now" vs.
    "clean the tip with N cycles of fresh solvent").

    Substep key: ``purge``.
    """
    _require_uncapped(context, require_uncapped, command_label="purge_pipette")
    resolved_source = _resolve_stock_source(
        context, source=source, solution=solution, volume_ul=volume_ul,
        command_label="purge_pipette",
    )
    resolved_waste = _resolve_waste_target(
        context, waste=waste, solution=solution, volume_ul=volume_ul,
        command_label="purge_pipette",
    )
    with _substep_scope(context, "purge"):
        _transfer_or_skip(
            context, source=resolved_source, destination=resolved_waste,
            volume_ul=volume_ul, speed=speed,
            source_height=source_height, destination_height=waste_height,
        )


@protocol_command("clear_well", summary=_summaries.clear_well)
def clear_well(
    context: ProtocolContext,
    well: str,
    target_volume_ul: float = 0.0,
    volume_ul: Optional[float] = None,
    waste: Optional[str] = None,
    solution: Optional[str] = None,
    speed: float = 50.0,
    well_height: Optional[float] = None,
    waste_height: Optional[float] = None,
    require_uncapped: Optional[List[str]] = None,
) -> None:
    """Remove *well*'s contents to waste until empty (or *target_volume_ul*).

    *volume_ul* overrides the amount removed explicitly (matching
    `serial_transfer`'s explicit-override convention). Otherwise the amount
    removed is ``current_volume_ul - target_volume_ul`` read from durable
    fluid state, which requires tracking to be active (there is no other
    source of "current volume" to drain down from). *waste* is optional;
    when omitted, automatic ``role=waste`` selection runs (*solution*, if
    given, filters by compatibility).

    A no-op (no substep, no motion) when the computed removal volume is at
    or below zero. Substep key: ``clear``.
    """
    _require_uncapped(context, require_uncapped, command_label="clear_well")
    resolved_volume_ul = volume_ul
    if resolved_volume_ul is None:
        if not _tracked_fluid_state(context):
            raise ProtocolExecutionError(
                "clear_well requires an explicit `volume_ul` when durable "
                "fluid tracking is inactive (no current volume to drain from)."
            )
        well_target = _resolve_fluid_target(context, well)
        try:
            current = context.data_store.get_fluid_container(
                context.fluid_state_id,
                well_target.labware_key,
                well_target.location_id or "",
            )["current_volume_ul"]
        except Exception as exc:
            raise ProtocolExecutionError(
                f"clear_well failed to read current volume for {well!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        resolved_volume_ul = float(current) - float(target_volume_ul)

    if resolved_volume_ul <= 1e-9:
        context.logger.info(
            "clear_well %s: already at or below target_volume_ul=%g; skipping.",
            well, target_volume_ul,
        )
        return

    resolved_waste = _resolve_waste_target(
        context, waste=waste, solution=solution, volume_ul=resolved_volume_ul,
        command_label="clear_well",
    )
    with _substep_scope(context, "clear"):
        _transfer_or_skip(
            context, source=well, destination=resolved_waste,
            volume_ul=resolved_volume_ul, speed=speed,
            source_height=well_height, destination_height=waste_height,
        )
