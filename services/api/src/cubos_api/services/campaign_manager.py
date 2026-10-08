"""Durable sequential BO orchestration over the native CubOS run manager."""
from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

import yaml

from cubos.optimization import SearchExhaustedError, suggest
from cubos.protocol_engine.setup_validator import run_setup_validation
from cubos_api.config import CubOSSettings, get_settings
from cubos_api.models.campaigns import CampaignRecord, CampaignSpec, CampaignTrial
from cubos_api.models.runs import RunSubmission
from cubos_api.models.state import RunStateSelection
from cubos_api.services.campaign_templates import (
    compile_trial, extract_result_objective, validate_template,
)
from cubos_api.services.run_manager import RunConflictError, RunManager, get_run_manager
from cubos_api.services.yaml_io import resolve_config_path

log = logging.getLogger(__name__)
TERMINAL = {"completed", "stopped", "failed", "interrupted"}
FLUID_COMMANDS = {"pick_up_tip", "drop_tip", "transfer", "serial_transfer", "mix", "aspirate", "blowout", "rinse_well", "flush_pipette", "purge_pipette", "clear_well"}


class CampaignManager:
    def __init__(self, settings: CubOSSettings, run_manager: RunManager | None = None,
                 *, validator: Callable | None = None, poll_interval: float = 0.2):
        self.settings = settings
        self.runs = run_manager or get_run_manager()
        self.base = settings.ensure_run_dir() / "campaigns"
        self.base.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._records: dict[str, CampaignRecord] = {}
        self._poll = poll_interval
        self._validator = validator or self._validate_setup
        for path in self.base.glob("*/campaign.json"):
            try:
                record = CampaignRecord.model_validate_json(path.read_text())
                self._records[record.campaign_id] = record
                if record.state not in TERMINAL:
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
    def _validate_setup(gantry, deck, protocol):
        with tempfile.TemporaryDirectory(prefix="cubos-campaign-check-") as directory:
            paths = []
            for name, content in (("gantry", gantry), ("deck", deck), ("protocol", protocol)):
                path = Path(directory) / f"{name}.yaml"
                path.write_text(content)
                paths.append(str(path))
            result = run_setup_validation(*paths)
            if not result.passed:
                raise ValueError("; ".join(result.errors) or result.output)

    def _preflight(self, spec, bundle):
        gantry, deck, protocol = bundle
        raw = spec.model_dump()
        validate_template(protocol, raw)
        self.runs._validate_bundle(gantry, deck, protocol)
        steps = yaml.safe_load(protocol)["protocol"]
        if not spec.mock_mode and spec.fluid_state_id is None and any(next(iter(s)) in FLUID_COMMANDS for s in steps):
            raise ValueError("Real fluid-handling campaigns require an existing fluid-state ID. Create and seed it in State first.")
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
        preview = compile_trial(protocol, raw, parameters, 0)
        self._validator(gantry, deck, preview)
        return {"parameters": parameters, "protocol_yaml": preview}

    def validate(self, spec):
        try:
            preview = self._preflight(spec, self._bundle(spec))
            return {"valid": True, "errors": [], "preview": preview}
        except (ValueError, OSError) as exc:
            return {"valid": False, "errors": [f"{type(exc).__name__}: {exc}"]}

    @staticmethod
    def _suggest(spec, observations):
        return suggest([p.model_dump() for p in spec.parameters], observations,
                       method=spec.optimizer.method, initial_trials=spec.optimizer.initial_trials,
                       exploration=spec.optimizer.exploration, seed=spec.optimizer.seed,
                       direction=spec.objective.direction,
                       sum_constraint=spec.sum_constraint.model_dump() if spec.sum_constraint else None)

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
            threading.Thread(target=self._loop, args=(campaign_id, bundle), daemon=True,
                             name=f"cubos-campaign-{campaign_id}").start()
            return record.model_copy(deep=True)

    def control(self, campaign_id, action):
        with self._lock:
            record = self._records.get(campaign_id)
            if record is None:
                raise KeyError(campaign_id)
            if record.state in TERMINAL:
                raise RunConflictError("Campaign has already stopped")
            if action == "pause":
                record.pause_requested = True
            elif action == "resume":
                record.pause_requested = False
                if record.state == "paused":
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
            self._save(record)
        return self.get(campaign_id)

    def _finish(self, record, state, reason, error=None):
        record.state, record.stop_reason, record.error = state, reason, error
        record.active_run_id = None
        self._save(record)

    def _loop(self, campaign_id, bundle):
        record = self._records[campaign_id]
        spec = record.spec
        stagnant = 0
        try:
            while True:
                with self._lock:
                    if record.stop_requested:
                        self._finish(record, "stopped", record.stop_reason or "operator_stopped")
                        return
                    if record.pause_requested:
                        if record.state != "paused":
                            record.state = "paused"
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
                                for t in record.trials if t.objective is not None]
                try:
                    parameters = self._suggest(spec, observations)
                except SearchExhaustedError as exc:
                    with self._lock:
                        self._finish(record, "completed", "search_exhausted", str(exc))
                    return
                index = len(record.trials)
                protocol = compile_trial(bundle[2], spec.model_dump(), parameters, index)
                self._validator(bundle[0], bundle[1], protocol)
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
                        trial.objective = extract_result_objective(child.result, spec.objective.path)
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


_manager = None
_manager_lock = threading.Lock()


def get_campaign_manager():
    global _manager
    with _manager_lock:
        settings = get_settings()
        if _manager is None or _manager.base.parent != settings.ensure_run_dir():
            _manager = CampaignManager(settings)
        return _manager
