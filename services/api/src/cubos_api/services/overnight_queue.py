"""Persistent, explicit-start queue for independent color campaigns."""
from __future__ import annotations

import hashlib
import fcntl
import logging
import threading
import time
import tempfile
import uuid
from pathlib import Path
from typing import Callable
from contextlib import contextmanager

from cubos.data import DataStore
from cubos.deck.loader import load_deck_from_yaml
from cubos.gantry.loader import load_gantry_from_yaml
from cubos_api.config import CubOSSettings, get_settings
from cubos_api.models.overnight_queue import (
    OvernightQueueEvent,
    OvernightQueueJob,
    OvernightQueuePrepare,
    OvernightQueueRecord,
    OvernightResourceSummary,
)
from cubos_api.models.state import RunStateSelection
from cubos_api.services.campaign_manager import (
    TERMINAL as CAMPAIGN_TERMINAL,
    CampaignManager,
    get_campaign_manager,
)
from cubos_api.services.color_campaign import build_color_campaign
from cubos_api.services.run_manager import RunConflictError, RunManager, get_run_manager
from cubos_api.services.run_store import _atomic_write
from cubos_api.services.yaml_io import resolve_config_path

log = logging.getLogger(__name__)
QUEUE_TERMINAL = {"completed", "failed", "cancelled", "interrupted", "blocked"}


class OvernightQueueManager:
    def __init__(
        self,
        settings: CubOSSettings,
        campaigns: CampaignManager | None = None,
        runs: RunManager | None = None,
        *,
        poll_interval: float = 0.2,
        builder: Callable = build_color_campaign,
    ):
        self.settings = settings
        self.campaigns = campaigns or get_campaign_manager()
        self.runs = runs or get_run_manager()
        self.base = settings.ensure_run_dir() / "overnight-queues"
        self.base.mkdir(parents=True, exist_ok=True)
        self._poll = poll_interval
        self._builder = builder
        self._lock = threading.RLock()
        self._records: dict[str, OvernightQueueRecord] = {}
        self._workers: set[str] = set()
        for path in self.base.glob("*/queue.json"):
            try:
                record = OvernightQueueRecord.model_validate_json(path.read_text())
                if record.state == "running":
                    record.state = "interrupted"
                    record.stop_reason = "server_restart"
                    record.error = (
                        "Server restarted; inspect the active campaign and physical "
                        "state. This queue will not replay automatically."
                    )
                    record.completed_at = time.time()
                    if record.current_job_index is not None:
                        job = record.jobs[record.current_job_index]
                        if job.state in {"starting", "active"}:
                            job.state = "interrupted"
                            job.stop_reason = "server_restart"
                            job.error = record.error
                            job.completed_at = record.completed_at
                    self._event(record, "queue_interrupted", record.error)
                    self._save(record)
                self._records[record.queue_id] = record
            except (OSError, ValueError):
                log.exception("Cannot recover overnight queue %s", path)

    @contextmanager
    def _claim_lock(self):
        path = self.base / ".queue.lock"
        with path.open("a+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _directory(self, queue_id: str) -> Path:
        return self.base / queue_id

    def _save(self, record: OvernightQueueRecord) -> None:
        record.updated_at = time.time()
        _atomic_write(
            self._directory(record.queue_id) / "queue.json",
            record.model_dump_json(indent=2),
        )

    def _event(
        self,
        record: OvernightQueueRecord,
        kind: str,
        message: str,
        *,
        job_index: int | None = None,
        campaign_id: str | None = None,
        data: dict | None = None,
    ) -> None:
        event = OvernightQueueEvent(
            sequence=len(record.events) + 1,
            timestamp=time.time(),
            kind=kind,
            job_index=job_index,
            campaign_id=campaign_id,
            message=message,
            data=data or {},
        )
        record.events.append(event)
        path = self._directory(record.queue_id) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(event.model_dump_json() + "\n")
            stream.flush()

    def list(self) -> list[OvernightQueueRecord]:
        with self._lock:
            return [
                record.model_copy(deep=True)
                for record in sorted(
                    self._records.values(), key=lambda item: item.created_at, reverse=True,
                )[:100]
            ]

    def latest(self) -> OvernightQueueRecord:
        records = self.list()
        if not records:
            raise KeyError("latest")
        return records[0]

    def get(self, queue_id: str) -> OvernightQueueRecord:
        with self._lock:
            record = self._records.get(queue_id)
            if record is None:
                raise KeyError(queue_id)
            return record.model_copy(deep=True)

    def prepare(self, request: OvernightQueuePrepare) -> OvernightQueueRecord:
        self._check_queue_inventory([job.color_setup for job in request.jobs])
        queue_id = uuid.uuid4().hex
        now = time.time()
        directory = self._directory(queue_id)
        directory.mkdir(parents=True, exist_ok=False)
        jobs: list[OvernightQueueJob] = []
        all_wells: list[str] = []
        tip_count = 0
        for index, incoming in enumerate(request.jobs):
            setup = incoming.color_setup.model_copy(deep=True)
            job_dir = directory / f"job-{index + 1}"
            job_dir.mkdir()
            for category, filename in (
                ("gantry", setup.gantry_file),
                ("deck", setup.deck_file),
                ("protocol", setup.source_protocol_file),
            ):
                source = resolve_config_path(self.settings.configs_dir, category, filename)
                if not source.is_file():
                    raise ValueError(f"Missing {category} file: {filename}")
                content = source.read_text(encoding="utf-8")
                _atomic_write(job_dir / f"{category}.yaml", content)
                _atomic_write(
                    job_dir / f"{category}.sha256",
                    hashlib.sha256(content.encode("utf-8")).hexdigest() + "\n",
                )
            self._validate_job_inputs(setup, job_dir)
            jobs.append(OvernightQueueJob(
                job_id=uuid.uuid4().hex,
                index=index,
                name=incoming.name,
                target_rgb=incoming.target_rgb,
                optimizer_seed=incoming.optimizer_seed,
                color_setup=setup,
                created_at=now,
            ))
            all_wells.extend(setup.candidate_wells)
            batch_count = (
                len(setup.candidate_wells) + setup.batch_size - 1
            ) // setup.batch_size
            tip_count += 3 * batch_count + len(setup.candidate_wells)
        first = jobs[0].color_setup
        record = OvernightQueueRecord(
            queue_id=queue_id,
            name=request.name,
            created_at=now,
            updated_at=now,
            resource_summary=OvernightResourceSummary(
                campaign_count=len(jobs),
                sample_count=len(all_wells),
                tip_count=tip_count,
                total_volume_ul=sum(
                    len(job.color_setup.candidate_wells)
                    * job.color_setup.total_volume_ul
                    for job in jobs
                ),
                maximum_per_stock_ul=sum(
                    job.color_setup.total_volume_ul
                    + 5 * job.color_setup.component_max_ul
                    for job in jobs
                ),
                candidate_wells=all_wells,
                fluid_state_id=first.fluid_state_id,
                gantry_file=first.gantry_file,
                deck_file=first.deck_file,
            ),
            jobs=jobs,
        )
        self._event(record, "queue_prepared", "Queue prepared; no hardware started.")
        self._save(record)
        with self._lock:
            self._records[queue_id] = record
        return record.model_copy(deep=True)

    def start(self, queue_id: str) -> OvernightQueueRecord:
        with self._lock:
            with self._claim_lock():
                path = self._directory(queue_id) / "queue.json"
                if not path.is_file():
                    raise KeyError(queue_id)
                record = OvernightQueueRecord.model_validate_json(path.read_text())
                if record.state != "prepared":
                    raise RunConflictError("Only a prepared overnight queue can be started")
                self._check_queue_inventory([job.color_setup for job in record.jobs])
                for candidate in self.base.glob("*/queue.json"):
                    other = OvernightQueueRecord.model_validate_json(candidate.read_text())
                    if other.queue_id != queue_id and other.state == "running":
                        raise RunConflictError("Another overnight queue is active")
                if any(item.state not in CAMPAIGN_TERMINAL for item in self.campaigns.list()):
                    raise RunConflictError("An active-learning campaign is already active")
                record.state = "running"
                record.started_at = time.time()
                record.current_job_index = 0
                self._event(record, "queue_started", "Operator started the overnight queue.")
                self._save(record)
                self._records[queue_id] = record
            self._workers.add(queue_id)
            threading.Thread(
                target=self._loop,
                args=(queue_id,),
                daemon=True,
                name=f"cubos-overnight-{queue_id}",
            ).start()
            return record.model_copy(deep=True)

    def cancel(self, queue_id: str) -> OvernightQueueRecord:
        active_campaign_id = None
        with self._lock:
            record = self._records.get(queue_id)
            if record is None:
                raise KeyError(queue_id)
            if record.state in QUEUE_TERMINAL:
                raise RunConflictError("Overnight queue has already stopped")
            if record.state == "prepared":
                record.cancel_requested = True
                for job in record.jobs:
                    job.state = "cancelled"
                    job.stop_reason = "operator_cancelled"
                    job.completed_at = time.time()
                self._stop(record, "cancelled", "operator_cancelled")
                return record.model_copy(deep=True)
            record.cancel_requested = True
            for job in record.jobs:
                if job.state == "active":
                    active_campaign_id = job.campaign_id
                elif job.state == "pending":
                    job.state = "cancelled"
                    job.stop_reason = "operator_cancelled"
                    job.completed_at = time.time()
            self._event(record, "cancel_requested", "Operator cancelled the queue.")
            self._save(record)
        if active_campaign_id is not None:
            self.campaigns.control(active_campaign_id, "cancel")
        return self.get(queue_id)

    def _check_queue_inventory(self, setups) -> None:
        first = setups[0]
        if first.fluid_state_id is None:
            return
        deck_path = resolve_config_path(
            self.settings.configs_dir, "deck", first.deck_file,
        )
        deck_yaml = deck_path.read_text(encoding="utf-8")
        self.runs._resolve_run_state(
            deck_yaml, RunStateSelection(fluid_state_id=first.fluid_state_id),
        )
        store = DataStore(self.settings.data_db_path)
        try:
            tips = store.get_tip_snapshot(first.fluid_state_id)
            fluids = store.get_fluid_snapshot(first.fluid_state_id)
        finally:
            store.close()
        available_tips = {
            (item["rack_key"], item["slot_id"])
            for item in tips["containers"] if item["status"] == "available"
        }
        required_tips = sum(
            3 * ((len(setup.candidate_wells) + setup.batch_size - 1) // setup.batch_size)
            + len(setup.candidate_wells)
            for setup in setups
        )
        if len(available_tips) < required_tips:
            raise ValueError(
                f"Overnight queue needs {required_tips} distinct available tips; "
                f"durable state {first.fluid_state_id} has {len(available_tips)}"
            )
        containers = {
            f"{item['labware_key']}.{item['location_id']}"
            if item["location_id"] else item["labware_key"]: item
            for item in fluids["containers"]
        }
        requirements: dict[str, float] = {}
        for setup in setups:
            for source in (setup.red_source, setup.yellow_source, setup.blue_source):
                requirements[source] = requirements.get(source, 0.0) + (
                    setup.total_volume_ul + 5 * setup.component_max_ul
                )
        deck = load_deck_from_yaml(deck_path)
        for source, required in requirements.items():
            container = containers.get(source)
            if container is None:
                raise ValueError(f"Durable state has no queued stock source {source}")
            resolved = deck.resolve_labware_target(source)
            labware = resolved.labware
            if resolved.location_id is not None and hasattr(labware, "vials"):
                labware = labware.vials[resolved.location_id]
            dead = float(getattr(labware, "dead_volume_ul", 0.0))
            usable = max(0.0, float(container["current_volume_ul"]) - dead)
            if usable + 1e-6 < required:
                raise ValueError(
                    f"Overnight queue needs {required:g} uL usable from {source}; "
                    f"durable state has {usable:g} uL after {dead:g} uL dead volume"
                )

    def _available_tips(self, setup) -> list[str] | None:
        if setup.fluid_state_id is None:
            return None
        deck_yaml = (self.settings.configs_dir / "deck" / setup.deck_file).read_text()
        self.runs._resolve_run_state(
            deck_yaml, RunStateSelection(fluid_state_id=setup.fluid_state_id),
        )
        store = DataStore(self.settings.data_db_path)
        try:
            snapshot = store.get_tip_snapshot(setup.fluid_state_id)
        finally:
            store.close()
        pipette = snapshot["pipette"]
        if pipette["attachment_uncertain"]:
            raise ValueError("The durable state has an uncertain pipette attachment")
        if pipette["tip_extension_mm"] is not None:
            raise ValueError("The durable state must record a bare pipette")
        return [
            f"{item['rack_key']}.{item['slot_id']}"
            for item in snapshot["containers"]
            if item["status"] == "available"
        ]

    def _validate_job_inputs(self, setup, job_dir: Path) -> None:
        """Compile and validate every batch without reserving or starting hardware."""
        with tempfile.TemporaryDirectory(prefix="cubos-overnight-prepare-") as output:
            spec = self._builder(
                setup,
                Path(output),
                available_tip_positions=self._available_tips(setup),
                source_protocol_yaml=(job_dir / "protocol.yaml").read_text(encoding="utf-8"),
                gantry_config=(
                    load_gantry_from_yaml(job_dir / "gantry.yaml")
                    if setup.photo_position is not None else None
                ),
            )
            protocol_yaml = (Path(output) / spec.protocol_file).read_text(encoding="utf-8")
            self.campaigns._preflight(
                spec,
                (
                    (job_dir / "gantry.yaml").read_text(encoding="utf-8"),
                    (job_dir / "deck.yaml").read_text(encoding="utf-8"),
                    protocol_yaml,
                ),
            )

    def _materialize_snapshot(self, record, job, category: str) -> str:
        source = self._directory(record.queue_id) / f"job-{job.index + 1}" / f"{category}.yaml"
        filename = f"overnight_{record.queue_id}_{job.index + 1}_{category}.yaml"
        destination = self.settings.configs_dir / category / filename
        if destination.exists():
            expected = source.read_text(encoding="utf-8")
            if destination.read_text(encoding="utf-8") != expected:
                raise ValueError(f"Immutable {category} snapshot collision: {filename}")
        else:
            _atomic_write(destination, source.read_text(encoding="utf-8"))
        return filename

    def _build_spec(self, record, job):
        setup = job.color_setup.model_copy(update={
            "gantry_file": self._materialize_snapshot(record, job, "gantry"),
            "deck_file": self._materialize_snapshot(record, job, "deck"),
            "source_protocol_file": self._materialize_snapshot(record, job, "protocol"),
        })
        job_dir = self._directory(record.queue_id) / f"job-{job.index + 1}"
        spec = self._builder(
            setup,
            self.settings.configs_dir / "protocol",
            available_tip_positions=self._available_tips(setup),
            source_protocol_yaml=(job_dir / "protocol.yaml").read_text(encoding="utf-8"),
            gantry_config=(
                load_gantry_from_yaml(job_dir / "gantry.yaml")
                if setup.photo_position is not None else None
            ),
        )
        expected_seeds = [
            {"red_ul": 100.0, "yellow_ul": 25.0, "blue_ul": 25.0},
            {"red_ul": 25.0, "yellow_ul": 100.0, "blue_ul": 25.0},
            {"red_ul": 25.0, "yellow_ul": 25.0, "blue_ul": 100.0},
        ]
        if spec.optimizer.initial_points != expected_seeds:
            raise ValueError(
                "Overnight color campaigns require the three approved dominant seeds"
            )
        return spec.model_copy(update={
            "name": job.name,
            "optimizer": spec.optimizer.model_copy(update={
                "seed": job.optimizer_seed,
                "initial_trials": 3,
            }),
            "stop": spec.stop.model_copy(update={
                "max_trials": 8,
                "target_value": 2.0,
                "patience": 0,
            }),
        })

    @staticmethod
    def _best_parameters(campaign) -> dict[str, float] | None:
        accepted = [
            trial for trial in campaign.trials
            if trial.objective is not None and trial.objective_status == "accepted"
        ]
        if not accepted:
            return None
        key = lambda trial: trial.objective
        best = (
            min(accepted, key=key)
            if campaign.spec.objective.direction == "minimize"
            else max(accepted, key=key)
        )
        return dict(best.parameters)

    def _stop(self, record, state, reason, error=None):
        record.state = state
        record.stop_reason = reason
        record.error = error
        record.completed_at = time.time()
        self._event(record, f"queue_{state}", error or reason)
        self._save(record)

    def _loop(self, queue_id: str) -> None:
        record = self._records[queue_id]
        active_campaign_id = None
        try:
            for job in record.jobs:
                with self._lock:
                    if record.cancel_requested:
                        self._stop(record, "cancelled", "operator_cancelled")
                        return
                    record.current_job_index = job.index
                    job.state = "starting"
                    job.started_at = time.time()
                    self._event(
                        record, "job_starting", f"Preparing {job.name} from fresh state.",
                        job_index=job.index,
                    )
                    self._save(record)
                with self._lock:
                    spec = self._build_spec(record, job)
                    if record.cancel_requested:
                        job.state = "cancelled"
                        job.stop_reason = "operator_cancelled"
                        job.completed_at = time.time()
                        self._stop(record, "cancelled", "operator_cancelled")
                        return
                    # Keep the queue claim and campaign creation in one critical
                    # section so a concurrent cancel cannot slip between them.
                    campaign = self.campaigns.start(spec)
                    active_campaign_id = campaign.campaign_id
                    job.campaign_id = campaign.campaign_id
                    job.state = "active"
                    self._event(
                        record, "job_started", f"Started {job.name}.",
                        job_index=job.index, campaign_id=campaign.campaign_id,
                    )
                    self._save(record)
                while True:
                    campaign = self.campaigns.get(campaign.campaign_id)
                    if campaign.state in CAMPAIGN_TERMINAL or campaign.state == "awaiting_refill":
                        break
                    time.sleep(self._poll)
                with self._lock:
                    job.best_objective = campaign.best_objective
                    job.best_parameters = self._best_parameters(campaign)
                    job.trials_completed = len([
                        trial for trial in campaign.trials
                        if trial.objective_status == "accepted"
                    ])
                    job.stop_reason = campaign.stop_reason
                    job.error = campaign.error
                    job.completed_at = time.time()
                    if record.cancel_requested:
                        job.state = "cancelled"
                        self._stop(record, "cancelled", "operator_cancelled")
                        return
                    if campaign.state == "completed":
                        active_campaign_id = None
                        job.state = "completed"
                        self._event(
                            record, "job_completed", f"Completed {job.name}.",
                            job_index=job.index, campaign_id=campaign.campaign_id,
                            data={
                                "best_objective": campaign.best_objective,
                                "best_parameters": job.best_parameters,
                                "trials_completed": job.trials_completed,
                                "stop_reason": campaign.stop_reason,
                            },
                        )
                        self._save(record)
                        continue
                    if campaign.state == "awaiting_refill":
                        active_campaign_id = None
                        job.state = "blocked"
                        job.stop_reason = "inventory_refill"
                        self._stop(
                            record, "blocked", "inventory_refill",
                            "Inventory refill is required; inspect and reconcile before a new queue.",
                        )
                        return
                    job.state = "interrupted" if campaign.state == "interrupted" else "failed"
                    active_campaign_id = None
                    self._stop(
                        record,
                        "interrupted" if campaign.state == "interrupted" else "failed",
                        campaign.stop_reason or "campaign_failed",
                        campaign.error,
                    )
                    return
            with self._lock:
                record.current_job_index = None
                self._stop(record, "completed", "all_targets_completed")
        except Exception as exc:
            log.exception("Overnight queue %s failed", queue_id)
            with self._lock:
                cancellation_error = None
                if active_campaign_id is not None:
                    try:
                        active = self.campaigns.get(active_campaign_id)
                        if active.state not in CAMPAIGN_TERMINAL:
                            self.campaigns.control(active_campaign_id, "cancel")
                    except Exception as cancel_exc:
                        cancellation_error = (
                            f"{type(cancel_exc).__name__}: {cancel_exc}"
                        )
                index = record.current_job_index
                if index is not None:
                    job = record.jobs[index]
                    if active_campaign_id is not None:
                        job.campaign_id = active_campaign_id
                    if job.state in {"starting", "active"}:
                        job.state = "blocked" if cancellation_error else "failed"
                        job.stop_reason = "queue_error"
                        job.error = f"{type(exc).__name__}: {exc}"
                        job.completed_at = time.time()
                self._stop(
                    record,
                    "blocked" if cancellation_error else "failed",
                    "active_campaign_cancel_unconfirmed" if cancellation_error else "queue_error",
                    (
                        f"{type(exc).__name__}: {exc}; active campaign "
                        f"{active_campaign_id} cancellation was not confirmed: "
                        f"{cancellation_error}"
                        if cancellation_error
                        else f"{type(exc).__name__}: {exc}"
                    ),
                )
        finally:
            self._workers.discard(queue_id)


_manager = None
_manager_lock = threading.Lock()


def get_overnight_queue_manager() -> OvernightQueueManager:
    global _manager
    with _manager_lock:
        settings = get_settings()
        expected = settings.ensure_run_dir() / "overnight-queues"
        if _manager is None or _manager.base != expected:
            _manager = OvernightQueueManager(settings)
        return _manager


def reset_overnight_queue_manager() -> None:
    global _manager
    with _manager_lock:
        _manager = None
