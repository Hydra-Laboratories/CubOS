"""Durable sequential and batched BO over the native CubOS run manager."""
from __future__ import annotations

import copy
from contextlib import contextmanager
import json
import logging
import math
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

import yaml

from cubos.data import DataStore
from cubos.optimization import SearchExhaustedError, suggest
from cubos.protocol_engine.setup_validator import run_setup_validation
from cubos_api.config import CubOSSettings, get_settings
from cubos_api.models.campaigns import (
    CampaignRecord,
    CampaignSpec,
    CampaignTrial,
    PendingBatch,
    RefillRequirement,
)
from cubos_api.models.runs import RunSubmission
from cubos_api.models.state import RunStateSelection
from cubos_api.services.campaign_templates import (
    TemplateError, compile_trial, extract_result_context, extract_result_objective,
    validate_objective_provenance, validate_template,
)
from cubos_api.services.run_manager import RunConflictError, RunManager, get_run_manager
from cubos_api.services.yaml_io import resolve_config_path

log = logging.getLogger(__name__)
TERMINAL = {"completed", "stopped", "failed", "interrupted"}
FLUID_COMMANDS = {
    "pick_up_tip", "drop_tip", "transfer", "serial_transfer", "mix",
    "aspirate", "blowout", "rinse_well", "flush_pipette", "purge_pipette",
    "clear_well",
}


class CampaignManager:
    def __init__(self, settings: CubOSSettings, run_manager: RunManager | None = None,
                 *, validator: Callable | None = None, poll_interval: float = 0.2):
        self.settings = settings
        self.runs = run_manager or get_run_manager()
        self.base = settings.ensure_run_dir() / "campaigns"
        self.base.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._records: dict[str, CampaignRecord] = {}
        self._workers: set[str] = set()
        self._poll = poll_interval
        self._validator = validator or self._validate_setup
        for path in self.base.glob("*/campaign.json"):
            try:
                record = CampaignRecord.model_validate_json(path.read_text())
                self._records[record.campaign_id] = record
                if record.state not in TERMINAL and record.state != "awaiting_refill":
                    record.state = "interrupted"
                    record.stop_reason = "server_restart"
                    record.error = "Server restarted; inspect the last run and physical state before starting a new campaign."
                    self._save(record)
            except (ValueError, OSError):
                log.exception("Cannot recover campaign %s", path)

    def _save(self, record: CampaignRecord):
        record.updated_at = time.time()
        directory = self.base / record.campaign_id
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "campaign.json.tmp"
        temporary.write_text(record.model_dump_json(indent=2))
        temporary.replace(directory / "campaign.json")

    @contextmanager
    def controller_recovery(self):
        """Exclude campaign starts and every nonterminal campaign during reset."""
        with self._lock:
            if any(record.state not in TERMINAL for record in self._records.values()):
                raise RunConflictError("An active-learning campaign owns the station")
            with self.runs.inventory_edit():
                yield

    def list(self):
        with self._lock:
            return [r.model_copy(deep=True) for r in sorted(self._records.values(), key=lambda r: r.created_at, reverse=True)[:100]]

    def get(self, campaign_id):
        with self._lock:
            record = self._records.get(campaign_id)
            if record is None:
                raise KeyError(campaign_id)
            return record.model_copy(deep=True)

    def _bundle(self, spec):
        result = []
        for category, name in (("gantry", spec.gantry_file), ("deck", spec.deck_file), ("protocol", spec.protocol_file)):
            path = resolve_config_path(self.settings.configs_dir, category, name)
            if not path.is_file():
                raise ValueError(f"Missing {category} file: {name}")
            result.append(path.read_text())
        return tuple(result)

    @staticmethod
    def _validate_setup(gantry, deck, protocol, tip_snapshot=None):
        with tempfile.TemporaryDirectory(prefix="cubos-campaign-check-") as directory:
            paths = []
            for name, content in (("gantry", gantry), ("deck", deck), ("protocol", protocol)):
                path = Path(directory) / f"{name}.yaml"
                path.write_text(content)
                paths.append(str(path))
            result = run_setup_validation(*paths, tip_snapshot=tip_snapshot)
            if not result.passed:
                raise ValueError("; ".join(result.errors) or result.output)

    def _preflight(self, spec, bundle):
        gantry, deck, protocol = bundle
        raw = spec.model_dump()
        validate_template(protocol, raw)
        self.runs._validate_bundle(gantry, deck, protocol)
        template_steps = yaml.safe_load(protocol)["protocol"]
        fluid_handling = any(
            next(iter(step)) in FLUID_COMMANDS
            for step in template_steps
        )
        has_tip_pickup = any("pick_up_tip" in step for step in template_steps)
        if not spec.mock_mode and fluid_handling and spec.fluid_state_id is None:
            raise ValueError(
                "Real fluid-handling campaigns require a durable fluid-state ID. "
                "Create a state matching the physical fluids and tip inventory, "
                "then select it before starting."
            )
        if spec.fluid_state_id is not None:
            self.runs._resolve_run_state(
                deck,
                RunStateSelection(fluid_state_id=spec.fluid_state_id),
            )
            tip_snapshot = self._tip_snapshot(spec.fluid_state_id)
            pipette = tip_snapshot["pipette"]
            if pipette["attachment_uncertain"]:
                raise ValueError(
                    "The durable state has an uncertain pipette attachment that "
                    "requires operator reconciliation"
                )
            if has_tip_pickup and pipette["tip_extension_mm"] is not None:
                raise ValueError(
                    "Campaigns with tip pickup steps must start with a bare pipette"
                )
            available_tips = [
                f"{item['rack_key']}.{item['slot_id']}"
                for item in tip_snapshot["containers"]
                if item["status"] == "available"
            ]
        else:
            tip_snapshot = None
            available_tips = None
        if spec.batch_size > 1:
            return self._preflight_batch(
                spec, bundle, tip_snapshot, available_tips,
            )
        parameters = self._suggest(spec, [])
        seen_tips = set()
        for index in range(spec.stop.max_trials):
            trial_yaml = compile_trial(protocol, raw, parameters, index)
            for step in yaml.safe_load(trial_yaml)["protocol"]:
                if "pick_up_tip" in step:
                    target = step["pick_up_tip"].get("position")
                    if target in seen_tips:
                        raise ValueError(f"Repeated tip target {target!r}; define distinct per-trial target sequences.")
                    seen_tips.add(target)
                    if available_tips is not None:
                        if target not in available_tips:
                            raise ValueError(
                                f"Tip target {target!r} is not available in durable "
                                f"fluid state {spec.fluid_state_id}."
                            )
                        available_tips.remove(target)
        preview = compile_trial(protocol, raw, parameters, 0)
        self._validator(gantry, deck, preview, tip_snapshot)
        return {"parameters": parameters, "protocol_yaml": preview}

    def _preflight_batch(self, spec, bundle, tip_snapshot, available_tips):
        from cubos_api.services.color_batch import compile_color_trial_batch

        if spec.objective.mode != "result":
            raise ValueError("Batched color campaigns require a protocol result objective")
        gantry, deck, base_protocol = bundle
        pending: list[dict[str, float]] = []
        planned: list[dict[str, float]] = []
        for _ in range(spec.stop.max_trials):
            try:
                point = self._suggest(spec, [], exclude_points=pending)
            except SearchExhaustedError as exc:
                raise ValueError(
                    "The feasible mixture grid has fewer unique points than "
                    "the requested sample budget"
                ) from exc
            pending.append(point)
            planned.append(point)
        seen_tips: set[str] = set()
        available = set(available_tips) if available_tips is not None else None
        virtual_tip_snapshot = copy.deepcopy(tip_snapshot)
        preview = None
        for start_index in range(0, spec.stop.max_trials, spec.batch_size):
            compiled = compile_color_trial_batch(
                base_protocol,
                spec,
                planned[start_index:start_index + spec.batch_size],
                start_index,
            )
            if preview is None:
                preview = compiled
            batch_tips: set[str] = set()
            for step in yaml.safe_load(compiled.protocol_yaml)["protocol"]:
                if "pick_up_tip" not in step:
                    continue
                target = step["pick_up_tip"].get("position")
                if not isinstance(target, str):
                    raise ValueError("Every batch tip pickup needs a named tip target")
                if target in batch_tips:
                    raise ValueError(
                        f"Tip target {target!r} is picked up more than once in "
                        "one batch"
                    )
                if target in seen_tips:
                    raise ValueError(
                        f"Tip target {target!r} is reused across batches"
                    )
                if available is not None and target not in available:
                    raise ValueError(
                        f"Tip target {target!r} is not available in durable "
                        f"fluid state {spec.fluid_state_id}."
                    )
                batch_tips.add(target)
            seen_tips.update(batch_tips)
            self._validator(
                gantry, deck, compiled.protocol_yaml, virtual_tip_snapshot,
            )
            if virtual_tip_snapshot is not None:
                for item in virtual_tip_snapshot["containers"]:
                    target = f"{item['rack_key']}.{item['slot_id']}"
                    if target in batch_tips:
                        item["status"] = "consumed"
        assert preview is not None
        return {
            "parameters": planned[0],
            "protocol_yaml": preview.protocol_yaml,
            "sample_map": list(preview.sample_map),
        }

    def _tip_snapshot(self, fluid_state_id):
        if fluid_state_id is None:
            return None
        store = DataStore(self.settings.data_db_path)
        try:
            return store.get_tip_snapshot(fluid_state_id)
        finally:
            store.close()

    def _batch_stock_shortages(
        self, spec: CampaignSpec, deck_yaml: str, protocol_yaml: str,
    ) -> list[dict[str, float | str]]:
        """Check all compiled transfers against the current durable state."""
        if spec.mock_mode or spec.fluid_state_id is None:
            return []
        # This also rejects pending fluid/tip/cap operations before a refill
        # check can authorize the next batch.
        self.runs._resolve_run_state(
            deck_yaml,
            RunStateSelection(fluid_state_id=spec.fluid_state_id),
        )
        document = yaml.safe_load(protocol_yaml)
        required: dict[str, float] = {}
        steps = document.get("protocol", []) if isinstance(document, dict) else []
        for step in steps:
            if not isinstance(step, dict) or len(step) != 1:
                continue
            command, body = next(iter(step.items()))
            if command not in {"transfer", "serial_transfer"} or not isinstance(body, dict):
                continue
            source = body.get("source")
            if not isinstance(source, str) or not source.strip():
                raise ValueError("Every batch transfer needs a named stock source")
            try:
                volume = float(body.get("volume_ul"))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Batch stock preflight found invalid volume for {source!r}"
                ) from exc
            if not math.isfinite(volume) or volume <= 0:
                raise ValueError(
                    f"Batch stock preflight found invalid volume for {source!r}"
                )
            required[source] = required.get(source, 0.0) + volume

        store = DataStore(self.settings.data_db_path)
        try:
            snapshot = store.get_fluid_snapshot(spec.fluid_state_id)
        finally:
            store.close()
        containers = {
            f"{item['labware_key']}.{item['location_id']}"
            if item["location_id"] else item["labware_key"]: item
            for item in snapshot["containers"]
        }
        shortages: list[dict[str, float | str]] = []
        for source, volume in required.items():
            container = containers.get(source)
            available = (
                float(container["current_volume_ul"])
                if container is not None else 0.0
            )
            if container is None or available + 1e-6 < volume:
                shortages.append({
                    "target": source,
                    "available_ul": available,
                    "required_ul": volume,
                    "capacity_ul": (
                        float(container["capacity_ul"])
                        if container is not None else 0.0
                    ),
                })
        return shortages

    def validate(self, spec):
        try:
            preview = self._preflight(spec, self._bundle(spec))
            return {"valid": True, "errors": [], "preview": preview}
        except (ValueError, OSError) as exc:
            return {"valid": False, "errors": [f"{type(exc).__name__}: {exc}"]}

    @staticmethod
    def _suggest(spec, observations, *, exclude_points=None):
        return suggest([p.model_dump() for p in spec.parameters], observations,
                       method=spec.optimizer.method, kernel=spec.optimizer.kernel,
                       initial_trials=spec.optimizer.initial_trials,
                       initial_points=spec.optimizer.initial_points,
                       exploration=spec.optimizer.exploration,
                       seed=spec.optimizer.seed + len(exclude_points or []),
                       direction=spec.objective.direction,
                       sum_constraint=spec.sum_constraint.model_dump() if spec.sum_constraint else None,
                       exclude_points=exclude_points)

    def start(self, spec):
        bundle = self._bundle(spec)
        self._preflight(spec, bundle)
        if not spec.mock_mode:
            from cubos_api.routers import gantry as gantry_router
            session = gantry_router.current_session()
            if session is None or not session.connected:
                raise ValueError("Connect the calibrated gantry before starting a real campaign")
            if session.calibration_active:
                raise ValueError("Finish calibration before starting a campaign")
            if gantry_router.run_active():
                raise RunConflictError("A native protocol is already running")
        campaign_id = uuid.uuid4().hex
        with self._lock:
            if any(r.state not in TERMINAL for r in self._records.values()):
                raise RunConflictError("An active-learning campaign is already active")
            self.runs.reserve_campaign(campaign_id)
            record = CampaignRecord(campaign_id=campaign_id, spec=spec.model_copy(deep=True),
                                    created_at=time.time(), updated_at=time.time())
            try:
                self._records[campaign_id] = record
                self._save(record)
                for name, text in zip(("gantry", "deck", "protocol"), bundle):
                    (self.base / campaign_id / f"{name}.yaml").write_text(text)
            except BaseException:
                self.runs.release_campaign(campaign_id)
                self._records.pop(campaign_id, None)
                raise
            self._workers.add(campaign_id)
            threading.Thread(target=self._loop, args=(campaign_id, bundle), daemon=True,
                             name=f"cubos-campaign-{campaign_id}").start()
            return record.model_copy(deep=True)

    def attach_fluid_state(
        self,
        campaign_id: str,
        fluid_state_id: int,
        reconciliation_note: str,
    ) -> CampaignRecord:
        note = reconciliation_note.strip()
        if not note:
            raise ValueError("A physical-state reconciliation note is required")
        with self._lock:
            record = self._records.get(campaign_id)
            if record is None:
                raise KeyError(campaign_id)
            if record.state not in {"failed", "interrupted", "stopped"}:
                raise RunConflictError(
                    "A fluid state can only be attached while the campaign is stopped"
                )
            if record.active_run_id is not None:
                raise RunConflictError("The campaign still has an active native run")
            existing = record.spec.fluid_state_id
            if existing is not None and existing != fluid_state_id:
                raise RunConflictError(
                    f"Campaign is already bound to fluid state {existing}; replacing "
                    "a durable physical state is not allowed"
                )
            if record.spec.mock_mode:
                raise ValueError("Offline campaigns cannot attach a real fluid state")
            bundle = tuple(
                (self.base / campaign_id / f"{name}.yaml").read_text()
                for name in ("gantry", "deck", "protocol")
            )
            self.runs._resolve_run_state(
                bundle[1], RunStateSelection(fluid_state_id=fluid_state_id),
            )
            record.spec.fluid_state_id = fluid_state_id
            record.fluid_state_reconciliation_note = note
            self._save(record)
            return record.model_copy(deep=True)

    def control(self, campaign_id, action):
        with self._lock:
            record = self._records.get(campaign_id)
            if record is None:
                raise KeyError(campaign_id)
            if (
                record.state == "awaiting_refill"
                and action == "resume"
                and campaign_id not in self._workers
            ):
                if record.pending_batch is None:
                    raise RunConflictError(
                        "Campaign is awaiting refill without a pending batch"
                    )
                bundle = tuple(
                    (self.base / campaign_id / f"{name}.yaml").read_text()
                    for name in ("gantry", "deck", "protocol")
                )
                if not record.spec.mock_mode:
                    from cubos_api.routers import gantry as gantry_router
                    session = gantry_router.current_session()
                    if session is None or not session.connected:
                        raise ValueError(
                            "Connect the calibrated gantry before resuming a real campaign"
                        )
                self.runs.reserve_campaign(campaign_id)
                record.pause_requested = False
                record.pause_reason = None
                record.state = "running"
                record.error = None
                self._save(record)
                self._workers.add(campaign_id)
                threading.Thread(
                    target=self._loop, args=(campaign_id, bundle), daemon=True,
                    name=f"cubos-campaign-{campaign_id}",
                ).start()
                return record.model_copy(deep=True)
            if (
                record.state in TERMINAL
                and action == "resume"
                and record.pending_batch is not None
            ):
                bundle = tuple(
                    (self.base / campaign_id / f"{name}.yaml").read_text()
                    for name in ("gantry", "deck", "protocol")
                )
                if not record.spec.mock_mode:
                    from cubos_api.routers import gantry as gantry_router
                    session = gantry_router.current_session()
                    if session is None or not session.connected:
                        raise ValueError(
                            "Connect the calibrated gantry before resuming a real campaign"
                        )
                self.runs.reserve_campaign(campaign_id)
                record.pause_requested = False
                record.pause_reason = None
                record.stop_requested = False
                record.stop_reason = None
                record.state = "running"
                record.error = None
                self._save(record)
                self._workers.add(campaign_id)
                threading.Thread(
                    target=self._loop, args=(campaign_id, bundle), daemon=True,
                    name=f"cubos-campaign-{campaign_id}",
                ).start()
                return record.model_copy(deep=True)
            if record.state in TERMINAL:
                if action == "resume" and record.spec.batch_size > 1:
                    raise RunConflictError(
                        "A stopped batch campaign cannot resume automatically. "
                        "Inspect the completed sample wells, used tips, and durable "
                        "fluid state; then prepare a new campaign that excludes "
                        "physically used resources."
                    )
                if (
                    action != "resume"
                    or record.state not in {"failed", "interrupted"}
                    or not record.trials
                ):
                    raise RunConflictError("Campaign has already stopped")
                trial = record.trials[-1]
                if (
                    not record.spec.mock_mode
                    and self._uses_fluid_handling(record.spec, campaign_id)
                    and record.spec.fluid_state_id is None
                ):
                    raise RunConflictError(
                        "This legacy physical campaign has no durable fluid/tip state. "
                        "Create a state matching the current physical setup and attach "
                        "it before attempting recovery."
                    )
                child = self.runs.get(trial.run_id)
                if child is None or child.state != "succeeded" or child.result is None:
                    raise RunConflictError(
                        "The failed campaign has no completed trial to recover"
                    )
                measurement = extract_result_context(
                    child.result, record.spec.objective.path
                )
                try:
                    validate_objective_provenance(
                        record.spec.objective.path, measurement,
                    )
                    objective = extract_result_objective(
                        child.result, record.spec.objective.path
                    )
                except TemplateError as exc:
                    trial.objective_status = "unverified"
                    trial.measurement = measurement
                    self._save(record)
                    raise RunConflictError(
                        f"The completed trial was preserved but cannot be used for "
                        f"optimization: {exc}. Capture a new accepted target/profile "
                        "and start a campaign that excludes physically used wells and tips."
                    ) from exc
                trial.objective = objective
                trial.measurement = measurement
                trial.objective_status = "accepted"
                objectives = [
                    item.objective for item in record.trials
                    if item.objective is not None and item.objective_status == "accepted"
                ]
                record.best_objective = (
                    min(objectives) if record.spec.objective.direction == "minimize"
                    else max(objectives)
                )
                bundle = tuple(
                    (self.base / campaign_id / f"{name}.yaml").read_text()
                    for name in ("gantry", "deck", "protocol")
                )
                if not record.spec.mock_mode:
                    from cubos_api.routers import gantry as gantry_router
                    session = gantry_router.current_session()
                    if session is None or not session.connected:
                        raise ValueError(
                            "Connect the calibrated gantry before resuming a real campaign"
                        )
                self.runs.reserve_campaign(campaign_id)
                record.state = "running"
                record.stop_reason = None
                record.error = None
                record.pause_requested = False
                record.stop_requested = False
                self._save(record)
                threading.Thread(
                    target=self._loop, args=(campaign_id, bundle), daemon=True,
                    name=f"cubos-campaign-{campaign_id}",
                ).start()
                return record.model_copy(deep=True)
            if action == "pause":
                record.pause_requested = True
            elif action == "resume":
                if record.state == "awaiting_refill":
                    if record.pending_batch is None:
                        raise RunConflictError(
                            "Campaign is awaiting refill without a pending batch"
                        )
                    # Inventory mutation is only allowed while this ownership
                    # reservation is absent. Reacquire it before clearing the
                    # boundary so no unrelated run can race the resume.
                    self.runs.reserve_campaign(campaign_id)
                    record.pause_reason = None
                record.pause_requested = False
                if record.state == "paused":
                    record.state = "running"
                elif record.state == "awaiting_refill":
                    record.state = "running"
            elif action in {"stop", "cancel"}:
                record.stop_requested = True
                record.stop_reason = "operator_cancelled" if action == "cancel" else "operator_stopped"
            else:
                raise ValueError("Unknown campaign action")
            self._save(record)
            active = record.active_run_id
        if action == "cancel" and active:
            child = self.runs.get(active)
            if child is not None and child.state not in {"succeeded", "failed", "cancelled"}:
                try:
                    self.runs.cancel(active)
                except RunConflictError:
                    latest = self.runs.get(active)
                    if latest is None or latest.state not in {"succeeded", "failed", "cancelled"}:
                        raise
        return self.get(campaign_id)

    def observe(self, campaign_id, value):
        import math
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError("Observation must be a finite number")
        with self._lock:
            record = self._records.get(campaign_id)
            if record is None:
                raise KeyError(campaign_id)
            if record.state != "awaiting_observation" or not record.trials or record.trials[-1].objective is not None:
                raise RunConflictError("No pending observation for this campaign")
            record.trials[-1].objective = float(value)
            record.trials[-1].objective_status = "accepted"
            self._save(record)
        return self.get(campaign_id)

    def _finish(self, record, state, reason, error=None):
        record.state, record.stop_reason, record.error = state, reason, error
        record.active_run_id = None
        if state == "completed":
            record.refill_requirements = []
        self._save(record)

    def _loop(self, campaign_id, bundle):
        record = self._records[campaign_id]
        spec = record.spec
        if spec.batch_size > 1:
            self._loop_batch(campaign_id, bundle)
            return
        stagnant = 0
        try:
            while True:
                with self._lock:
                    if record.stop_requested:
                        self._finish(record, "stopped", record.stop_reason or "operator_stopped")
                        return
                    if record.pause_requested:
                        paused_state = (
                            "awaiting_refill"
                            if record.pause_reason == "inventory_refill"
                            else "paused"
                        )
                        if record.state != paused_state:
                            record.state = paused_state
                            self._save(record)
                        paused = True
                    else:
                        paused = False
                    if not paused:
                        if len(record.trials) >= spec.stop.max_trials:
                            self._finish(record, "completed", "trial_budget")
                            return
                        if spec.stop.max_seconds and time.time() - record.created_at >= spec.stop.max_seconds:
                            self._finish(record, "completed", "time_budget")
                            return
                if paused:
                    time.sleep(self._poll)
                    continue
                observations = [{"parameters": t.parameters, "objective": t.objective}
                                for t in record.trials
                                if t.objective is not None
                                and t.objective_status == "accepted"]
                try:
                    parameters = self._suggest(spec, observations)
                except SearchExhaustedError as exc:
                    with self._lock:
                        self._finish(record, "completed", "search_exhausted", str(exc))
                    return
                index = len(record.trials)
                protocol = compile_trial(bundle[2], spec.model_dump(), parameters, index)
                self._validator(bundle[0], bundle[1], protocol, self._tip_snapshot(spec.fluid_state_id))
                submission = RunSubmission(run_id=f"{campaign_id}-trial-{index+1}",
                    gantry_config=bundle[0], deck_config=bundle[1], protocol_yaml=protocol,
                    mock_mode=spec.mock_mode,
                    state=RunStateSelection(fluid_state_id=spec.fluid_state_id) if spec.fluid_state_id else None,
                    metadata={"active_learning_campaign_id": campaign_id, "trial": index+1, "parameters": parameters})
                with self._lock:
                    if record.stop_requested or record.pause_requested:
                        continue
                    child = self.runs.submit(submission, campaign_owner=campaign_id)
                    trial = CampaignTrial(index=index, parameters=parameters, run_id=child.run_id, state=child.state)
                    record.trials.append(trial)
                    record.active_run_id = child.run_id
                    record.state = "running"
                    self._save(record)
                while True:
                    child = self.runs.get(trial.run_id)
                    if child is None:
                        raise RuntimeError(f"Native run {trial.run_id} disappeared")
                    with self._lock:
                        if trial.state != child.state:
                            trial.state = child.state
                            self._save(record)
                    if child.state in {"succeeded", "failed", "cancelled"} and self.runs.active_run_id != child.run_id:
                        break
                    time.sleep(self._poll)
                with self._lock:
                    record.active_run_id = None
                    if child.state != "succeeded":
                        trial.error = child.error
                        self._finish(record, "stopped" if record.stop_requested else "failed",
                                     record.stop_reason or "run_failed", child.error)
                        return
                    if record.stop_requested and spec.objective.mode == "manual":
                        self._finish(record, "stopped", record.stop_reason or "operator_stopped")
                        return
                    if spec.objective.mode == "manual":
                        record.state = "awaiting_observation"
                        self._save(record)
                    else:
                        measurement = extract_result_context(
                            child.result, spec.objective.path
                        )
                        try:
                            validate_objective_provenance(
                                spec.objective.path, measurement,
                            )
                            trial.objective = extract_result_objective(
                                child.result, spec.objective.path,
                            )
                        except TemplateError:
                            trial.objective_status = "rejected"
                            trial.measurement = measurement
                            raise
                        trial.measurement = measurement
                        trial.objective_status = "accepted"
                while trial.objective is None:
                    with self._lock:
                        if record.stop_requested:
                            self._finish(record, "stopped", record.stop_reason or "operator_stopped")
                            return
                    time.sleep(self._poll)
                with self._lock:
                    previous = record.best_objective
                    value = trial.objective
                    assert value is not None
                    improvement = float("inf") if previous is None else ((previous-value) if spec.objective.direction == "minimize" else (value-previous))
                    if previous is None or improvement > 0:
                        record.best_objective = value
                    stagnant = 0 if improvement > spec.stop.min_improvement else stagnant+1
                    self._save(record)
                    if record.stop_requested:
                        self._finish(record, "stopped", record.stop_reason or "operator_stopped")
                        return
                    target = spec.stop.target_value
                    if target is not None and ((value <= target) if spec.objective.direction == "minimize" else (value >= target)):
                        self._finish(record, "completed", "target_reached")
                        return
                    if spec.stop.patience and stagnant >= spec.stop.patience:
                        self._finish(record, "completed", "no_improvement")
                        return
        except Exception as exc:
            log.exception("Active-learning campaign %s failed", campaign_id)
            with self._lock:
                self._finish(record, "failed", "error", f"{type(exc).__name__}: {exc}")
        finally:
            self.runs.release_campaign(campaign_id)
            self._workers.discard(campaign_id)

    def _loop_batch(self, campaign_id, bundle):
        # TODO(iter): add permanent batch lifecycle regressions after operator review.
        from cubos_api.services.color_batch import compile_color_trial_batch

        record = self._records[campaign_id]
        spec = record.spec
        stagnant = 0
        try:
            while True:
                with self._lock:
                    if record.stop_requested:
                        self._finish(
                            record, "stopped",
                            record.stop_reason or "operator_stopped",
                        )
                        return
                    if record.pause_requested:
                        paused_state = (
                            "awaiting_refill"
                            if record.pause_reason == "inventory_refill"
                            else "paused"
                        )
                        if record.state != paused_state:
                            record.state = paused_state
                            self._save(record)
                        paused = True
                    else:
                        paused = False
                    if not paused:
                        if len(record.trials) >= spec.stop.max_trials:
                            self._finish(record, "completed", "trial_budget")
                            return
                        if (
                            spec.stop.max_seconds
                            and time.time() - record.created_at >= spec.stop.max_seconds
                        ):
                            self._finish(record, "completed", "time_budget")
                            return
                if paused:
                    time.sleep(self._poll)
                    continue

                batch_start = len(record.trials)
                pending = record.pending_batch
                if pending is not None:
                    if pending.batch_index != batch_start // spec.batch_size + 1:
                        raise RuntimeError(
                            "Pending batch does not match completed trial count"
                        )
                    parameter_sets = [dict(point) for point in pending.parameters]
                    protocol_yaml = pending.protocol_yaml
                    objective_paths = tuple(pending.objective_paths)
                    sample_map = tuple(pending.sample_map)
                    batch_number = pending.batch_index
                else:
                    observations = [
                        {"parameters": trial.parameters, "objective": trial.objective}
                        for trial in record.trials
                        if trial.objective is not None
                        and trial.objective_status == "accepted"
                    ]
                    batch_limit = min(
                        spec.batch_size, spec.stop.max_trials - batch_start,
                    )
                    parameter_sets = []
                    for _ in range(batch_limit):
                        try:
                            point = self._suggest(
                                spec, observations, exclude_points=parameter_sets,
                            )
                        except SearchExhaustedError:
                            break
                        parameter_sets.append(point)
                    if not parameter_sets:
                        with self._lock:
                            self._finish(record, "completed", "search_exhausted")
                        return

                    compiled = compile_color_trial_batch(
                        bundle[2], spec, parameter_sets, batch_start,
                    )
                    if (
                        len(compiled.objective_paths) != len(parameter_sets)
                        or len(compiled.sample_map) != len(parameter_sets)
                    ):
                        raise RuntimeError(
                            "Batch compiler did not return one objective and sample "
                            "map entry per proposed mixture"
                        )
                    for offset, (point, path, sample) in enumerate(zip(
                        parameter_sets, compiled.objective_paths,
                        compiled.sample_map,
                    )):
                        if (
                            sample.get("sample_index") != batch_start + offset
                            or sample.get("parameters") != point
                            or sample.get("objective_path") != path
                            or not isinstance(sample.get("candidate_well"), str)
                        ):
                            raise RuntimeError(
                                "Batch compiler returned mismatched sample provenance"
                            )
                    protocol_yaml = compiled.protocol_yaml
                    objective_paths = compiled.objective_paths
                    sample_map = compiled.sample_map
                    batch_number = batch_start // spec.batch_size + 1
                    pending = PendingBatch(
                        batch_index=batch_number,
                        parameters=parameter_sets,
                        protocol_yaml=protocol_yaml,
                        objective_paths=list(objective_paths),
                        sample_map=list(sample_map),
                        run_id=f"{campaign_id}-batch-{batch_number}",
                    )
                    with self._lock:
                        record.pending_batch = pending
                        self._save(record)

                # Re-run the existing tip/setup preflight on a resumed pending
                # batch, then check stock before RunManager.submit can create a
                # native run or trigger any hardware action.
                self._validator(
                    bundle[0], bundle[1], protocol_yaml,
                    self._tip_snapshot(spec.fluid_state_id),
                )
                shortages = self._batch_stock_shortages(
                    spec, bundle[1], protocol_yaml,
                )
                if shortages:
                    with self._lock:
                        record.pause_requested = True
                        record.pause_reason = "inventory_refill"
                        record.state = "awaiting_refill"
                        record.refill_requirements = [
                            RefillRequirement.model_validate(item)
                            for item in shortages
                        ]
                        record.error = (
                            "Batch is waiting for operator-confirmed stock refill: "
                            + "; ".join(
                                f"{item['target']}: requires "
                                f"{item['required_ul']:g} uL, durable state has "
                                f"{item['available_ul']:g} uL"
                                for item in shortages
                            )
                        )
                        self._save(record)
                    # The refill endpoint requires an idle, unreserved station.
                    self.runs.release_campaign(campaign_id)
                    time.sleep(self._poll)
                    continue

                submission = RunSubmission(
                    run_id=pending.run_id or f"{campaign_id}-batch-{batch_number}",
                    gantry_config=bundle[0],
                    deck_config=bundle[1],
                    protocol_yaml=protocol_yaml,
                    mock_mode=spec.mock_mode,
                    state=(
                        RunStateSelection(fluid_state_id=spec.fluid_state_id)
                        if spec.fluid_state_id else None
                    ),
                    metadata={
                        "active_learning_campaign_id": campaign_id,
                        "batch": batch_number,
                        "sample_map": list(sample_map),
                    },
                )
                with self._lock:
                    if record.stop_requested or record.pause_requested:
                        continue
                    child = self.runs.get(submission.run_id)
                    if child is None:
                        child = self.runs.submit(
                            submission, campaign_owner=campaign_id,
                        )
                    batch_trials = [
                        CampaignTrial(
                            index=batch_start + offset,
                            parameters=point,
                            run_id=child.run_id,
                            state=child.state,
                            objective_path=objective_paths[offset],
                            sample_well=str(
                                sample_map[offset]["candidate_well"]
                            ),
                            batch_index=batch_number,
                        )
                        for offset, point in enumerate(parameter_sets)
                    ]
                    record.trials.extend(batch_trials)
                    record.pending_batch = None
                    record.pause_reason = None
                    record.error = None
                    record.refill_requirements = []
                    record.active_run_id = child.run_id
                    record.state = "running"
                    self._save(record)

                while True:
                    child = self.runs.get(submission.run_id)
                    if child is None:
                        raise RuntimeError(
                            f"Native batch run {submission.run_id} disappeared"
                        )
                    with self._lock:
                        if any(trial.state != child.state for trial in batch_trials):
                            for trial in batch_trials:
                                trial.state = child.state
                            self._save(record)
                    if (
                        child.state in {"succeeded", "failed", "cancelled"}
                        and self.runs.active_run_id != child.run_id
                    ):
                        break
                    time.sleep(self._poll)

                with self._lock:
                    record.active_run_id = None
                    if child.state != "succeeded":
                        for trial in batch_trials:
                            trial.objective_status = "unverified"
                            trial.error = child.error or (
                                "Batch did not complete; physically used wells and "
                                "tips require reconciliation"
                            )
                        self._finish(
                            record,
                            "stopped" if record.stop_requested else "failed",
                            "batch_interrupted_requires_reconciliation",
                            child.error or (
                                "A batch may have partially used wells and tips. "
                                "Inspect the physical setup before creating a new campaign."
                            ),
                        )
                        return

                    rejected: list[str] = []
                    for trial in batch_trials:
                        assert trial.objective_path is not None
                        try:
                            measurement = extract_result_context(
                                child.result, trial.objective_path,
                            )
                            trial.measurement = measurement
                            validate_objective_provenance(
                                trial.objective_path, measurement,
                            )
                            if (
                                measurement is None
                                or not isinstance(measurement.get("well_identity"), dict)
                                or measurement["well_identity"].get("expected_well")
                                != trial.sample_well
                            ):
                                raise TemplateError(
                                    "Sample result does not identify its expected "
                                    f"protocol well {trial.sample_well!r}"
                                )
                            trial.objective = extract_result_objective(
                                child.result, trial.objective_path,
                            )
                            trial.objective_status = "accepted"
                        except (TemplateError, ValueError) as exc:
                            trial.objective_status = "rejected"
                            trial.error = f"{type(exc).__name__}: {exc}"
                            rejected.append(
                                f"sample {trial.index + 1} ({trial.sample_well}): {exc}"
                            )
                    self._save(record)
                    if rejected:
                        self._finish(
                            record, "failed", "batch_objective_rejected",
                            "Batch results were preserved, but optimization "
                            "cannot continue: " + "; ".join(rejected),
                        )
                        return

                    target_reached = False
                    for trial in batch_trials:
                        value = trial.objective
                        assert value is not None
                        previous = record.best_objective
                        improvement = (
                            float("inf") if previous is None
                            else previous - value
                            if spec.objective.direction == "minimize"
                            else value - previous
                        )
                        if previous is None or improvement > 0:
                            record.best_objective = value
                        stagnant = (
                            0 if improvement > spec.stop.min_improvement
                            else stagnant + 1
                        )
                        target = spec.stop.target_value
                        if target is not None and (
                            (value <= target)
                            if spec.objective.direction == "minimize"
                            else (value >= target)
                        ):
                            target_reached = True
                    self._save(record)
                    if record.stop_requested:
                        self._finish(
                            record, "stopped",
                            record.stop_reason or "operator_stopped",
                        )
                        return
                    if target_reached:
                        self._finish(record, "completed", "target_reached")
                        return
                    if spec.stop.patience and stagnant >= spec.stop.patience:
                        self._finish(record, "completed", "no_improvement")
                        return
        except Exception as exc:
            log.exception("Active-learning batch campaign %s failed", campaign_id)
            with self._lock:
                self._finish(
                    record, "failed", "batch_error_requires_reconciliation",
                    f"{type(exc).__name__}: {exc}. Inspect physically used wells "
                    "and tips before creating a new campaign.",
                )
        finally:
            self.runs.release_campaign(campaign_id)
            self._workers.discard(campaign_id)

    def _uses_fluid_handling(self, spec: CampaignSpec, campaign_id: str) -> bool:
        protocol = (self.base / campaign_id / "protocol.yaml").read_text()
        return any(
            next(iter(step)) in FLUID_COMMANDS
            for step in yaml.safe_load(protocol)["protocol"]
        )


_manager = None
_manager_lock = threading.Lock()


def get_campaign_manager():
    global _manager
    with _manager_lock:
        settings = get_settings()
        if _manager is None or _manager.base.parent != settings.ensure_run_dir():
            _manager = CampaignManager(settings)
        return _manager
