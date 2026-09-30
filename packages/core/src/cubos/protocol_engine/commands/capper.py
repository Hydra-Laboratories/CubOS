"""Protocol commands for generic vial capper/decapper operations.

``decap``/``cap`` are built entirely from generic gantry primitives
(``InstrumentedGantry.move_to_labware``/``.move``, exactly like every other
instrument command) plus the vendor-agnostic
``cubos.instruments.capper.interface.CapperInstrument`` surface. No
vendor-specific behavior (Arduino command IDs, electromagnet, line-break
sensor parsing) appears here -- see
``cubos.instruments.capper.vendors.pawduino`` for that.

Sequencing is fixed and explicit for every decap/cap action:

1. **approach** -- travel to the vial XY at the gantry's absolute ``safe_z``
   (``InstrumentedGantry.move_to_labware``).
2. **engage** -- descend to ``vial.z + capper.engage_depth_mm`` (a
   labware-relative offset carried on the *instrument config*, not
   hardcoded here -- see ``CapperInstrument.engage_depth_mm``).
3. **actuate** -- ``capture_cap``/``release_cap`` at engage depth.
4. **retract + confirm** -- ascend back to ``safe_z``, THEN sensor-confirm
   (``read_cap_present``) the expected state, retrying (re-engage, actuate,
   retract, read) up to ``capper.capture_retries`` times. The confirm must
   happen after the retract: at engage depth the line-break beam is broken
   by the cap whether it is held by the tool or sitting on the vial, so the
   reading is meaningless until the tool has lifted clear.

The tool is left at ``safe_z`` above the vial. There is no lateral park
leg: the next command's ``move_to_labware`` lifts to the multi-tool ceiling
before any XY travel, and every raw XY move does the same.

A timeout or a sensor reading that contradicts the expected state after all
retries FAILS CLOSED: the tool is retracted to ``safe_z`` on a best-effort
basis, the durable cap state (when tracking is active) is marked
``reconciliation_required``, and a ``ProtocolExecutionError`` is raised --
no cap-state transition is recorded as applied. The same safe-retract path
runs for a mid-motion gantry failure at any stage, not just a sensor fault.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from cubos.instruments.capper.exceptions import CapperError
from cubos.instruments.capper.interface import CapperInstrument

from ..errors import ProtocolExecutionError
from ..registry import protocol_command
from . import _summaries
from ._movement import unpack_xyz

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..runtime import ProtocolContext


def _get_capper(context: "ProtocolContext", instrument: str) -> CapperInstrument:
    try:
        capper = context.gantry.instruments[instrument]
    except KeyError as exc:
        raise ProtocolExecutionError(
            f"No instrument {instrument!r} registered on the gantry."
        ) from exc
    if not isinstance(capper, CapperInstrument):
        raise ProtocolExecutionError(
            f"Instrument {instrument!r} is a {type(capper).__name__}, not a "
            "CapperInstrument. decap/cap require a `capper` type instrument."
        )
    return capper


def _tracked_fluid_state(context: "ProtocolContext") -> bool:
    """Return whether durable tracking is active, rejecting partial context."""
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


def _resolve_vial_target(context: "ProtocolContext", vial: str) -> tuple[str, str]:
    from cubos.data.cap_state import CapStateError, resolve_cap_target

    try:
        return resolve_cap_target(context.deck, vial)
    except CapStateError as exc:
        raise ProtocolExecutionError(str(exc)) from exc


def _mark_cap_uncertain(
    context: "ProtocolContext",
    operation_key: str,
    exc: BaseException,
) -> None:
    """Best-effort marker for physical cap actions whose outcome needs review."""
    try:
        context.data_store.mark_cap_reconciliation_required(
            operation_key,
            f"{type(exc).__name__}: {exc}",
        )
    except Exception:
        logger.exception(
            "Failed to mark cap operation %s reconciliation-required",
            operation_key,
        )


def _actuate_retract_confirm(
    context: "ProtocolContext",
    capper: CapperInstrument,
    instrument: str,
    *,
    x: float,
    y: float,
    engage_z: float,
    capturing: bool,
    command_label: str,
) -> None:
    """Engage, actuate, retract, then sensor-confirm; re-engage per retry.

    The line-break sensor sits at the tool head and reads "beam broken"
    whenever a cap is inside it -- held by the electromagnet, or simply
    sitting on the vial the tool is still lowered over. At engage depth the
    reading is therefore identical whether the actuation worked or not; only
    after retracting to ``safe_z`` does it distinguish a held cap (decap
    succeeded / cap failed) from no cap (cap succeeded / decap failed). The
    source firmware's decapping/capping sequences confirmed after retract
    for the same reason (see the pawduino vendor docstring).

    Fails closed (raises ``CapperError``) if the sensor never confirms the
    expected post-retract state -- covers both an outright timeout/command
    error from the vendor driver and a reading that contradicts what was
    just commanded. Gantry motion errors propagate to the caller unchanged.
    """
    expected = capturing
    verb = "capture" if capturing else "release"
    attempts = capper.capture_retries + 1
    last_reading: Any = None
    last_error: BaseException | None = None
    for _attempt in range(attempts):
        context.gantry.move(instrument, (x, y, engage_z))  # (re-)engage
        try:
            if capturing:
                capper.capture_cap()
            else:
                capper.release_cap()
            if capper.capture_settle_s > 0:
                time.sleep(capper.capture_settle_s)
        except CapperError as exc:
            last_reading = None
            last_error = exc
            continue
        context.gantry.move(instrument, (x, y, context.gantry.safe_z))  # retract
        try:
            last_reading = capper.read_cap_present()
            last_error = None
        except CapperError as exc:
            last_reading = None
            last_error = exc
            continue
        if last_reading == expected:
            return

    if last_error is not None:
        raise CapperError(
            f"{command_label}: sensor failed to confirm cap {verb} after "
            f"{attempts} attempt(s): {type(last_error).__name__}: {last_error}."
        ) from last_error
    raise CapperError(
        f"{command_label}: sensor did not confirm cap {verb} after "
        f"{attempts} attempt(s) (last reading: cap_present={last_reading!r}, "
        f"expected {expected!r})."
    )


def _run_capper_sequence(
    context: "ProtocolContext",
    instrument: str,
    vial: str,
    *,
    capturing: bool,
    command_label: str,
) -> None:
    capper = _get_capper(context, instrument)
    try:
        coord = context.deck.resolve_coordinate(vial)
    except (KeyError, AttributeError, ValueError) as exc:
        raise ProtocolExecutionError(
            f"{command_label}: cannot resolve vial {vial!r} on the deck: {exc}"
        ) from exc
    x, y, vial_z = unpack_xyz(coord)

    tracked = _tracked_fluid_state(context)
    operation_key = None
    if tracked:
        labware_key, location_id = _resolve_vial_target(context, vial)
        operation_key = context.fluid_operation_key(command_label)
        try:
            should_execute = context.data_store.begin_cap_operation(
                context.fluid_state_id,
                operation_key,
                command_label,
                labware_key,
                location_id,
                campaign_id=context.campaign_id,
            )
        except Exception as exc:
            raise ProtocolExecutionError(
                f"Cap-state preflight failed for {command_label} at {vial!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not should_execute:
            context.logger.info(
                "Skipping already-applied cap operation %s", operation_key,
            )
            return

    # Steps 1-4: approach, engage, actuate, retract; sensor-confirm after
    # the retract (re-engaging per retry) -- see _actuate_retract_confirm.
    try:
        context.gantry.move_to_labware(instrument, coord)  # approach
        engage_z = vial_z + capper.engage_depth_mm
        _actuate_retract_confirm(
            context, capper, instrument,
            x=x, y=y, engage_z=engage_z,
            capturing=capturing, command_label=command_label,
        )
    except BaseException as exc:
        _safe_retract(context, instrument, x, y)
        if operation_key is not None:
            _mark_cap_uncertain(context, operation_key, exc)
        raise ProtocolExecutionError(
            f"{command_label} failed for {vial!r}: {type(exc).__name__}: {exc}. "
            "Tool retracted to safe_z; reconciliation is required before "
            "further liquid handling involving this vial."
        ) from exc

    if operation_key is not None:
        try:
            context.data_store.complete_cap_operation(operation_key)
        except Exception as exc:
            _mark_cap_uncertain(context, operation_key, exc)
            raise ProtocolExecutionError(
                f"Physical {command_label} completed, but its cap-state "
                f"commit failed for operation {operation_key!r}: "
                f"{type(exc).__name__}: {exc}. Reconciliation is required."
            ) from exc


def _safe_retract(context: "ProtocolContext", instrument: str, x: float, y: float) -> None:
    """Best-effort retract to safe_z after a failure at any sequence stage.

    Never raises: a retract failure is logged, not propagated, so it cannot
    mask the original error that triggered it.
    """
    try:
        context.gantry.move(instrument, (x, y, context.gantry.safe_z))
    except Exception:
        logger.exception(
            "Safe retract failed for instrument %r after a decap/cap error; "
            "physical position is uncertain.",
            instrument,
        )


@protocol_command("decap", summary=_summaries.decap)
def decap(context: "ProtocolContext", instrument: str, vial: str) -> None:
    """Remove the cap from *vial* using a capper instrument.

    Approaches at ``safe_z``, engages at the instrument's configured
    ``engage_depth_mm``, captures the cap (sensor-confirmed, with retries),
    and retracts to ``safe_z``. When durable fluid/cap tracking is active
    (``context.fluid_state_id``), the vial must currently be tracked
    ``capped``; the operation is journaled with the same two-phase
    begin/complete pattern as tip pickups, and marked
    ``reconciliation_required`` (blocking further liquid handling on this
    vial) if the physical outcome is uncertain.
    """
    _run_capper_sequence(context, instrument, vial, capturing=True, command_label="decap")


@protocol_command("cap", summary=_summaries.cap)
def cap(context: "ProtocolContext", instrument: str, vial: str) -> None:
    """Replace the cap on *vial* using a capper instrument.

    Mirrors ``decap``: approach, engage, release (sensor-confirmed cap no
    longer held), retract. When durable fluid/cap tracking is active,
    the vial must currently be tracked ``uncapped``.
    """
    _run_capper_sequence(context, instrument, vial, capturing=False, command_label="cap")
