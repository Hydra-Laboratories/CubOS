import time
from pathlib import Path

import pytest

from cubos_api.models.campaigns import CampaignSpec
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


class FakeRuns:
    def __init__(self, outcomes=None, hold=False, fail_indices=None):
        self.records = {}
        self.outcomes = list(outcomes or [1.0])
        self.submissions = []
        self.cancels = []
        self.owner = None
        self.hold = hold
        self.fail_indices = set(fail_indices or ())
        import threading
        self.release_event = threading.Event()

    @property
    def active_run_id(self):
        return next((k for k, v in self.records.items() if v.state in {"queued", "running", "cancel_requested"}), None)

    def _validate_bundle(self, *args):
        return None

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


def test_time_budget_is_checked_between_trials(tmp_path):
    settings, spec = _setup(tmp_path, max_trials=4, max_seconds=0.001)
    runs = FakeRuns([2, 1])
    manager = CampaignManager(settings, runs, validator=lambda *args: None, poll_interval=0.005)
    cid = manager.start(spec).campaign_id
    final = wait_for(manager, cid, lambda r: r.state == "completed")
    assert final.stop_reason == "time_budget"
    assert len(final.trials) == 1


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
