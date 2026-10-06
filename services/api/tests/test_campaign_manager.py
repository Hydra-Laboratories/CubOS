import time
from pathlib import Path

import pytest

from cubos_api.models.campaigns import CampaignRecord, CampaignSpec, PendingBatch
from cubos_api.models.runs import RunRecord
from cubos_api.services.campaign_manager import CampaignManager
from cubos_api.services.run_manager import RunConflictError
from cubos_api.config import CubOSSettings


PROTOCOL = """protocol:
  - measure:
      instrument: asmi
      position: plate.A1
      measurement_height: 0
      method_kwargs:
        value: 0.0
"""


class ControlledClock:
    def __init__(self):
        self.value = 100.0

    def time(self):
        return self.value

    def sleep(self, seconds):
        time.sleep(seconds)

    def advance(self, seconds):
        self.value += seconds


class FakeRuns:
    def __init__(self, outcomes=None, hold=False, fail_indices=None, on_complete=None):
        self.records = {}
        self.outcomes = list(outcomes or [1.0])
        self.submissions = []
        self.cancels = []
        self.owner = None
        self.hold = hold
        self.fail_indices = set(fail_indices or ())
        self.on_complete = on_complete
        self.state_resolutions = []
        import threading
        self.release_event = threading.Event()

    @property
    def active_run_id(self):
        return next((k for k, v in self.records.items() if v.state in {"queued", "running", "cancel_requested"}), None)

    def _validate_bundle(self, *args):
        return None

    def _resolve_run_state(self, deck_yaml, state):
        self.state_resolutions.append((deck_yaml, state.fluid_state_id))
        return state.fluid_state_id

    @property
    def campaign_owner(self):
        return self.owner

    def reserve_campaign(self, campaign_id):
        if self.owner is not None or self.active_run_id is not None:
            raise RunConflictError("busy")
        self.owner = campaign_id

    def release_campaign(self, campaign_id):
        if self.owner == campaign_id:
            self.owner = None

    def submit(self, submission, *, campaign_owner=None):
        if campaign_owner != self.owner:
            raise RunConflictError("wrong owner")
        index = len(self.submissions)
        record = RunRecord(run_id=submission.run_id, state="running", created_at=time.time(), mock_mode=True)
        self.records[record.run_id] = record
        self.submissions.append(submission)

        def finish():
            if self.hold:
                self.release_event.wait(10)
            else:
                time.sleep(0.01)
            if self.on_complete is not None:
                self.on_complete(index)
            record.state = "succeeded"
            if index in self.fail_indices:
                record.state = "failed"
                record.error = "native failure"
            else:
                record.result = {"results": [{"value": self.outcomes[index]}]} if index < len(self.outcomes) else None
            self.records[record.run_id] = record

        import threading
        threading.Thread(target=finish, daemon=True).start()
        return record

    def get(self, run_id):
        return self.records.get(run_id)

    def cancel(self, run_id):
        self.cancels.append(run_id)
        self.records[run_id].state = "cancelled"
        return self.records[run_id]


def _setup(tmp_path: Path, *, mock=True, max_trials=3, objective_path="results.0.value", **stop):
    config = tmp_path / "configs"
    for category in ("gantry", "deck", "protocol"):
        (config / category).mkdir(parents=True)
    (config / "gantry" / "g.yaml").write_text("gantry: {}")
    (config / "deck" / "d.yaml").write_text("deck: {}")
    (config / "protocol" / "p.yaml").write_text(PROTOCOL)
    settings = CubOSSettings(config_dir=config, run_dir=tmp_path / "runs", allowed_commands=["measure"], allowed_instruments=["asmi"])
    spec = CampaignSpec(name="test", gantry_file="g.yaml", deck_file="d.yaml", protocol_file="p.yaml", mock_mode=mock,
        parameters=[{"name": "x", "minimum": 0, "maximum": 2, "step": 1, "bindings": [{"step_index": 0, "argument": "method_kwargs.value"}]}],
        objective={"path": objective_path}, optimizer={"initial_trials": 1}, stop={"max_trials": max_trials, **stop})
    return settings, spec


def wait_for(manager, campaign_id, predicate, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = manager.get(campaign_id)
        if predicate(value):
            return value
        time.sleep(0.01)
    pytest.fail(f"timed out waiting for campaign; last={manager.get(campaign_id)}")


def test_sequential_trials_stop_at_target(tmp_path):
    outcomes = [2, 0.5]
    settings, spec = _setup(tmp_path, max_trials=5, target_value=1)
    runs = FakeRuns([2, 0.5])
    manager = CampaignManager(settings, runs, validator=lambda *args: None, poll_interval=0.005)
    record = manager.start(spec)
    final = wait_for(manager, record.campaign_id, lambda r: r.state == "completed")
    assert [t.index for t in final.trials] == [0, 1]
    assert final.stop_reason == "target_reached"
    assert len(runs.submissions) == 2


def test_manual_observation_waits_then_resumes(tmp_path):
    settings, spec = _setup(tmp_path, max_trials=2)
    spec.objective.mode = "manual"
    runs = FakeRuns([9, 8])
    manager = CampaignManager(settings, runs, validator=lambda *args: None, poll_interval=0.005)
    cid = manager.start(spec).campaign_id
    wait_for(manager, cid, lambda r: r.state == "awaiting_observation")
    assert len(runs.submissions) == 1
    manager.observe(cid, 3)
    final = wait_for(manager, cid, lambda r: len(r.trials) == 2)
    assert final.trials[0].objective == 3


def test_pause_resume_and_stop_cancel_active_child(tmp_path):
    settings, spec = _setup(tmp_path, max_trials=3)
    runs = FakeRuns([1, 1, 1], hold=True)
    manager = CampaignManager(settings, runs, validator=lambda *args: None, poll_interval=0.005)
    cid = manager.start(spec).campaign_id
    wait_for(manager, cid, lambda r: r.active_run_id is not None)
    active = manager.get(cid).active_run_id
    manager.control(cid, "pause")
    wait_for(manager, cid, lambda r: r.pause_requested)
    manager.control(cid, "cancel")
    wait_for(manager, cid, lambda r: bool(r.stop_requested))
    runs.release_event.set()
    wait_for(manager, cid, lambda r: r.state == "stopped")
    assert runs.cancels == [active]


def test_missing_objective_fails_without_next_trial(tmp_path):
    settings, spec = _setup(tmp_path, max_trials=2)
    runs = FakeRuns([None])
    manager = CampaignManager(settings, runs, validator=lambda *args: None, poll_interval=0.005)
    cid = manager.start(spec).campaign_id
    final = wait_for(manager, cid, lambda r: r.state == "failed")
    assert len(final.trials) == 1
    assert len(runs.submissions) == 1


def test_existing_run_manager_ownership_conflict(tmp_path):
    settings, spec = _setup(tmp_path)
    runs = FakeRuns()
    runs.owner = "other"
    manager = CampaignManager(settings, runs, validator=lambda *args: None)
    with pytest.raises(RunConflictError):
        manager.start(spec)


def test_batch_stock_preflight_aggregates_current_durable_sources(tmp_path, monkeypatch):
    import cubos_api.services.campaign_manager as module

    settings, spec = _setup(tmp_path, mock=False)
    spec = spec.model_copy(update={"fluid_state_id": 7, "batch_size": 2, "source_protocol_file": "source.yaml"})
    manager = CampaignManager(settings, FakeRuns(), validator=lambda *args: None)

    class Store:
        def __init__(self, _path):
            pass

        def get_fluid_snapshot(self, state_id):
            assert state_id == 7
            return {"containers": [{
                "labware_key": "stocks", "location_id": "A1",
                "current_volume_ul": 120.0, "capacity_ul": 500.0,
            }]}

        def close(self):
            pass

    monkeypatch.setattr(module, "DataStore", Store)
    protocol = """protocol:
- transfer: {source: stocks.A1, destination: plate.A1, volume_ul: 70}
- transfer: {source: stocks.A1, destination: plate.A2, volume_ul: 60}
"""
    shortages = manager._batch_stock_shortages(spec, "deck: {}", protocol)
    assert shortages == [{
        "target": "stocks.A1",
        "available_ul": 120.0,
        "required_ul": 130.0,
        "capacity_ul": 500.0,
    }]


def test_awaiting_refill_resume_reacquires_owner_and_preserves_pending_batch(tmp_path):
    settings, spec = _setup(tmp_path, mock=True, max_trials=2)
    spec = spec.model_copy(update={"batch_size": 2, "source_protocol_file": "source.yaml"})
    runs = FakeRuns()
    manager = CampaignManager(settings, runs, validator=lambda *args: None)
    campaign_id = "awaiting-refill"
    pending = PendingBatch(
        batch_index=1,
        parameters=[{"x": 1.0}, {"x": 0.0}],
        protocol_yaml="protocol: []\n",
        objective_paths=["0.value", "1.value"],
        sample_map=[
            {"sample_index": 0, "candidate_well": "plate.A1"},
            {"sample_index": 1, "candidate_well": "plate.A2"},
        ],
        run_id=f"{campaign_id}-batch-1",
    )
    record = CampaignRecord(
        campaign_id=campaign_id,
        spec=spec,
        state="awaiting_refill",
        created_at=time.time(),
        updated_at=time.time(),
        pause_requested=True,
        pause_reason="inventory_refill",
        pending_batch=pending,
    )
    manager._records[campaign_id] = record
    manager._workers.add(campaign_id)
    manager._save(record)
    (manager.base / campaign_id / "gantry.yaml").write_text("gantry: {}")
    (manager.base / campaign_id / "deck.yaml").write_text("deck: {}")
    (manager.base / campaign_id / "protocol.yaml").write_text("protocol: []\n")

    resumed = manager.control(campaign_id, "resume")
    assert runs.campaign_owner == campaign_id
    assert resumed.state == "running"
    assert resumed.pending_batch == pending


def test_awaiting_refill_pending_batch_survives_manager_restart(tmp_path, monkeypatch):
    settings, spec = _setup(tmp_path, mock=True, max_trials=2)
    spec = spec.model_copy(update={"batch_size": 2, "source_protocol_file": "source.yaml"})
    first_runs = FakeRuns()
    first = CampaignManager(settings, first_runs, validator=lambda *args: None)
    campaign_id = "awaiting-refill-restart"
    pending = PendingBatch(
        batch_index=1,
        parameters=[{"x": 2.0}, {"x": 1.0}],
        protocol_yaml="protocol: []\n",
        objective_paths=["0.value", "1.value"],
        sample_map=[
            {"sample_index": 0, "candidate_well": "plate.A1"},
            {"sample_index": 1, "candidate_well": "plate.A2"},
        ],
        run_id=f"{campaign_id}-batch-1",
    )
    record = CampaignRecord(
        campaign_id=campaign_id,
        spec=spec,
        state="awaiting_refill",
        created_at=time.time(),
        updated_at=time.time(),
        pause_requested=True,
        pause_reason="inventory_refill",
        pending_batch=pending,
    )
    first._records[campaign_id] = record
    first._save(record)
    for name, text in zip(("gantry", "deck", "protocol"), first._bundle(spec)):
        (first.base / campaign_id / f"{name}.yaml").write_text(text)

    recovered_runs = FakeRuns()
    recovered = CampaignManager(settings, recovered_runs, validator=lambda *args: None)
    restored = recovered.get(campaign_id)
    assert restored.state == "awaiting_refill"
    assert restored.pending_batch == pending

    monkeypatch.setattr(recovered, "_loop", lambda *_args: None)
    resumed = recovered.control(campaign_id, "resume")
    assert resumed.state == "running"
    assert recovered_runs.submissions == []
    assert recovered_runs.campaign_owner == campaign_id


def test_pending_batch_existing_run_is_not_submitted_again(tmp_path, monkeypatch):
    import cubos_api.services.campaign_manager as module

    settings, spec = _setup(tmp_path, mock=True, max_trials=2)
    spec = spec.model_copy(update={"batch_size": 2, "source_protocol_file": "source.yaml"})
    runs = FakeRuns()
    manager = CampaignManager(settings, runs, validator=lambda *args: None, poll_interval=0.001)
    campaign_id = "existing-batch"
    run_id = f"{campaign_id}-batch-1"
    pending = PendingBatch(
        batch_index=1,
        parameters=[{"x": 1.0}, {"x": 0.0}],
        protocol_yaml="protocol: []\n",
        objective_paths=["0.value", "0.value"],
        sample_map=[
            {"sample_index": 0, "candidate_well": "plate.A1"},
            {"sample_index": 1, "candidate_well": "plate.A2"},
        ],
        run_id=run_id,
    )
    record = CampaignRecord(
        campaign_id=campaign_id,
        spec=spec,
        created_at=time.time(),
        updated_at=time.time(),
        pending_batch=pending,
    )
    manager._records[campaign_id] = record
    manager._save(record)
    for name, text in zip(("gantry", "deck", "protocol"), manager._bundle(spec)):
        (manager.base / campaign_id / f"{name}.yaml").write_text(text)
    runs.owner = campaign_id
    runs.records[run_id] = RunRecord(
        run_id=run_id, state="succeeded", created_at=time.time(), mock_mode=True,
        result={"results": [{"value": 1.0}]},
    )
    monkeypatch.setattr(manager, "_batch_stock_shortages", lambda *args: [])
    expected_wells = iter(("plate.A1", "plate.A2"))
    monkeypatch.setattr(
        module, "extract_result_context",
        lambda *_args: {"well_identity": {"expected_well": next(expected_wells)}},
    )
    monkeypatch.setattr(module, "extract_result_objective", lambda *_args: 1.0)

    manager._loop_batch(campaign_id, tuple(
        (manager.base / campaign_id / f"{name}.yaml").read_text()
        for name in ("gantry", "deck", "protocol")
    ))

    assert runs.submissions == []
    assert manager.get(campaign_id).state == "completed"


def test_batch_target_at_five_stops_after_completed_batch(tmp_path, monkeypatch):
    import cubos_api.services.campaign_manager as module

    settings, spec = _setup(tmp_path, mock=True, max_trials=4, target_value=5.0)
    spec = spec.model_copy(update={"batch_size": 2, "source_protocol_file": "source.yaml"})
    runs = FakeRuns()
    manager = CampaignManager(settings, runs, validator=lambda *args: None, poll_interval=0.001)
    campaign_id = "batch-target-five"
    run_id = f"{campaign_id}-batch-1"
    pending = PendingBatch(
        batch_index=1,
        parameters=[{"x": 1.0}, {"x": 0.0}],
        protocol_yaml="protocol: []\n",
        objective_paths=["0.value", "0.value"],
        sample_map=[
            {"sample_index": 0, "candidate_well": "plate.A1"},
            {"sample_index": 1, "candidate_well": "plate.A2"},
        ],
        run_id=run_id,
    )
    record = CampaignRecord(
        campaign_id=campaign_id, spec=spec, created_at=time.time(),
        updated_at=time.time(), pending_batch=pending,
    )
    manager._records[campaign_id] = record
    manager._save(record)
    for name, text in zip(("gantry", "deck", "protocol"), manager._bundle(spec)):
        (manager.base / campaign_id / f"{name}.yaml").write_text(text)
    runs.owner = campaign_id
    runs.records[run_id] = RunRecord(
        run_id=run_id, state="succeeded", created_at=time.time(), mock_mode=True,
        result={"results": [{"value": 5.0}]},
    )
    monkeypatch.setattr(manager, "_batch_stock_shortages", lambda *args: [])
    expected_wells = iter(("plate.A1", "plate.A2"))
    objectives = iter((5.0, 7.0))
    monkeypatch.setattr(
        module, "extract_result_context",
        lambda *_args: {"well_identity": {"expected_well": next(expected_wells)}},
    )
    monkeypatch.setattr(module, "extract_result_objective", lambda *_args: next(objectives))

    manager._loop_batch(campaign_id, tuple(
        (manager.base / campaign_id / f"{name}.yaml").read_text()
        for name in ("gantry", "deck", "protocol")
    ))

    final = manager.get(campaign_id)
    assert final.state == "completed"
    assert final.stop_reason == "target_reached"
    assert len(final.trials) == 2
    assert runs.submissions == []


def test_real_fluid_campaign_requires_state(tmp_path):
    settings, spec = _setup(tmp_path, mock=False)
    spec.parameters[0].bindings[0].argument = "method_kwargs.value"
    # The protocol itself has no fluid command, so exercise the guard directly.
    import cubos_api.services.campaign_manager as module
    old = module.FLUID_COMMANDS.copy()
    module.FLUID_COMMANDS.add("measure")
    try:
        manager = CampaignManager(settings, FakeRuns(), validator=lambda *args: None)
        with pytest.raises(ValueError, match="fluid-state"):
            manager._preflight(spec, manager._bundle(spec))
    finally:
        module.FLUID_COMMANDS.clear(); module.FLUID_COMMANDS.update(old)


def test_trial_budget_and_owner_release(tmp_path):
    settings, spec = _setup(tmp_path, max_trials=1)
    runs = FakeRuns([1])
    manager = CampaignManager(settings, runs, validator=lambda *args: None, poll_interval=0.005)
    cid = manager.start(spec).campaign_id
    final = wait_for(manager, cid, lambda r: r.state == "completed")
    assert final.stop_reason == "trial_budget"
    wait_for(manager, cid, lambda r: runs.campaign_owner is None)


def test_patience_stops_after_min_improvement(tmp_path):
    settings, spec = _setup(tmp_path, max_trials=4, patience=1, min_improvement=0.5)
    runs = FakeRuns([2, 1.8])
    manager = CampaignManager(settings, runs, validator=lambda *args: None, poll_interval=0.005)
    cid = manager.start(spec).campaign_id
    final = wait_for(manager, cid, lambda r: r.state == "completed")
    assert final.stop_reason == "no_improvement"
    assert len(final.trials) == 2


def test_time_budget_is_checked_between_trials(tmp_path, monkeypatch):
    import cubos_api.services.campaign_manager as campaign_manager_module

    clock = ControlledClock()
    monkeypatch.setattr(campaign_manager_module, "time", clock)
    settings, spec = _setup(tmp_path, max_trials=4, max_seconds=0.5)
    runs = FakeRuns(
        [2, 1],
        on_complete=lambda index: clock.advance(1.0) if index == 0 else None,
    )
    manager = CampaignManager(settings, runs, validator=lambda *args: None, poll_interval=0.005)
    cid = manager.start(spec).campaign_id
    final = wait_for(manager, cid, lambda r: r.state == "completed")
    assert final.stop_reason == "time_budget"
    assert len(final.trials) == 1


def test_declared_initial_design_is_submitted_in_order(tmp_path):
    settings, spec = _setup(tmp_path, max_trials=2)
    spec.optimizer.initial_trials = 2
    spec.optimizer.initial_points = [{"x": 2.0}, {"x": 0.0}]
    runs = FakeRuns([2.0, 1.0])
    manager = CampaignManager(
        settings, runs, validator=lambda *args: None, poll_interval=0.005
    )
    campaign_id = manager.start(spec).campaign_id
    wait_for(manager, campaign_id, lambda record: record.state == "completed")
    assert [submission.metadata["parameters"] for submission in runs.submissions] == [
        {"x": 2.0},
        {"x": 0.0},
    ]


def test_initial_design_cannot_exceed_initial_trial_count(tmp_path):
    _, spec = _setup(tmp_path)
    with pytest.raises(ValueError, match="Initial design"):
        CampaignSpec.model_validate(
            {
                **spec.model_dump(),
                "optimizer": {
                    **spec.optimizer.model_dump(),
                    "initial_trials": 1,
                    "initial_points": [{"x": 0.0}, {"x": 1.0}],
                },
            }
        )


def test_expired_time_budget_stops_before_first_trial(tmp_path, monkeypatch):
    import cubos_api.services.campaign_manager as campaign_manager_module

    clock = ControlledClock()
    monkeypatch.setattr(campaign_manager_module, "time", clock)
    settings, spec = _setup(tmp_path, max_trials=4, max_seconds=0.5)
    runs = FakeRuns([2, 1])
    manager = CampaignManager(settings, runs, validator=lambda *args: None, poll_interval=0.005)
    original_save = manager._save
    initial_record_saved = False

    def save_and_expire(record):
        nonlocal initial_record_saved
        original_save(record)
        if not initial_record_saved:
            initial_record_saved = True
            clock.advance(1.0)

    monkeypatch.setattr(manager, "_save", save_and_expire)
    cid = manager.start(spec).campaign_id
    final = wait_for(manager, cid, lambda r: r.state == "completed")
    assert final.stop_reason == "time_budget"
    assert final.trials == []
    assert runs.submissions == []


def test_native_failure_stops_without_next_trial_and_releases_owner(tmp_path):
    settings, spec = _setup(tmp_path, max_trials=3)
    runs = FakeRuns([1], fail_indices={0})
    manager = CampaignManager(settings, runs, validator=lambda *args: None, poll_interval=0.005)
    cid = manager.start(spec).campaign_id
    final = wait_for(manager, cid, lambda r: r.state == "failed")
    assert len(final.trials) == len(runs.submissions) == 1
    wait_for(manager, cid, lambda r: runs.campaign_owner is None)


def test_snapshot_is_immutable_after_source_edit(tmp_path):
    settings, spec = _setup(tmp_path, max_trials=1)
    runs = FakeRuns([1], hold=True)
    manager = CampaignManager(settings, runs, validator=lambda *args: None, poll_interval=0.005)
    cid = manager.start(spec).campaign_id
    source = settings.configs_dir / "protocol" / "p.yaml"
    source.write_text(PROTOCOL.replace("asmi", "changed"))
    runs.release_event.set()
    wait_for(manager, cid, lambda r: r.state == "completed")
    assert "instrument: asmi" in runs.submissions[0].protocol_yaml
    assert "changed" not in runs.submissions[0].protocol_yaml


def test_restart_marks_active_campaign_interrupted_without_submitting(tmp_path):
    settings, spec = _setup(tmp_path)
    runs = FakeRuns()
    first = CampaignManager(settings, runs, validator=lambda *args: None)
    cid = "persisted"
    record = first._records.get(cid)
    if record is None:
        from cubos_api.models.campaigns import CampaignRecord
        record = CampaignRecord(campaign_id=cid, spec=spec, created_at=time.time(), updated_at=time.time())
        first._save(record)
    recovered_runs = FakeRuns()
    recovered = CampaignManager(settings, recovered_runs, validator=lambda *args: None)
    assert recovered.get(cid).state == "interrupted"
    assert recovered_runs.submissions == []


def test_restart_interrupted_campaign_recovers_completed_child_without_replay(
    tmp_path,
):
    from cubos_api.models.campaigns import CampaignRecord, CampaignTrial

    settings, spec = _setup(tmp_path, max_trials=2)
    first_runs = FakeRuns()
    first = CampaignManager(settings, first_runs, validator=lambda *args: None)
    campaign_id = "interrupted-after-success"
    record = CampaignRecord(
        campaign_id=campaign_id,
        spec=spec,
        state="running",
        created_at=time.time(),
        updated_at=time.time(),
        trials=[CampaignTrial(
            index=0, parameters={"x": 0.0}, run_id=f"{campaign_id}-trial-1",
            state="succeeded",
        )],
    )
    first._records[campaign_id] = record
    first._save(record)
    for name, text in zip(("gantry", "deck", "protocol"), first._bundle(spec)):
        (first.base / campaign_id / f"{name}.yaml").write_text(text)

    recovered_runs = FakeRuns([0.5])
    recovered_runs.records[f"{campaign_id}-trial-1"] = RunRecord(
        run_id=f"{campaign_id}-trial-1",
        state="succeeded",
        created_at=time.time(),
        mock_mode=True,
        result={"results": [{"value": 1.0}]},
    )
    recovered = CampaignManager(
        settings, recovered_runs, validator=lambda *args: None, poll_interval=0.005,
    )
    assert recovered.get(campaign_id).state == "interrupted"

    resumed = recovered.control(campaign_id, "resume")
    assert resumed.trials[0].objective == 1.0
    assert resumed.trials[0].objective_status == "accepted"
    done = wait_for(recovered, campaign_id, lambda item: item.state == "completed")

    assert [trial.run_id for trial in done.trials] == [
        f"{campaign_id}-trial-1",
        f"{campaign_id}-trial-2",
    ]
    assert [submission.run_id for submission in recovered_runs.submissions] == [
        f"{campaign_id}-trial-2",
    ]


def test_pause_finishes_trial_then_resume_continues(tmp_path):
    settings, spec = _setup(tmp_path, max_trials=2)
    native = FakeRuns([2, 1], hold=True)
    manager = CampaignManager(settings, native, validator=lambda *a: None, poll_interval=.005)
    record = manager.start(spec)
    wait_for(manager, record.campaign_id, lambda r: r.active_run_id is not None)
    manager.control(record.campaign_id, 'pause')
    native.release_event.set()
    paused = wait_for(manager, record.campaign_id, lambda r: r.state == 'paused')
    assert len(paused.trials) == 1 and paused.trials[0].objective == 2
    assert native.owner == record.campaign_id
    manager.control(record.campaign_id, 'resume')
    done = wait_for(manager, record.campaign_id, lambda r: r.state == 'completed')
    assert len(done.trials) == 2
    assert native.owner is None


def test_drain_stop_records_finished_observation_without_new_trial(tmp_path):
    settings, spec = _setup(tmp_path, max_trials=2)
    native = FakeRuns([2, 1], hold=True)
    manager = CampaignManager(settings, native, validator=lambda *a: None, poll_interval=.005)
    record = manager.start(spec)
    wait_for(manager, record.campaign_id, lambda r: r.active_run_id is not None)
    manager.control(record.campaign_id, 'stop')
    native.release_event.set()
    done = wait_for(manager, record.campaign_id, lambda r: r.state == 'stopped')
    assert len(done.trials) == 1 and done.trials[0].objective == 2
    assert native.cancels == [] and native.owner is None


def test_attach_fluid_state_preserves_failed_trial_and_does_not_resume(tmp_path):
    from cubos_api.models.campaigns import CampaignRecord, CampaignTrial

    settings, spec = _setup(tmp_path, mock=False)
    runs = FakeRuns()
    manager = CampaignManager(settings, runs, validator=lambda *a: None)
    campaign_id = "legacy-physical"
    record = CampaignRecord(
        campaign_id=campaign_id,
        spec=spec,
        state="failed",
        created_at=time.time(),
        updated_at=time.time(),
        trials=[CampaignTrial(index=0, parameters={"x": 1.0}, run_id="trial-1")],
    )
    manager._records[campaign_id] = record
    manager._save(record)
    for name, text in zip(("gantry", "deck", "protocol"), manager._bundle(spec)):
        (manager.base / campaign_id / f"{name}.yaml").write_text(text)

    attached = manager.attach_fluid_state(
        campaign_id, 17, "A1-A3 absent; current stock volumes entered by operator",
    )

    assert attached.state == "failed"
    assert attached.spec.fluid_state_id == 17
    assert attached.trials[0].run_id == "trial-1"
    assert attached.fluid_state_reconciliation_note.startswith("A1-A3 absent")
    assert runs.state_resolutions[-1][1] == 17
    assert runs.submissions == []


def test_untracked_physical_fluid_campaign_cannot_recover_completed_run(tmp_path):
    from cubos_api.models.campaigns import CampaignRecord, CampaignTrial

    settings, spec = _setup(tmp_path, mock=False)
    runs = FakeRuns()
    manager = CampaignManager(settings, runs, validator=lambda *a: None)
    campaign_id = "untracked-physical"
    record = CampaignRecord(
        campaign_id=campaign_id,
        spec=spec,
        state="failed",
        created_at=time.time(),
        updated_at=time.time(),
        trials=[CampaignTrial(index=0, parameters={"x": 1.0}, run_id="trial-1")],
    )
    manager._records[campaign_id] = record
    manager._save(record)
    for name, text in zip(("gantry", "deck", "protocol"), manager._bundle(spec)):
        (manager.base / campaign_id / f"{name}.yaml").write_text(text)
    runs.records["trial-1"] = RunRecord(
        run_id="trial-1", state="succeeded", created_at=time.time(),
        mock_mode=False, result={"results": [{"value": 1.0}]},
    )
    import cubos_api.services.campaign_manager as module
    module.FLUID_COMMANDS.add("measure")
    try:
        with pytest.raises(RunConflictError, match="no durable fluid/tip state"):
            manager.control(campaign_id, "resume")
    finally:
        module.FLUID_COMMANDS.remove("measure")

    assert manager.get(campaign_id).trials[0].objective is None
    assert runs.submissions == []


def test_preflight_uses_durable_available_tips_across_trials(tmp_path, monkeypatch):
    import cubos_api.services.campaign_manager as module

    settings, _ = _setup(tmp_path, mock=False, max_trials=2)
    protocol = """protocol:
  - pick_up_tip:
      position: tips.A4
      speed: 50.0
  - drop_tip:
      position: waste
"""
    (settings.configs_dir / "protocol" / "p.yaml").write_text(protocol)
    spec = CampaignSpec(
        name="tracked tips",
        gantry_file="g.yaml",
        deck_file="d.yaml",
        protocol_file="p.yaml",
        mock_mode=False,
        fluid_state_id=17,
        parameters=[{
            "name": "speed", "minimum": 40.0, "maximum": 60.0, "step": 10.0,
            "bindings": [{"step_index": 0, "argument": "speed"}],
        }],
        sequences=[{
            "name": "tip", "values": ["tips.A4", "tips.A7"],
            "bindings": [{"step_index": 0, "argument": "position"}],
        }],
        optimizer={"initial_trials": 1},
        stop={"max_trials": 2},
    )

    class Store:
        def __init__(self, _path):
            pass

        def get_tip_snapshot(self, _state_id):
            return {
                "pipette": {"attachment_uncertain": False, "tip_extension_mm": None},
                "containers": [
                    {"rack_key": "tips", "slot_id": slot, "status": status}
                    for slot, status in (
                        ("A1", "consumed"), ("A2", "consumed"),
                        ("A3", "consumed"), ("A4", "available"),
                        ("A7", "available"),
                    )
                ],
            }

        def close(self):
            pass

    monkeypatch.setattr(module, "DataStore", Store)
    manager = CampaignManager(
        settings, FakeRuns(), validator=lambda *args: None,
    )

    preview = manager._preflight(spec, manager._bundle(spec))
    assert "tips.A4" in preview["protocol_yaml"]

    unavailable = spec.model_copy(deep=True)
    unavailable.sequences[0].values[0] = "tips.A1"
    with pytest.raises(ValueError, match="not available in durable fluid state"):
        manager._preflight(unavailable, manager._bundle(unavailable))

    validations = []
    manager = CampaignManager(
        settings, FakeRuns(), validator=lambda *args: validations.append(args),
    )
    manager._preflight(spec, manager._bundle(spec))
    assert [slot["slot_id"] for slot in validations[-1][3]["containers"]
            if slot["status"] == "consumed"] == ["A1", "A2", "A3"]

    mock_spec = spec.model_copy(update={"mock_mode": True, "fluid_state_id": None})
    manager._preflight(mock_spec, manager._bundle(mock_spec))
    assert validations[-1][3] is None


def _dark_recovery_fixture(tmp_path):
    from cubos_api.models.campaigns import CampaignTrial
    settings, spec = _setup(tmp_path, mock=True, max_trials=8)
    spec = spec.model_copy(update={"batch_size": 3, "source_protocol_file": "source.yaml"})
    runs = FakeRuns()
    manager = CampaignManager(settings, runs, validator=lambda *args: None)
    record = CampaignRecord(campaign_id="dark", spec=spec, state="failed",
        stop_reason="batch_objective_rejected", created_at=time.time(), updated_at=time.time())
    results = []
    for index in range(6):
        dark = index == 3
        measurement = {
            "measurement_status": "rejected" if dark else "accepted",
            "comparison_status": "rejected" if dark else "accepted",
            "quality": {"accepted": not dark, "status": "rejected" if dark else "accepted", "flags": ["underexposed"] if dark else []},
            "processing_profile": {"schema": "cubos.camera-well-cielab.v1", "id": "profile"},
            "reference_processing_profile_id": "profile",
            "well_identity": {"expected_well": f"plate.{chr(65 + index)}5"},
        }
        if not dark:
            measurement["delta_e_00"] = float(40 - index)
        results.append(measurement)
        record.trials.append(CampaignTrial(index=index, parameters={"x": 1.0},
            run_id="native", state="succeeded", objective=None if dark else float(40-index),
            objective_status="rejected" if dark else "accepted", measurement=measurement,
            objective_path=f"{index}.delta_e_00", sample_well=f"plate.{chr(65 + index)}5", batch_index=index // 3 + 1))
    runs.records["native"] = RunRecord(run_id="native", state="succeeded", created_at=time.time(), mock_mode=True, result={"results": results})
    manager._records["dark"] = record
    manager._save(record)
    for name, content in zip(("gantry", "deck", "protocol"), manager._bundle(spec)):
        (manager.base / "dark" / f"{name}.yaml").write_text(content)
    return manager, runs, record


def test_underexposure_resume_preserves_six_trials_and_accepted_scores(tmp_path, monkeypatch):
    import cubos_api.services.campaign_manager as module
    manager, runs, record = _dark_recovery_fixture(tmp_path)
    before = [trial.model_dump() for trial in record.trials]
    launched = []
    monkeypatch.setattr(module.threading.Thread, "start", lambda self: launched.append(self))
    resumed = manager.resume_underexposed("dark")
    assert resumed.state == "running"
    assert resumed.spec.skip_underexposed is True
    assert len(resumed.trials) == 6
    assert [trial.model_dump() for trial in resumed.trials] == before
    assert resumed.best_objective == 35.0
    assert runs.submissions == []
    assert runs.state_resolutions == []
    assert len(launched) == 1
    with pytest.raises(RunConflictError):
        manager.resume_underexposed("dark")
    restored = CampaignManager(manager.settings, runs, validator=lambda *args: None)
    assert restored.get("dark").state == "interrupted"
    assert restored._workers == set()
    assert runs.submissions == []


@pytest.mark.parametrize("unsafe", ["centering", "profile", "native_failed", "pending", "wrong_well", "unknown_quality"])
def test_underexposure_resume_refuses_unsafe_evidence(tmp_path, unsafe):
    manager, runs, record = _dark_recovery_fixture(tmp_path)
    measurement = runs.records["native"].result["results"][3]
    if unsafe == "centering":
        measurement["quality"]["flags"].append("expected_center_residual_too_large")
    elif unsafe == "profile":
        measurement["reference_processing_profile_id"] = "other"
    elif unsafe == "native_failed":
        runs.records["native"].state = "failed"
    elif unsafe == "pending":
        record.pending_batch = PendingBatch(batch_index=3, parameters=[{"x": 1.0}], protocol_yaml=PROTOCOL,
            objective_paths=["0.delta_e_00"], sample_map=[{"candidate_well": "plate.G5"}])
    elif unsafe == "wrong_well":
        runs.records["native"].result["results"][0]["well_identity"]["expected_well"] = "plate.H5"
    else:
        measurement["quality"]["flags"] = []
    with pytest.raises(RunConflictError):
        manager.resume_underexposed("dark")
    assert record.state == "failed"
    assert record.spec.skip_underexposed is False
    assert runs.owner is None
    assert runs.submissions == []


@pytest.mark.parametrize("final_dark", [False, True])
def test_resumed_dark_batch_continues_at_g5_h5_and_never_scores_dark(tmp_path, monkeypatch, final_dark):
    import cubos_api.services.campaign_manager as module
    import cubos_api.services.color_batch as batch_module
    from cubos_api.services.color_batch import BatchCompilation
    manager, runs, record = _dark_recovery_fixture(tmp_path)
    monkeypatch.setattr(module.threading.Thread, "start", lambda self: None)
    manager.resume_underexposed("dark")
    original = [trial.model_dump() for trial in record.trials]
    suggestions = []
    def suggest(_spec, observations, **kwargs):
        suggestions.append(observations)
        return {"x": float(len(suggestions) - 1)}
    monkeypatch.setattr(manager, "_suggest", suggest)
    monkeypatch.setattr(manager, "_batch_stock_shortages", lambda *args: [])
    starts = []
    def compile_batch(_yaml, _spec, parameters, start):
        starts.append(start)
        return BatchCompilation(PROTOCOL, ("0.delta_e_00", "1.delta_e_00"), tuple(
            {"sample_index": start + offset, "candidate_well": f"plate.{chr(71 + offset)}5", "parameters": point, "objective_path": f"{offset}.delta_e_00"}
            for offset, point in enumerate(parameters)))
    monkeypatch.setattr(batch_module, "compile_color_trial_batch", compile_batch)
    native_measurements = []
    for offset in range(2):
        measurement = dict(runs.records["native"].result["results"][0])
        measurement["well_identity"] = {"expected_well": f"plate.{chr(71 + offset)}5"}
        measurement["delta_e_00"] = float(34-offset)
        if final_dark and offset == 1:
            measurement = dict(runs.records["native"].result["results"][3])
            measurement["well_identity"] = {"expected_well": "plate.H5"}
        native_measurements.append(measurement)
    run_id = "dark-batch-3"
    runs.records[run_id] = RunRecord(run_id=run_id, state="succeeded", created_at=time.time(), mock_mode=True, result={"results": native_measurements})
    manager._loop_batch("dark", manager._bundle(record.spec))
    final = manager.get("dark")
    assert final.state == "completed"
    assert final.stop_reason == "trial_budget"
    assert len(final.trials) == 8
    assert [trial.model_dump() for trial in final.trials[:6]] == original
    assert [trial.sample_well for trial in final.trials[6:]] == ["plate.G5", "plate.H5"]
    assert starts == [6]
    assert all(len(observations) == 5 for observations in suggestions)
    assert final.best_objective == (34.0 if final_dark else 33.0)
    assert final.trials[3].objective is None
    assert final.trials[3].objective_status == "rejected"
    assert runs.submissions == []


@pytest.mark.parametrize("unsafe_tip", [False, True])
def test_dark_resume_checks_durable_state_and_never_mutates_tips(tmp_path, monkeypatch, unsafe_tip):
    import copy
    import cubos_api.services.campaign_manager as module
    manager, runs, record = _dark_recovery_fixture(tmp_path)
    record.spec.fluid_state_id = 7
    snapshot = {"pipette": {"attachment_uncertain": unsafe_tip, "tip_extension_mm": None}, "containers": [{"status": "used"}]}
    before = copy.deepcopy(snapshot)
    monkeypatch.setattr(manager, "_tip_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(module.threading.Thread, "start", lambda self: None)
    if unsafe_tip:
        with pytest.raises(RunConflictError):
            manager.resume_underexposed("dark")
        assert record.state == "failed"
        assert runs.owner is None
    else:
        manager.resume_underexposed("dark")
        assert record.state == "running"
    assert snapshot == before
    assert runs.state_resolutions == [("deck: {}", 7)]
    assert runs.submissions == []
