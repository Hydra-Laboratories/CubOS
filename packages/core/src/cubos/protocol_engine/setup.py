"""Protocol setup: load all configs, validate, and return a ready-to-run protocol."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import os
from pathlib import Path
from typing import Any, List, Tuple

logger = logging.getLogger(__name__)

import yaml

from cubos.deck.deck import Deck
from cubos.deck.loader import load_deck_from_yaml_safe
from cubos.gantry.errors import GantryLoaderError
from cubos.gantry.gantry import Gantry
from cubos.gantry.gantry_config import GantryConfig
from cubos.gantry.instrument_loader import load_instrumented_gantry_from_config
from cubos.gantry.instrument_mount import InstrumentedGantry
from cubos.gantry.loader import load_gantry_from_yaml_safe
from cubos.gantry.origin import validate_working_volume_origin
from cubos.protocol_engine.errors import GantryHealthCheckError
from cubos.protocol_engine.loader import load_protocol_from_yaml_safe
from cubos.protocol_engine.protocol import Protocol
from cubos.protocol_engine.runtime import ProtocolContext
from cubos.validation.bounds import validate_protocol_motion_bounds
from cubos.validation.errors import ProtocolSemanticValidationError, SetupValidationError
from cubos.validation.protocol_semantics import validate_protocol_semantics

ProtocolInput = str | os.PathLike[str] | Protocol


def _validate_protocol_input(protocol_input: ProtocolInput) -> Protocol | None:
    if isinstance(protocol_input, Protocol):
        return protocol_input
    if isinstance(protocol_input, (str, os.PathLike)):
        return None
    raise TypeError(
        "protocol must be a YAML path or protocol_engine.Protocol, "
        f"got {type(protocol_input).__name__}."
    )


def setup_protocol(
    gantry_path: str | Path,
    deck_path: str | Path,
    protocol_path: ProtocolInput,
    gantry: Any | None = None,
    mock_mode: bool = False,
    data_store: Any | None = None,
    campaign_id: int | None = None,
    fluid_state_id: int | None = None,
    step_observer: Any | None = None,
) -> Tuple[Protocol, ProtocolContext]:
    """Load all configs, validate bounds, and return a ready-to-run protocol.

    Steps:
        1. Load gantry config (working volume and instruments)
        2. Load deck (labware positions)
        3. Build instrumented gantry (mounted instruments with offsets)
        4. Load protocol (command steps)
        5. Validate protocol motion targets within gantry bounds
        6. Validate protocol semantics
        7. Return (Protocol, ProtocolContext)

    Args:
        gantry_path: Path to gantry machine YAML config.
        deck_path: Path to deck YAML config.
        protocol_path: Path to protocol YAML config or a prebuilt Protocol.
        gantry: Optional Gantry instance. If None, an offline Gantry is used
            for validation.
        mock_mode: If True, instantiate real driver classes in offline mode.
        data_store: Optional persistence store attached to the runtime context.
        campaign_id: Optional campaign ID used with ``data_store``.
        fluid_state_id: Optional durable deck-bound fluid-state session ID.
        step_observer: Optional :class:`~cubos.protocol_engine.observer.
            StepObserver` notified as each step and substep runs. Advisory
            only -- observer errors are swallowed and never affect the run.

    Returns:
        Tuple of (Protocol, ProtocolContext) ready for ``protocol.execute(context)``.

    Raises:
        GantryLoaderError: If gantry YAML is invalid or missing.
        DeckLoaderError: If deck YAML is invalid or missing.
        GantryLoaderError: If embedded instruments are invalid or missing.
        ProtocolLoaderError: If protocol YAML is invalid or missing.
        TypeError: If protocol_path is neither a path nor a Protocol.
        SetupValidationError: If any positions violate gantry bounds.
    """
    protocol = _validate_protocol_input(protocol_path)

    gantry_config: GantryConfig = load_gantry_from_yaml_safe(gantry_path)
    validate_working_volume_origin(gantry_config)
    deck: Deck = load_deck_from_yaml_safe(
        deck_path,
        factory_z_travel_mm=gantry_config.factory_z_travel_mm,
    )

    if gantry is None:
        gantry = Gantry(offline=True)
    try:
        instrumented_gantry: InstrumentedGantry = load_instrumented_gantry_from_config(
            gantry_config, gantry, mock_mode=mock_mode,
        )
    except Exception as exc:
        raise GantryLoaderError(
            f"Machine config error in `{gantry_path}`: {exc}\n"
            "How to fix: Add valid mounted instruments under the "
            "gantry YAML top-level 'instruments' key."
        ) from exc

    if hasattr(gantry, "set_expected_grbl_settings"):
        gantry.set_expected_grbl_settings(
            instrumented_gantry.expected_grbl_settings,
            source=str(gantry_path),
        )

    if protocol is None:
        protocol = load_protocol_from_yaml_safe(protocol_path)

    violations = validate_protocol_motion_bounds(
        gantry_config, protocol, deck, instrumented_gantry,
    )
    if violations:
        raise SetupValidationError(violations)

    semantic_violations = validate_protocol_semantics(
        protocol, instrumented_gantry, deck, gantry_config,
    )
    if semantic_violations:
        raise ProtocolSemanticValidationError(semantic_violations)

    context = ProtocolContext(
        gantry=instrumented_gantry,
        deck=deck,
        positions=protocol.positions,
        gantry_config=gantry_config,
        data_store=data_store,
        campaign_id=campaign_id,
        fluid_state_id=fluid_state_id,
        step_observer=step_observer,
    )
    return protocol, context


def run_on_hardware(
    gantry_path: str | Path,
    deck_path: str | Path,
    protocol_path: ProtocolInput,
    gantry: Any | None = None,
    mock_mode: bool = False,
    data_store: Any | None = None,
    campaign_id: int | None = None,
    campaign_description: str | None = None,
    protocol_config: str | None = None,
    fluid_state_id: int | None = None,
    initial_fluids: str | os.PathLike[str] | Mapping[str, Any] | None = None,
) -> List[Any]:
    """Load configs, validate, and run the protocol as a full hardware session.

    This is the single orchestration path shared by the YAML CLI
    (``packages/core/src/cubos/tools/run_protocol.py``) and ``Protocol.run()``. Measurement-producing
    commands are persisted by default: when no ``data_store``/``campaign_id`` is
    supplied, this function creates a default ``DataStore`` campaign for the
    run and attaches it to the runtime context. The lifecycle is:

        1. construct a ``Gantry`` from the gantry YAML (when ``gantry`` is None)
        2. ``setup_protocol`` — load/validate configs, build the context
        3. create or verify the optional deck-bound fluid state
        4. create a protocol-run campaign if one was not supplied, and attach
           the optional fluid state to it
        5. ``gantry.connect()``
        6. ``gantry.prepare_for_protocol_run()`` — clear any startup alarm
        7. ``connect_instruments()``
        8. ``gantry.is_healthy()`` health check (abort if unhealthy)
        9. ``protocol.execute(context)`` — run the steps
        10. disconnect instruments and gantry in ``finally``, even on error

    Args:
        gantry_path: Path to gantry machine YAML config.
        deck_path: Path to deck YAML config.
        protocol_path: Path to protocol YAML config or a prebuilt Protocol.
        gantry: Optional Gantry instance. If None, one is constructed from
            ``gantry_path``. Tests pass an offline Gantry to stay off hardware.
        mock_mode: If True, instantiate real driver classes in offline mode.
        data_store: Optional persistence store attached to the runtime context.
        campaign_id: Optional campaign ID used with ``data_store``. If omitted,
            a campaign is created automatically.
        campaign_description: Optional campaign description for auto-created
            campaigns.
        protocol_config: Optional identifier recorded for in-memory protocol
            objects; YAML path inputs use their own path by default.
        fluid_state_id: Existing deck-bound fluid state to verify and resume.
            Its saved deck fingerprint must match ``deck_path``, and it must
            not have an operation awaiting physical reconciliation.
        initial_fluids: Initial-fluid mapping or seed YAML path used to create
            a new deck-bound fluid state. Mutually exclusive with
            ``fluid_state_id``.

    Returns:
        List of step results from protocol execution.

    Raises:
        GantryHealthCheckError: If the gantry health check fails.
    """
    if fluid_state_id is not None and initial_fluids is not None:
        raise ValueError(
            "fluid_state_id and initial_fluids are mutually exclusive; resume "
            "an existing state or create a new one, not both."
        )

    if gantry is None:
        if mock_mode:
            gantry = Gantry(offline=True)
        else:
            with open(gantry_path) as f:
                raw_config = yaml.safe_load(f)
            gantry = Gantry(config=raw_config)

    owns_data_store = False
    if data_store is None:
        from cubos.data import DataStore

        data_store = DataStore()
        owns_data_store = True

    context = None
    run_failed = False
    try:
        if campaign_id is not None:
            linked_fluid_state_id = data_store.get_campaign_fluid_state_id(
                campaign_id,
            )
            if linked_fluid_state_id is not None:
                if initial_fluids is not None:
                    raise ValueError(
                        f"Campaign {campaign_id} is already attached to fluid "
                        f"state {linked_fluid_state_id}; initial_fluids cannot "
                        "be used to replace it. Resume the linked state instead."
                    )
                if (
                    fluid_state_id is not None
                    and fluid_state_id != linked_fluid_state_id
                ):
                    raise ValueError(
                        f"Campaign {campaign_id} is already attached to fluid "
                        f"state {linked_fluid_state_id}, not {fluid_state_id}."
                    )
                fluid_state_id = linked_fluid_state_id

        protocol, context = setup_protocol(
            gantry_path, deck_path, protocol_path,
            gantry=gantry, mock_mode=mock_mode,
            data_store=data_store, campaign_id=campaign_id,
            fluid_state_id=fluid_state_id,
        )
        if fluid_state_id is not None:
            fluid_state_id = data_store.resume_fluid_state(
                fluid_state_id,
                deck_path,
                context.deck,
            )
            pipette = context.gantry.instruments.get("pipette")
            if pipette is not None:
                # Restores the attached-tip extension onto the pipette
                # instrument, or raises if attachment is uncertain -- resume
                # already refuses when a tip operation needs reconciliation,
                # so this is primarily a same-session consistency guarantee.
                data_store.restore_pipette_attachment(fluid_state_id, pipette)
        elif initial_fluids is not None:
            from cubos.data import load_initial_fluids

            seed = (
                _initial_fluid_mapping(initial_fluids)
                if isinstance(initial_fluids, Mapping)
                else load_initial_fluids(initial_fluids)
            )
            fluid_state_id = data_store.create_fluid_state(
                deck_path,
                context.deck,
                initial_fluids=seed,
            )

        if campaign_id is None:
            from cubos.data import register_deck_labware

            protocol_file = _protocol_config_label(protocol_path, protocol_config)
            campaign_id = data_store.create_campaign(
                description=campaign_description or (
                    f"Protocol run: gantry={gantry_path}, deck={deck_path}, "
                    f"protocol={protocol_file}"
                ),
                deck_config=str(deck_path),
                gantry_config=str(gantry_path),
                protocol_config=protocol_file,
                fluid_state_id=fluid_state_id,
            )
            register_deck_labware(data_store, campaign_id, context.deck)
        elif fluid_state_id is not None:
            data_store.attach_campaign_fluid_state(
                campaign_id,
                fluid_state_id,
            )
        context.data_store = data_store
        context.campaign_id = campaign_id
        context.fluid_state_id = fluid_state_id
        gantry.connect()
        gantry.prepare_for_protocol_run()
        context.gantry.connect_instruments()
        if not gantry.is_healthy():
            raise GantryHealthCheckError(
                "Gantry health check failed before protocol execution; aborting."
            )
        return protocol.execute(context)
    except BaseException:
        run_failed = True
        logger.exception(
            "Protocol run failed; last commanded pose: %s",
            _last_commanded_pose(context),
        )
        raise
    finally:
        try:
            if context is not None:
                if run_failed:
                    _best_effort_retract_to_safe_z(context)
                try:
                    context.gantry.disconnect_instruments()
                except Exception as exc:
                    logger.error(
                        "disconnect_instruments failed: %s", exc, exc_info=True,
                    )
                try:
                    gantry.disconnect()
                except Exception as exc:
                    logger.error("cubos.gantry.disconnect failed: %s", exc, exc_info=True)
        finally:
            if data_store is not None and campaign_id is not None:
                try:
                    data_store.finish_campaign(
                        campaign_id,
                        "failed" if run_failed else "completed",
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to mark campaign %s as finished: %s",
                        campaign_id,
                        exc,
                        exc_info=True,
                    )
            if owns_data_store:
                try:
                    data_store.close()
                except Exception as exc:
                    logger.error("data_store.close failed: %s", exc, exc_info=True)


def _last_commanded_pose(context: Any | None) -> Any:
    if context is None:
        return None
    return getattr(context.gantry, "last_commanded_pose", None)


def _best_effort_retract_to_safe_z(context: ProtocolContext) -> None:
    pose = _last_commanded_pose(context)
    safe_z = getattr(context.gantry, "safe_z", None)
    if not pose or safe_z is None:
        logger.warning(
            "Skipping failure retract: no last commanded pose or safe_z available."
        )
        return
    try:
        instrument = pose["instrument"]
        x, y, _z = pose["instrument_position"]
        logger.error(
            "Protocol failed; retracting %s at current XY to safe_z %.3f.",
            instrument, safe_z,
        )
        context.gantry.move(instrument, (x, y, safe_z), travel_z=safe_z)
    except Exception as exc:
        logger.error(
            "Failure retract to safe_z failed; manual hardware check required: %s",
            exc,
            exc_info=True,
        )


def _protocol_config_label(
    protocol_input: ProtocolInput,
    explicit: str | None,
) -> str:
    if explicit is not None:
        return explicit
    if isinstance(protocol_input, (str, os.PathLike)):
        return str(protocol_input)
    source_path = getattr(protocol_input, "source_path", None)
    if source_path is not None:
        return str(source_path)
    return "<in-memory protocol>"


def _initial_fluid_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Accept either direct targets or the on-disk ``fluids`` wrapper."""
    seed = dict(value)
    if "fluids" not in seed:
        return seed
    if set(seed) != {"fluids"}:
        raise ValueError(
            "A YAML-shaped initial_fluids mapping must contain exactly one "
            "top-level `fluids` key. Pass direct target entries without the "
            "wrapper, or use {'fluids': {...}}."
        )
    wrapped = seed["fluids"]
    if not isinstance(wrapped, Mapping):
        raise TypeError(
            "The top-level `fluids` value in initial_fluids must be a mapping."
        )
    return dict(wrapped)
