"""Single-owner asynchronous execution for versioned CubOS runs."""

from __future__ import annotations

import logging
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from cubos.data import DataStore
from cubos.deck import load_deck_from_yaml
from cubos.deck.errors import DeckLoaderError
from cubos.protocol_engine.loader import load_protocol_from_yaml
from cubos.validation.fluid_volumes import validate_protocol_fluid_volumes

from cubos_api.config import CubOSSettings, get_settings
from cubos_api.models.runs import RunRecord, RunSubmission
from cubos_api.models.state import RunStateSelection
from cubos_api.services.run_store import RunStore, sha256_text
from cubos_api.services.step_observer import RunStoreStepObserver
from cubos_api.services.yaml_io import resolve_config_path
from cubos_api.services.contents_ownership import get_contents_ownership


log = logging.getLogger(__name__)


class RunConflictError(RuntimeError):
    pass


class RunPolicyError(ValueError):
    pass


_FLUID_TARGET_FIELDS: dict[str, tuple[str, ...]] = {
    "transfer": ("source", "destination"),
    "serial_transfer": ("source", "plate"),
    "mix": ("position",),
    "rinse_well": ("well", "source", "waste"),
    "flush_pipette": ("source", "waste"),
    "purge_pipette": ("source", "waste"),
    "clear_well": ("well", "waste"),
}

_DYNAMIC_LIQUID_COMMANDS = {
    "rinse_well",
    "flush_pipette",
    "purge_pipette",
    "clear_well",
}


def _fluid_targets(protocol: Any, deck: Any) -> list[tuple[str, Any]]:
    """Return explicit liquid targets, resolving aliases through the deck."""
    targets: list[tuple[str, Any]] = []
    for step in protocol.steps:
        if step.command_name in _DYNAMIC_LIQUID_COMMANDS:
            args = step.args
            if args.get("solution") and not args.get("source"):
                raise RunPolicyError(
                    f"fluid-state preflight blocked: {step.command_name} uses "
                    "dynamic stock selection (`solution`); provide an explicit "
                    "known `source` before running."
                )
            if not args.get("waste"):
                raise RunPolicyError(
                    f"fluid-state preflight blocked: {step.command_name} uses "
                    "dynamic waste selection; provide an explicit known `waste` "
                    "before running."
                )
        fields = _FLUID_TARGET_FIELDS.get(step.command_name, ())
        for field in fields:
            value = step.args.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            if step.command_name == "serial_transfer" and field == "plate":
                try:
                    plate = deck.resolve_labware(value)
                    well_ids = list(getattr(plate, "wells", ()))
                    axis = step.args.get("axis")
                    if isinstance(axis, str):
                        if axis.isalpha():
                            well_ids = [well for well in well_ids if well[0] == axis.upper()]
                        else:
                            well_ids = [well for well in well_ids if well[1:] == axis]
                    for well_id in well_ids:
                        target_name = f"{value}.{well_id}"
                        targets.append((target_name, deck.resolve_labware_target(target_name)))
                except (KeyError, ValueError):
                    pass
                continue
            # `solution`/`waste` selectors are intentionally not inferred:
            # runtime selection may choose any suitable registered container.
            try:
                target = deck.resolve_labware_target(value)
            except (KeyError, ValueError):
                continue
            targets.append((value, target))
    return targets


def _validate_fluid_state_preflight(
    *, state_id: int, deck: Any, protocol_yaml: str, db_path: Path
) -> None:
    """Validate active contents before a run can reach hardware setup.

    Unknownness is checked only for explicit liquid targets in the protocol;
    an unrelated unknown well is allowed to remain unresolved.  Known values
    then flow through CubOS's existing static volume simulator for shortage,
    dead-volume, and destination-overflow checks.
    """
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as protocol_file:
            protocol_file.write(protocol_yaml)
            protocol_path = Path(protocol_file.name)
        protocol = load_protocol_from_yaml(protocol_path)
        protocol_path.unlink(missing_ok=True)
        # Resolve command semantics before opening the state database so an
        # unsupported dynamic liquid selector fails deterministically even if
        # the caller's state id is stale.
        targets = _fluid_targets(protocol, deck)
        state_store = DataStore(db_path)
        try:
            snapshot = state_store.get_fluid_snapshot(state_id)
        finally:
            state_store.close()
    except RunPolicyError:
        if "protocol_path" in locals():
            protocol_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        if "protocol_path" in locals():
            protocol_path.unlink(missing_ok=True)
        raise RunPolicyError(f"fluid-state preflight could not run: {exc}") from exc

    containers = {
        (item["labware_key"], item.get("location_id", "")): item
        for item in snapshot.get("containers", [])
    }
    unknown: list[str] = []
    initial_fluids: dict[str, dict[str, float]] = {}
    for display_name, target in targets:
        key = (target.labware_key, target.location_id or "")
        item = containers.get(key)
        if item is None or not item.get("volume_known", False):
            unknown.append(display_name)
            continue
        initial_fluids[display_name] = {"volume_ul": float(item["current_volume_ul"])}
    if unknown:
        names = ", ".join(sorted(set(unknown)))
        raise RunPolicyError(
            f"fluid-state preflight blocked: volume is unknown for required target(s): {names}. "
            "Reconcile or manually set those containers before running."
        )

    violations = validate_protocol_fluid_volumes(protocol, deck, initial_fluids)
    if violations:
        detail = "; ".join(
            f"step {item.step_index} ({item.command_name}): {item.message}"
            for item in violations
        )
        raise RunPolicyError(f"fluid-state preflight blocked: {detail}")


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump())
    cubos_result_fields = ("status", "steps_executed", "campaign_id", "results")
    if any(hasattr(value, name) for name in cubos_result_fields):
        return {
            name: _jsonable(getattr(value, name))
            for name in cubos_result_fields
            if hasattr(value, name)
        }
    if hasattr(value, "__dict__") and vars(value):
        return {
            str(key): _jsonable(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _check_protocol_policy(
    protocol_yaml: str,
    *,
    allowed_commands: set[str],
    allowed_instruments: set[str],
) -> None:
    try:
        document = yaml.safe_load(protocol_yaml)
    except yaml.YAMLError as exc:
        raise RunPolicyError(f"protocol YAML is not parseable: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("protocol"), list):
        raise RunPolicyError("protocol YAML must contain a top-level protocol list")
    if not document["protocol"]:
        raise RunPolicyError("protocol list must not be empty")

    for index, step in enumerate(document["protocol"]):
        if not isinstance(step, dict) or len(step) != 1:
            raise RunPolicyError(f"step {index} must be a single-command mapping")
        command, body = next(iter(step.items()))
        if allowed_commands and command not in allowed_commands:
            raise RunPolicyError(f"step {index}: command {command!r} is not allowed")
        if isinstance(body, dict):
            instrument = body.get("instrument")
            if allowed_instruments and instrument is not None and instrument not in allowed_instruments:
                raise RunPolicyError(f"step {index}: instrument {instrument!r} is not allowed")


def _mock_execute(
    *,
    gantry_path: Path,
    deck_path: Path,
    protocol_path: Path,
    step_observer: Any | None = None,
) -> Any:
    from cubos.protocol_engine.setup import setup_protocol

    protocol, context = setup_protocol(
        str(gantry_path),
        str(deck_path),
        str(protocol_path),
        gantry=None,
        mock_mode=True,
        step_observer=step_observer,
    )
    context.gantry.connect_instruments()
    try:
        return protocol.execute(context)
    finally:
        context.gantry.disconnect_instruments()


class RunManager:
    def __init__(self, settings: CubOSSettings):
        self.settings = settings
        self.store = RunStore(settings.ensure_run_dir())
        self._lock = threading.Lock()
        self._contents_ownership = get_contents_ownership()
        self._active_run_id: str | None = None
        self._recover_interrupted_runs()

    def _recover_interrupted_runs(self) -> None:
        for record in self.store.incomplete_records():
            record.state = "failed"
            record.finished_at = time.time()
            record.error = "server restarted before the run reached a terminal state"
            self.store.write_error(record, record.error)
            self.store.append_event(record.run_id, state="failed", message=record.error)
            self.store.write(record)

    @property
    def active_run_id(self) -> str | None:
        with self._lock:
            return self._active_run_id

    def submit(self, submission: RunSubmission) -> RunRecord:
        run_id = submission.run_id or uuid.uuid4().hex
        contents_claimed = False
        with self._lock:
            if self._active_run_id is not None:
                raise RunConflictError(f"server busy with run {self._active_run_id!r}")
            if self.store.exists(run_id) or self.store.run_dir(run_id).exists():
                raise RunConflictError(f"run {run_id!r} already exists")
            # Claim before reading the active pointer.  This makes state
            # capture and active-setup edits one indivisible workflow.  The
            # claim applies to legacy stateless runs too: they still own the
            # physical station while hardware executes, even though they do
            # not opt into persistent contents accounting.
            self._contents_ownership.claim_run(run_id)
            contents_claimed = True
            try:
                gantry_yaml, deck_yaml, protocol_yaml = self._resolve_bundle(submission)
                self._validate_bundle(gantry_yaml, deck_yaml, protocol_yaml)
                fluid_state_id = self._resolve_run_state(
                    deck_yaml,
                    protocol_yaml,
                    submission.state,
                    use_active_state=submission.use_active_state,
                )
                if fluid_state_id is not None:
                    self._contents_ownership.bind_fluid_state(run_id, fluid_state_id)
                record = RunRecord(
                    run_id=run_id,
                    state="queued",
                    created_at=time.time(),
                    mock_mode=submission.mock_mode,
                    metadata=submission.metadata,
                    fluid_state_id=fluid_state_id,
                )
                self.store.create(
                    record,
                    gantry_yaml=gantry_yaml,
                    deck_yaml=deck_yaml,
                    protocol_yaml=protocol_yaml,
                )
                self._active_run_id = run_id
            except Exception:
                if contents_claimed:
                    self._contents_ownership.release(run_id)
                raise

        thread = threading.Thread(
            target=self._execute,
            args=(run_id,),
            name=f"cubos-run-{run_id}",
            daemon=True,
        )
        thread.start()
        return record

    def get(self, run_id: str) -> RunRecord | None:
        return self.store.read(run_id)

    def events(self, run_id: str):
        return self.store.events(run_id)

    def cancel(self, run_id: str) -> RunRecord:
        from cubos_api.routers import gantry as gantry_router

        with self._lock:
            record = self.store.read(run_id)
            if record is None:
                raise KeyError(run_id)
            if record.state in {"succeeded", "failed", "cancelled"}:
                raise RunConflictError(f"run {run_id!r} is already {record.state}")
            if self._active_run_id != run_id:
                raise RunConflictError(f"run {run_id!r} is not active")
            record.state = "cancel_requested"
            self.store.append_event(
                run_id,
                state="cancel_requested",
                message="operator requested cancellation",
            )
            self.store.write(record)

        if not record.mock_mode:
            gantry_router.request_feed_hold_interrupt()
        return record

    def _resolve_bundle(self, submission: RunSubmission) -> tuple[str, str, str]:
        if submission.gantry_config is not None:
            assert submission.deck_config is not None
            assert submission.protocol_yaml is not None
            return submission.gantry_config, submission.deck_config, submission.protocol_yaml

        assert submission.gantry_file is not None
        assert submission.deck_file is not None
        assert submission.protocol_file is not None
        base = self.settings.configs_dir
        paths = (
            resolve_config_path(base, "gantry", submission.gantry_file),
            resolve_config_path(base, "deck", submission.deck_file),
            resolve_config_path(base, "protocol", submission.protocol_file),
        )
        for path in paths:
            if not path.is_file():
                raise RunPolicyError(f"configuration file not found: {path.name}")
        return tuple(path.read_text(encoding="utf-8") for path in paths)  # type: ignore[return-value]

    def _validate_bundle(self, gantry_yaml: str, deck_yaml: str, protocol_yaml: str) -> None:
        _check_protocol_policy(
            protocol_yaml,
            allowed_commands=set(self.settings.allowed_commands),
            allowed_instruments=set(self.settings.allowed_instruments),
        )
        expected_gantry = self.settings.expected_gantry_sha256
        if expected_gantry and sha256_text(gantry_yaml) != expected_gantry:
            raise RunPolicyError("gantry configuration digest does not match the device pin")
        expected_deck = self.settings.expected_deck_sha256
        if expected_deck and sha256_text(deck_yaml) != expected_deck:
            raise RunPolicyError("deck configuration digest does not match the device pin")

    def _resolve_run_state(
        self,
        deck_yaml: str,
        protocol_yaml: str,
        state: RunStateSelection | None,
        *,
        use_active_state: bool = False,
    ) -> int | None:
        """Create or resume the run's fluid state, ahead of hardware execution.

        Returns ``None`` for a stateless run (``state`` omitted entirely —
        every run submitted before Feature 07 keeps behaving exactly as it
        did). ``cubos.data`` state exceptions (not-found, deck-fingerprint
        mismatch, reconciliation-required) propagate unchanged so the router
        can map each to a distinct HTTP status.
        """
        # The new workflow uses the explicit persisted setup as the default.
        # Keep stateless behavior when no setup has ever been selected, which
        # preserves compatibility for older clients and fresh installations.
        if state is None and use_active_state:
            active_store = DataStore(self.settings.data_db_path)
            active = active_store.get_active_fluid_state()
            active_store.close()
            if active is None:
                raise RunPolicyError("no active physical setup has been selected")
            state = RunStateSelection(fluid_state_id=int(active["fluid_state_id"]))
        if state is None:
            return None

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".yaml", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(deck_yaml)
                tmp_path = Path(tmp.name)
            try:
                deck = load_deck_from_yaml(tmp_path)
            except DeckLoaderError as exc:
                raise RunPolicyError(f"cannot load run deck: {exc}") from exc

            store = DataStore(self.settings.data_db_path)
            try:
                if state.initial_state is not None:
                    fluids = {
                        key: {
                            "volume_ul": item.volume_ul,
                            "composition": item.composition,
                        }
                        for key, item in state.initial_state.fluids.items()
                    }
                    state_id = store.create_fluid_state(
                        str(tmp_path),
                        deck,
                        label=state.initial_state.label,
                        initial_fluids=fluids,
                    )
                    self._preflight_fluid_state(state_id, deck, protocol_yaml)
                    return state_id
                assert state.fluid_state_id is not None
                state_id = store.resume_fluid_state(
                    state.fluid_state_id, str(tmp_path), deck
                )
                self._preflight_fluid_state(state_id, deck, protocol_yaml)
                return state_id
            finally:
                store.close()
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    def _preflight_fluid_state(
        self, state_id: int, deck: Any, protocol_yaml: str
    ) -> None:
        """Run contents preflight while admission still owns the station."""
        # The helper opens its own short-lived read connection so its
        # snapshot is independent of the store's active transaction.
        _validate_fluid_state_preflight(
            state_id=state_id,
            deck=deck,
            protocol_yaml=protocol_yaml,
            db_path=Path(self.settings.data_db_path),
        )

    def _execute(self, run_id: str) -> None:
        from cubos_api.routers import gantry as gantry_router

        record = self.store.read(run_id)
        if record is None:
            return
        directory = self.store.run_dir(run_id)
        record.state = "running"
        record.started_at = time.time()
        self.store.append_event(run_id, state="running", message="execution started")
        self.store.write(record)

        # Advisory progress reporting; see cubos.protocol_engine.observer for
        # why an observer can never fail a run.
        step_observer = RunStoreStepObserver(self.store, run_id)
        gate_acquired = False
        try:
            gantry_router.begin_run(protocol_file="protocol.yaml")
            gate_acquired = True
            if record.mock_mode:
                raw_result = _mock_execute(
                    gantry_path=directory / "gantry.yaml",
                    deck_path=directory / "deck.yaml",
                    protocol_path=directory / "protocol.yaml",
                    step_observer=step_observer,
                )
            else:
                # Manual bring-up handles (Camera view preview, lighting) hold
                # the vendor device open; the run's own instruments must be
                # able to claim it.
                from cubos_api.routers.instruments import reset_manual_instruments
                reset_manual_instruments()
                raw_result = gantry_router.run_protocol_on_session(
                    gantry_path=str(directory / "gantry.yaml"),
                    deck_path=str(directory / "deck.yaml"),
                    protocol_path=str(directory / "protocol.yaml"),
                    gantry_file="gantry.yaml",
                    deck_file="deck.yaml",
                    protocol_file="protocol.yaml",
                    db_path=self.settings.data_db_path,
                    fluid_state_id=record.fluid_state_id,
                    step_observer=step_observer,
                )
            result = _jsonable(raw_result)
            if record.fluid_state_id is not None or isinstance(result, dict):
                campaign_id = result.get("campaign_id") if isinstance(result, dict) else None
                self._contents_ownership.bind_campaign(run_id, campaign_id)
            record = self.store.read(run_id) or record
            record.state = "succeeded"
            record.result = result
            record.finished_at = time.time()
            self.store.write_result(record, result)
            self.store.append_event(run_id, state="succeeded", message="execution completed")
            self.store.write(record)
        except Exception as exc:  # noqa: BLE001 - persist complete failure details
            log.exception("CubOS run %s failed", run_id)
            record = self.store.read(run_id) or record
            cancelled = record.state == "cancel_requested"
            record.state = "cancelled" if cancelled else "failed"
            record.finished_at = time.time()
            record.error = f"{type(exc).__name__}: {exc}"
            self.store.write_error(record, traceback.format_exc())
            self.store.append_event(
                run_id,
                state=record.state,
                message=record.error,
            )
            self.store.write(record)
        finally:
            if gate_acquired:
                gantry_router.end_run()
            with self._lock:
                if self._active_run_id == run_id:
                    self._active_run_id = None
            self._contents_ownership.release(run_id)


_manager: RunManager | None = None
_manager_lock = threading.Lock()


def get_run_manager() -> RunManager:
    global _manager
    with _manager_lock:
        settings = get_settings()
        desired = settings.run_dir.expanduser().resolve()
        if _manager is None or _manager.store.base_dir != desired:
            _manager = RunManager(settings)
        return _manager


def reset_run_manager() -> None:
    global _manager
    with _manager_lock:
        _manager = None
