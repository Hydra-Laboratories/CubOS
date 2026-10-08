from cubos_api.services.step_observer import RunStoreStepObserver


class _Store:
    def __init__(self):
        self.events = []

    def append_event(self, run_id, **event):
        self.events.append((run_id, event))


def test_photo_pause_completion_emits_capture_contract(monkeypatch):
    monkeypatch.setattr("cubos_api.services.step_observer.time.time", lambda: 1234.5)
    store = _Store()
    observer = RunStoreStepObserver(store, "run-1")

    observer.photo_pause_completed(
        index=13,
        command="photo_pause",
        substep=None,
        seconds=2.0,
        capture_hold_seconds=2.0,
        well="plate.A4",
    )

    assert store.events == [("run-1", {
        "state": "running",
        "message": "photo pose settled for plate.A4",
        "kind": "photo_pause",
        "data": {
            "capture_still": True,
            "wait_completed": True,
            "settle_ms": 2000,
            "stable_until_server_time": 1236.5,
            "well": "plate.A4",
            "step_index": 13,
        },
        "timestamp": 1234.5,
    })]
