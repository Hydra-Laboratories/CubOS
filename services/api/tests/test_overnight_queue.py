from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from cubos_api.config import CubOSSettings
from cubos_api.models.overnight_queue import OvernightQueuePrepare
from cubos_api.services.overnight_queue import OvernightQueueManager
from cubos_api.services.run_manager import RunConflictError


TARGETS = ["#508D31", "#3D7189", "#B4298A", "#A44741", "#D5C426"]
WELLS = [
    [f"plate.{row}{column}" for row in "ABCDEFGH"]
    for column in range(4, 9)
]


def _rgb(value):
    return tuple(float(int(value[index:index + 2], 16)) for index in (1, 3, 5))


def _request() -> OvernightQueuePrepare:
    jobs = []
    for index, (target, wells) in enumerate(zip(TARGETS, WELLS)):
        rgb = _rgb(target)
        jobs.append({
            "name": f"target-{index + 1}",
            "target_rgb": rgb,
            "optimizer_seed": 20261005 + index,
            "color_setup": {
                "gantry_file": "gantry.yaml",
                "deck_file": "deck.yaml",
                "source_protocol_file": "source.yaml",
                "batch_size": 3,
                "target_mode": "rgb",
                "target_rgb": rgb,
                "target_well": "plate.A1",
                "red_source": "stocks.A1",
                "yellow_source": "stocks.A2",
                "blue_source": "stocks.A3",
                "component_min_ul": 25.0,
                "component_max_ul": 100.0,
                "total_volume_ul": 150.0,
                "candidate_wells": wells,
                "expected_center": (0.5, 0.5),
                "expected_center_source": "frame_center",
                "fluid_state_id": 10,
                "mock_mode": False,
            },
        })
    return OvernightQueuePrepare(name="five targets", jobs=jobs)


class _Optimizer:
    seed = 7
    initial_trials = 6
    initial_points = [
        {"red_ul": 100.0, "yellow_ul": 25.0, "blue_ul": 25.0},
        {"red_ul": 25.0, "yellow_ul": 100.0, "blue_ul": 25.0},
        {"red_ul": 25.0, "yellow_ul": 25.0, "blue_ul": 100.0},
        {"red_ul": 62.5, "yellow_ul": 62.5, "blue_ul": 25.0},
        {"red_ul": 62.5, "yellow_ul": 25.0, "blue_ul": 62.5},
        {"red_ul": 25.0, "yellow_ul": 62.5, "blue_ul": 62.5},
    ]

    def model_copy(self, update=None):
        clone = _Optimizer()
        for name, value in (update or {}).items():
            setattr(clone, name, value)
        return clone


class _Stop:
    def model_copy(self, update=None):
        clone = _Stop()
        for name, value in (update or {}).items():
            setattr(clone, name, value)
        return clone


class _Spec:
    optimizer = _Optimizer()
    stop = _Stop()

    def model_copy(self, update=None):
        clone = _Spec()
        for name, value in (update or {}).items():
            setattr(clone, name, value)
        return clone


class _Runs:
    def _resolve_run_state(self, *_args, **_kwargs):
        return None


class _Campaigns:
    def __init__(self, outcomes=None, gate=None):
        self.outcomes = list(outcomes or ["completed"] * 5)
        self.records = {}
        self.started = []
        self.cancelled = []
        self.gate = gate

    def list(self):
        return list(self.records.values())

    def start(self, spec):
        campaign_id = f"campaign-{len(self.started) + 1}"
        outcome = self.outcomes[len(self.started)]
        record = SimpleNamespace(
            campaign_id=campaign_id,
            state="running" if self.gate else outcome,
            best_objective=1.5,
            stop_reason="trial_budget" if outcome == "completed" else outcome,
            error=None if outcome == "completed" else "physical failure",
            trials=[],
            spec=SimpleNamespace(objective=SimpleNamespace(direction="minimize")),
        )
        self.records[campaign_id] = record
        self.started.append(spec)
        return record

    def get(self, campaign_id):
        record = self.records[campaign_id]
        if self.gate and self.gate.is_set():
            record.state = "completed"
            record.stop_reason = "trial_budget"
        return record

    def control(self, campaign_id, action):
        self.cancelled.append((campaign_id, action))
        record = self.records[campaign_id]
        record.state = "stopped"
        record.stop_reason = "operator_cancelled"
        return record


def _manager(tmp_path, campaigns, builder=lambda *_a, **_k: _Spec()):
    configs = tmp_path / "configs"
    for category, filename in (
        ("gantry", "gantry.yaml"),
        ("deck", "deck.yaml"),
        ("protocol", "source.yaml"),
    ):
        path = configs / category
        path.mkdir(parents=True, exist_ok=True)
        (path / filename).write_text("protocol: []\n" if category == "protocol" else "{}\n")
    settings = CubOSSettings(
        config_dir=configs,
        run_dir=tmp_path / "runs",
        data_db_path=tmp_path / "data.db",
    )
    manager = OvernightQueueManager(
        settings, campaigns, _Runs(), poll_interval=0.001, builder=builder,
    )
    manager._available_tips = lambda _setup: [f"tips.A{i}" for i in range(1, 97)]
    manager._validate_job_inputs = lambda _setup, _job_dir: None
    manager._check_queue_inventory = lambda _setups, **_kwargs: None
    return manager


def _wait(manager, queue_id, states):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        record = manager.get(queue_id)
        if record.state in states:
            return record
        time.sleep(0.002)
    raise AssertionError(manager.get(queue_id))


def test_prepare_persists_immutable_inputs_without_starting_hardware(tmp_path):
    campaigns = _Campaigns()
    manager = _manager(tmp_path, campaigns)
    record = manager.prepare(_request())

    assert record.state == "prepared"
    assert campaigns.started == []
    assert record.resource_summary.sample_count == 40
    assert record.resource_summary.tip_count == 85
    assert record.resource_summary.total_volume_ul == 6000.0
    assert record.resource_summary.maximum_per_stock_ul == 3250.0
    assert len(set(record.resource_summary.candidate_wells)) == 40
    assert (manager.base / record.queue_id / "job-1" / "gantry.yaml").is_file()


def test_runs_five_campaigns_sequentially_and_trial_budget_is_success(tmp_path):
    campaigns = _Campaigns()
    manager = _manager(tmp_path, campaigns)
    queue = manager.prepare(_request())
    manager.start(queue.queue_id)
    final = _wait(manager, queue.queue_id, {"completed", "failed"})

    assert final.state == "completed"
    assert final.stop_reason == "all_targets_completed"
    assert [job.state for job in final.jobs] == ["completed"] * 5
    assert len(campaigns.started) == 5
    assert [spec.optimizer.seed for spec in campaigns.started] == [
        20261005, 20261006, 20261007, 20261008, 20261009,
    ]
    assert all(len(spec.optimizer.initial_points) == 3 for spec in campaigns.started)


def test_allocates_tips_from_fresh_state_for_each_job(tmp_path):
    campaigns = _Campaigns()
    seen_tips = []
    seen_sources = []

    def builder(setup, *_args, available_tip_positions, **_kwargs):
        seen_tips.append(list(available_tip_positions))
        seen_sources.append(setup.source_protocol_file)
        return _Spec()

    manager = _manager(tmp_path, campaigns, builder=builder)
    snapshots = iter([
        [f"tips.A{i}"] for i in range(1, 6)
    ])
    manager._available_tips = lambda _setup: next(snapshots)
    queue = manager.prepare(_request())
    manager.start(queue.queue_id)
    final = _wait(manager, queue.queue_id, {"completed", "failed"})

    assert final.state == "completed"
    assert seen_tips == [[f"tips.A{i}"] for i in range(1, 6)]
    assert all(name.startswith(f"overnight_{queue.queue_id}_") for name in seen_sources)


def test_failure_stops_before_starting_later_jobs(tmp_path):
    campaigns = _Campaigns(["completed", "failed", "completed", "completed", "completed"])
    manager = _manager(tmp_path, campaigns)
    queue = manager.prepare(_request())
    manager.start(queue.queue_id)
    final = _wait(manager, queue.queue_id, {"failed"})

    assert len(campaigns.started) == 2
    assert final.jobs[1].state == "failed"
    assert all(job.state == "pending" for job in final.jobs[2:])


def test_cancel_delegates_active_campaign_and_skips_future_jobs(tmp_path):
    gate = threading.Event()
    campaigns = _Campaigns(gate=gate)
    manager = _manager(tmp_path, campaigns)
    queue = manager.prepare(_request())
    manager.start(queue.queue_id)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not campaigns.started:
        time.sleep(0.002)
    manager.cancel(queue.queue_id)
    final = _wait(manager, queue.queue_id, {"cancelled"})

    assert campaigns.cancelled == [("campaign-1", "cancel")]
    assert final.jobs[0].state == "cancelled"
    assert [job.state for job in final.jobs[1:]] == ["cancelled"] * 4


def test_double_start_is_rejected(tmp_path):
    gate = threading.Event()
    campaigns = _Campaigns(gate=gate)
    manager = _manager(tmp_path, campaigns)
    queue = manager.prepare(_request())
    manager.start(queue.queue_id)
    with pytest.raises(RunConflictError):
        manager.start(queue.queue_id)
    gate.set()
    _wait(manager, queue.queue_id, {"completed"})


def test_restart_marks_running_queue_interrupted_without_replay(tmp_path):
    gate = threading.Event()
    campaigns = _Campaigns(gate=gate)
    first = _manager(tmp_path, campaigns)
    queue = first.prepare(_request())
    first.start(queue.queue_id)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not campaigns.started:
        time.sleep(0.002)

    recovered_campaigns = _Campaigns()
    recovered = OvernightQueueManager(
        first.settings, recovered_campaigns, _Runs(), poll_interval=0.001,
    )
    record = recovered.get(queue.queue_id)
    assert record.state == "interrupted"
    assert record.stop_reason == "server_restart"
    assert recovered_campaigns.started == []
    gate.set()


def test_restart_keeps_untouched_prepared_queue_startable(tmp_path):
    first = _manager(tmp_path, _Campaigns())
    queue = first.prepare(_request())

    recovered = OvernightQueueManager(
        first.settings, _Campaigns(), _Runs(), poll_interval=0.001,
    )
    record = recovered.get(queue.queue_id)
    assert record.state == "prepared"
    assert record.stop_reason is None


def test_multiple_prepared_queues_do_not_block_selected_start(tmp_path):
    gate = threading.Event()
    manager = _manager(tmp_path, _Campaigns(gate=gate))
    first = manager.prepare(_request())
    second = manager.prepare(_request())

    started = manager.start(second.queue_id)
    assert started.state == "running"
    assert manager.get(first.queue_id).state == "prepared"
    gate.set()
    _wait(manager, second.queue_id, {"completed"})


def test_failure_after_campaign_start_cancels_active_campaign(tmp_path):
    gate = threading.Event()
    campaigns = _Campaigns(gate=gate)
    manager = _manager(tmp_path, campaigns)
    original_event = manager._event
    raised = False

    def fail_once(record, kind, message, **kwargs):
        nonlocal raised
        if kind == "job_started" and not raised:
            raised = True
            raise OSError("injected queue persistence failure")
        return original_event(record, kind, message, **kwargs)

    manager._event = fail_once
    queue = manager.prepare(_request())
    manager.start(queue.queue_id)
    final = _wait(manager, queue.queue_id, {"failed", "blocked"})

    assert final.state == "failed"
    assert final.jobs[0].campaign_id == "campaign-1"
    assert campaigns.cancelled == [("campaign-1", "cancel")]


def test_cancel_prepared_queue_terminalizes_without_start(tmp_path):
    manager = _manager(tmp_path, _Campaigns())
    queue = manager.prepare(_request())
    cancelled = manager.cancel(queue.queue_id)
    assert cancelled.state == "cancelled"
    assert [job.state for job in cancelled.jobs] == ["cancelled"] * 5
    with pytest.raises(RunConflictError):
        manager.start(queue.queue_id)


@pytest.mark.parametrize("tip_count", [17, 84])
def test_queue_inventory_rejects_fewer_than_85_distinct_tips(tmp_path, monkeypatch, tip_count):
    manager = _manager(tmp_path, _Campaigns())
    setups = [job.color_setup for job in _request().jobs]
    class Store:
        def __init__(self, _path): pass
        def close(self): pass
        def get_tip_snapshot(self, _state):
            return {"containers": [
                {"rack_key": "tips", "slot_id": str(i), "status": "available"}
                for i in range(tip_count)
            ]}
        def get_fluid_snapshot(self, _state): return {"containers": []}
    monkeypatch.setattr("cubos_api.services.overnight_queue.DataStore", Store)
    with pytest.raises(ValueError, match="85 distinct available tips"):
        OvernightQueueManager._check_queue_inventory(manager, setups)


@pytest.mark.parametrize("volume,passes", [(3249.0, False), (3250.0, True)])
def test_queue_inventory_checks_aggregate_stock_budget(tmp_path, monkeypatch, volume, passes):
    manager = _manager(tmp_path, _Campaigns())
    setups = [job.color_setup for job in _request().jobs]
    class Store:
        def __init__(self, _path): pass
        def close(self): pass
        def get_tip_snapshot(self, _state):
            return {"containers": [
                {"rack_key": "tips", "slot_id": str(i), "status": "available"}
                for i in range(85)
            ]}
        def get_fluid_snapshot(self, _state):
            return {"containers": [
                {"labware_key": "stocks", "location_id": slot, "current_volume_ul": volume}
                for slot in ("A1", "A2", "A3")
            ]}
    class Deck:
        def resolve_labware_target(self, _source):
            return SimpleNamespace(labware=SimpleNamespace(dead_volume_ul=0.0), location_id=None)
    monkeypatch.setattr("cubos_api.services.overnight_queue.DataStore", Store)
    monkeypatch.setattr("cubos_api.services.overnight_queue.load_deck_from_yaml", lambda _path: Deck())
    if passes:
        OvernightQueueManager._check_queue_inventory(manager, setups)
    else:
        with pytest.raises(ValueError, match="3250 uL usable"):
            OvernightQueueManager._check_queue_inventory(manager, setups)


def test_queue_inventory_subtracts_configured_dead_volume(tmp_path, monkeypatch):
    manager = _manager(tmp_path, _Campaigns())
    setups = [job.color_setup for job in _request().jobs]
    class Store:
        def __init__(self, _path): pass
        def close(self): pass
        def get_tip_snapshot(self, _state):
            return {"containers": [
                {"rack_key": "tips", "slot_id": str(i), "status": "available"}
                for i in range(85)
            ]}
        def get_fluid_snapshot(self, _state):
            return {"containers": [
                {"labware_key": "stocks", "location_id": slot, "current_volume_ul": 3300.0}
                for slot in ("A1", "A2", "A3")
            ]}
    class Deck:
        def resolve_labware_target(self, _source):
            return SimpleNamespace(labware=SimpleNamespace(dead_volume_ul=51.0), location_id=None)
    monkeypatch.setattr("cubos_api.services.overnight_queue.DataStore", Store)
    monkeypatch.setattr("cubos_api.services.overnight_queue.load_deck_from_yaml", lambda _path: Deck())
    with pytest.raises(ValueError, match="after 51 uL dead volume"):
        OvernightQueueManager._check_queue_inventory(manager, setups)


def _failed_exposure_queue(tmp_path):
    campaigns = _Campaigns(["completed", "failed", "completed", "completed", "completed"])
    manager = _manager(tmp_path, campaigns)
    queue = manager.prepare(_request())
    manager.start(queue.queue_id)
    _wait(manager, queue.queue_id, {"failed"})
    deadline = time.monotonic() + 2
    while queue.queue_id in manager._workers and time.monotonic() < deadline:
        time.sleep(.001)
    record = manager._records[queue.queue_id]
    record.stop_reason = record.jobs[1].stop_reason = "batch_objective_rejected"
    record.jobs[0].trials_completed = 8
    record.jobs[1].trials_completed = 5
    failed = campaigns.records[record.jobs[1].campaign_id]
    failed.stop_reason = "batch_objective_rejected"
    failed.trials = [SimpleNamespace(objective=1.5, objective_status="accepted", parameters={}) for _ in range(5)] + [SimpleNamespace(objective=None, objective_status="rejected", parameters={})]
    manager._save(record)
    campaigns.resumed = []
    def resume(campaign_id):
        campaigns.resumed.append(campaign_id)
        failed.state = "completed"
        failed.stop_reason = "trial_budget"
        return failed
    campaigns.resume_underexposed = resume
    return manager, campaigns, queue.queue_id


def test_resume_keeps_completed_job_and_existing_campaign(tmp_path):
    manager, campaigns, queue_id = _failed_exposure_queue(tmp_path)
    original = manager.get(queue_id).jobs[0]
    manager.resume_underexposed(queue_id)
    final = _wait(manager, queue_id, {"completed", "failed"})
    assert final.state == "completed"
    assert final.jobs[0] == original
    assert campaigns.resumed == ["campaign-2"]
    assert final.jobs[1].campaign_id == "campaign-2"
    assert len(campaigns.started) == 5
    assert all(spec.skip_underexposed for spec in campaigns.started[2:])
    assert final.jobs[1].trials_completed == 5
    assert final.jobs[1].trials_attempted == 6
    assert final.jobs[1].unscored_count == 1
    with pytest.raises(RunConflictError):
        manager.resume_underexposed(queue_id)


@pytest.mark.parametrize("guard", ["physical", "snapshot", "other_queue", "history", "restart"])
def test_resume_guards_leave_failed_queue_unchanged(tmp_path, guard):
    manager, campaigns, queue_id = _failed_exposure_queue(tmp_path)
    if guard == "physical":
        def reject(_campaign_id):
            raise RunConflictError("Native run failed")
        campaigns.resume_underexposed = reject
    elif guard == "snapshot":
        (manager.base / queue_id / "job-2" / "deck.yaml").write_text("changed")
    elif guard == "other_queue":
        other = manager.prepare(_request())
        record = manager._records[other.queue_id]
        record.state = "running"
        manager._save(record)
    elif guard == "history":
        record = manager._records[queue_id]
        record.jobs[0].state = "pending"
        manager._save(record)
    else:
        record = manager._records[queue_id]
        record.state = "interrupted"
        record.stop_reason = "server_restart"
        manager._save(record)
    before = (manager.base / queue_id / "queue.json").read_text()
    with pytest.raises((RunConflictError, ValueError)):
        manager.resume_underexposed(queue_id)
    assert (manager.base / queue_id / "queue.json").read_text() == before
    assert campaigns.resumed == []
    assert len(campaigns.started) == 2


def test_resume_cancel_and_duplicate_do_not_start_later_jobs(tmp_path):
    manager, campaigns, queue_id = _failed_exposure_queue(tmp_path)
    def resume(campaign_id):
        campaigns.resumed.append(campaign_id)
        campaign = campaigns.records[campaign_id]
        campaign.state = "running"
        return campaign
    campaigns.resume_underexposed = resume
    manager.resume_underexposed(queue_id)
    with pytest.raises(RunConflictError):
        manager.resume_underexposed(queue_id)
    manager.cancel(queue_id)
    final = _wait(manager, queue_id, {"cancelled"})
    assert campaigns.cancelled == [("campaign-2", "cancel")]
    assert len(campaigns.started) == 2
    assert final.jobs[0].state == "completed"
    assert all(job.state == "cancelled" for job in final.jobs[1:])


@pytest.mark.parametrize("cancel_fails", [False, True])
def test_resume_persistence_failure_cancels_existing_campaign(tmp_path, cancel_fails):
    manager, campaigns, queue_id = _failed_exposure_queue(tmp_path)
    save = manager._save
    failed_once = False
    def fail_once(record):
        nonlocal failed_once
        if record.state == "running" and not failed_once:
            failed_once = True
            raise OSError("queue persistence unavailable")
        save(record)
    manager._save = fail_once
    if cancel_fails:
        def reject_cancel(campaign_id, action):
            campaigns.cancelled.append((campaign_id, action))
            raise OSError("cancel unavailable")
        campaigns.control = reject_cancel
    with pytest.raises(ValueError, match="queue persistence unavailable"):
        manager.resume_underexposed(queue_id)
    result = manager.get(queue_id)
    assert result.state == ("blocked" if cancel_fails else "failed")
    assert campaigns.cancelled == [("campaign-2", "cancel")]
    assert campaigns.resumed == ["campaign-2"]
    assert len(campaigns.started) == 2
    assert result.jobs[1].campaign_id == "campaign-2"
    assert result.jobs[0].state == "completed"
    assert all(job.state == "pending" for job in result.jobs[2:])
    assert queue_id not in manager._workers
