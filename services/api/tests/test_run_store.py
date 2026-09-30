"""Tests for RunStore event sequencing.

Step-level events push the event log from ~4 entries per run into the
hundreds, so sequence assignment has to stay cheap and stay correct across a
process restart.
"""

from __future__ import annotations

import time

from cubos_api.models.runs import RunRecord
from cubos_api.services.run_store import RunStore


def _store(tmp_path) -> RunStore:
    return RunStore(tmp_path / "runs")


def _create(store: RunStore, run_id: str = "run-a") -> RunRecord:
    record = RunRecord(run_id=run_id, state="queued", created_at=time.time())
    return store.create(
        record,
        gantry_yaml="g: 1\n",
        deck_yaml="d: 1\n",
        protocol_yaml="p: 1\n",
    )


def test_sequences_are_dense_and_ordered(tmp_path):
    store = _store(tmp_path)
    _create(store)  # writes the "queued" event as sequence 1
    for index in range(50):
        store.append_event("run-a", state="running", message=f"event {index}")
    sequences = [event.sequence for event in store.events("run-a")]
    assert sequences == list(range(1, 52))


def test_sequence_survives_a_process_restart(tmp_path):
    store = _store(tmp_path)
    _create(store)
    store.append_event("run-a", state="running", message="before restart")

    # A fresh RunStore has an empty cache and must seed from events.jsonl
    # rather than restarting the numbering.
    reopened = RunStore(tmp_path / "runs")
    event = reopened.append_event("run-a", state="running", message="after restart")
    assert event.sequence == 3
    assert [e.sequence for e in reopened.events("run-a")] == [1, 2, 3]


def test_appending_does_not_reparse_the_whole_log(tmp_path, monkeypatch):
    """Guards the O(n^2) regression: appending must not re-read every event.

    The previous implementation numbered each event by counting the existing
    ones, which is quadratic once a run emits per-step events on the
    execution thread between motion commands.
    """
    store = _store(tmp_path)
    _create(store)
    calls = {"count": 0}
    original = RunStore.events

    def counting_events(self, run_id):
        calls["count"] += 1
        return original(self, run_id)

    monkeypatch.setattr(RunStore, "events", counting_events)
    for index in range(100):
        store.append_event("run-a", state="running", message=f"event {index}")
    # At most one seeding read for the whole burst.
    assert calls["count"] <= 1


def test_kind_and_data_round_trip(tmp_path):
    store = _store(tmp_path)
    _create(store)
    payload = {"index": 3, "command": "transfer", "outcome": "started"}
    store.append_event(
        "run-a", state="running", message="step 3 transfer started",
        kind="step", data=payload,
    )
    written = store.events("run-a")[-1]
    assert written.kind == "step"
    assert written.data == payload


def test_lifecycle_events_default_to_kind_lifecycle(tmp_path):
    store = _store(tmp_path)
    _create(store)
    assert store.events("run-a")[0].kind == "lifecycle"
    assert store.events("run-a")[0].data is None


def test_sequences_are_independent_per_run(tmp_path):
    store = _store(tmp_path)
    _create(store, "run-a")
    _create(store, "run-b")
    store.append_event("run-a", state="running", message="a1")
    store.append_event("run-b", state="running", message="b1")
    assert [e.sequence for e in store.events("run-a")] == [1, 2]
    assert [e.sequence for e in store.events("run-b")] == [1, 2]
