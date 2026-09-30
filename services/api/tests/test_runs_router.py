"""Contract tests for asynchronous versioned CubOS runs."""

from __future__ import annotations

import hashlib
import time
from threading import Event

from tests.api_client import api_request
from cubos_api.app import create_app
from cubos_api.config import get_settings
from cubos_api.services.run_manager import reset_run_manager


GANTRY_YAML = "name: test-gantry\n"
DECK_YAML = "labware: {}\n"
PROTOCOL_YAML = "protocol:\n  - home: null\n"

STATE_DECK_YAML = """\
labware:
  source:
    type: vial
    name: source
    model_name: source_vial
    height: 50.0
    diameter: 20.0
    location: {x: 5.0, y: 5.0, z: 20.0}
    capacity_ul: 500.0
    working_volume_ul: 400.0

  waste:
    type: vial
    name: waste
    model_name: waste_vial
    height: 50.0
    diameter: 20.0
    location: {x: 40.0, y: 5.0, z: 20.0}
    capacity_ul: 500.0
    working_volume_ul: 400.0
"""

OTHER_STATE_DECK_YAML = """\
labware:
  source:
    type: vial
    name: source
    model_name: source_vial
    height: 50.0
    diameter: 20.0
    location: {x: 5.0, y: 5.0, z: 20.0}
    capacity_ul: 999.0
    working_volume_ul: 900.0

  waste:
    type: vial
    name: waste
    model_name: waste_vial
    height: 50.0
    diameter: 20.0
    location: {x: 40.0, y: 5.0, z: 20.0}
    capacity_ul: 500.0
    working_volume_ul: 400.0
"""


def _payload(run_id: str = "run-001") -> dict:
    return {
        "run_id": run_id,
        "gantry_config": GANTRY_YAML,
        "deck_config": DECK_YAML,
        "protocol_yaml": PROTOCOL_YAML,
        "metadata": {"sample": "A1"},
    }


def _stateful_payload(run_id: str, *, state: dict, deck_yaml: str = STATE_DECK_YAML) -> dict:
    return {
        "run_id": run_id,
        "gantry_config": GANTRY_YAML,
        "deck_config": deck_yaml,
        "protocol_yaml": PROTOCOL_YAML,
        "state": state,
    }


def _wait_for_state(app, run_id: str, *states: str) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = api_request(app, "GET", f"/api/v1/runs/{run_id}")
        assert response.status_code == 200
        body = response.json()
        if body["state"] in states:
            return body
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not reach {states}")


def test_submit_returns_202_and_persists_artifacts(monkeypatch):
    from cubos_api.routers import gantry as gantry_router

    monkeypatch.setattr(
        gantry_router,
        "run_protocol_on_session",
        lambda **_kwargs: type(
            "Result",
            (),
            {
                "status": "ok",
                "steps_executed": 1,
                "campaign_id": 7,
                "results": [{"well": "A1", "force": 0.42}],
            },
        )(),
    )
    app = create_app()
    response = api_request(app, "POST", "/api/v1/runs", json=_payload())
    assert response.status_code == 202
    assert response.headers["location"] == "/api/v1/runs/run-001"
    assert response.json()["digests"]["protocol_sha256"] == hashlib.sha256(
        PROTOCOL_YAML.encode()
    ).hexdigest()

    completed = _wait_for_state(app, "run-001", "succeeded")
    assert completed["result"] == {
        "campaign_id": 7,
        "results": [{"force": 0.42, "well": "A1"}],
        "status": "ok",
        "steps_executed": 1,
    }
    artifacts = api_request(app, "GET", "/api/v1/runs/run-001/artifacts").json()
    assert "protocol.yaml" in artifacts["artifacts"]
    assert "result.json" in artifacts["artifacts"]
    protocol = api_request(
        app, "GET", "/api/v1/runs/run-001/artifacts/protocol.yaml"
    )
    assert protocol.status_code == 200
    assert protocol.text == PROTOCOL_YAML


def test_run_events_are_ordered_and_filterable(monkeypatch):
    from cubos_api.routers import gantry as gantry_router

    monkeypatch.setattr(gantry_router, "run_protocol_on_session", lambda **_kwargs: {"ok": True})
    app = create_app()
    api_request(app, "POST", "/api/v1/runs", json=_payload())
    _wait_for_state(app, "run-001", "succeeded")
    events = api_request(app, "GET", "/api/v1/runs/run-001/events").json()["events"]
    assert [event["state"] for event in events] == ["queued", "running", "succeeded"]
    filtered = api_request(
        app, "GET", "/api/v1/runs/run-001/events?after=1"
    ).json()["events"]
    assert [event["sequence"] for event in filtered] == [2, 3]


def test_second_run_is_rejected_while_first_is_active(monkeypatch):
    from cubos_api.routers import gantry as gantry_router

    release = Event()
    monkeypatch.setattr(
        gantry_router,
        "run_protocol_on_session",
        lambda **_kwargs: release.wait(timeout=2) or {"ok": True},
    )
    app = create_app()
    first = api_request(app, "POST", "/api/v1/runs", json=_payload("first"))
    assert first.status_code == 202
    _wait_for_state(app, "first", "running")
    second = api_request(app, "POST", "/api/v1/runs", json=_payload("second"))
    assert second.status_code == 409
    assert "busy with run 'first'" in second.text
    release.set()
    _wait_for_state(app, "first", "succeeded")


def test_cancel_uses_existing_session_interrupt(monkeypatch):
    from cubos_api.routers import gantry as gantry_router

    release = Event()
    interrupted = Event()

    def execute(**_kwargs):
        release.wait(timeout=2)
        return {"ok": True}

    monkeypatch.setattr(gantry_router, "run_protocol_on_session", execute)
    monkeypatch.setattr(gantry_router, "request_feed_hold_interrupt", interrupted.set)
    app = create_app()
    api_request(app, "POST", "/api/v1/runs", json=_payload())
    _wait_for_state(app, "run-001", "running")
    response = api_request(app, "POST", "/api/v1/runs/run-001/cancel")
    assert response.status_code == 202
    assert response.json()["state"] == "cancel_requested"
    assert interrupted.is_set()
    release.set()
    _wait_for_state(app, "run-001", "succeeded")


def test_allow_list_and_config_digest_are_enforced():
    settings = get_settings()
    settings.allowed_commands = ["move"]
    app = create_app()
    denied = api_request(app, "POST", "/api/v1/runs", json=_payload())
    assert denied.status_code == 400
    assert "command 'home' is not allowed" in denied.text

    settings.allowed_commands = ["home"]
    settings.expected_gantry_sha256 = "not-the-real-digest"
    denied = api_request(app, "POST", "/api/v1/runs", json=_payload())
    assert denied.status_code == 400
    assert "digest does not match" in denied.text


def test_native_state_changes_require_token_when_configured(monkeypatch):
    from pydantic import SecretStr
    from cubos_api.routers import gantry as gantry_router

    get_settings().api_token = SecretStr("device-secret")
    monkeypatch.setattr(gantry_router, "run_protocol_on_session", lambda **_kwargs: {})
    app = create_app()
    denied = api_request(app, "POST", "/api/v1/runs", json=_payload())
    assert denied.status_code == 401
    accepted = api_request(
        app,
        "POST",
        "/api/v1/runs",
        json=_payload(),
        headers={"authorization": "Bearer device-secret"},
    )
    assert accepted.status_code == 202
    _wait_for_state(app, "run-001", "succeeded")


def test_unsafe_or_incomplete_submissions_are_rejected():
    app = create_app()
    unsafe = api_request(app, "POST", "/api/v1/runs", json=_payload("../escape"))
    assert unsafe.status_code == 422
    incomplete = api_request(
        app,
        "POST",
        "/api/v1/runs",
        json={"gantry_config": GANTRY_YAML, "deck_config": DECK_YAML},
    )
    assert incomplete.status_code == 422


def test_missing_run_and_artifact_return_404():
    app = create_app()
    assert api_request(app, "GET", "/api/v1/runs/missing").status_code == 404
    assert (
        api_request(app, "GET", "/api/v1/runs/missing/artifacts/result.json").status_code
        == 404
    )


# ── Feature 07: run submission state exclusivity ────────────────────────


def test_run_state_selection_rejects_both_initial_state_and_existing_id():
    app = create_app()
    payload = _stateful_payload(
        "state-both",
        state={
            "initial_state": {"fluids": {"source": {"volume_ul": 10.0}}},
            "fluid_state_id": 1,
        },
    )
    response = api_request(app, "POST", "/api/v1/runs", json=payload)
    assert response.status_code == 422


def test_run_state_selection_rejects_neither_initial_state_nor_existing_id():
    app = create_app()
    payload = _stateful_payload("state-neither", state={})
    response = api_request(app, "POST", "/api/v1/runs", json=payload)
    assert response.status_code == 422


def test_run_without_state_stays_unlinked(monkeypatch):
    """Every run submitted before Feature 07 keeps behaving identically:
    omitting `state` entirely persists no fluid_state_id."""
    from cubos_api.routers import gantry as gantry_router

    monkeypatch.setattr(gantry_router, "run_protocol_on_session", lambda **_kwargs: {"ok": True})
    app = create_app()
    api_request(app, "POST", "/api/v1/runs", json=_payload("stateless"))
    completed = _wait_for_state(app, "stateless", "succeeded")
    assert completed["fluid_state_id"] is None


def test_run_submission_creates_new_fluid_state_and_persists_link(monkeypatch):
    from cubos_api.routers import gantry as gantry_router

    monkeypatch.setattr(gantry_router, "run_protocol_on_session", lambda **_kwargs: {"ok": True})
    app = create_app()
    payload = _stateful_payload(
        "state-new",
        state={
            "initial_state": {
                "label": "run-seeded",
                "fluids": {"source": {"volume_ul": 50.0, "composition": {"water": 50.0}}},
            }
        },
    )
    response = api_request(app, "POST", "/api/v1/runs", json=payload)
    assert response.status_code == 202
    fluid_state_id = response.json()["fluid_state_id"]
    assert isinstance(fluid_state_id, int)

    completed = _wait_for_state(app, "state-new", "succeeded")
    assert completed["fluid_state_id"] == fluid_state_id

    snapshot = api_request(app, "GET", f"/api/v1/fluid-states/{fluid_state_id}")
    assert snapshot.status_code == 200
    assert snapshot.json()["label"] == "run-seeded"


def test_run_execution_receives_fluid_state_id(monkeypatch):
    """The linked fluid state must reach the execution context, not just the
    run record — otherwise runs silently execute untracked (no volume
    journaling, no state-derived liquid heights)."""
    from cubos_api.routers import gantry as gantry_router

    seen_kwargs = {}

    def capture(**kwargs):
        seen_kwargs.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(gantry_router, "run_protocol_on_session", capture)
    app = create_app()
    payload = _stateful_payload(
        "state-exec",
        state={
            "initial_state": {
                "label": "exec-linked",
                "fluids": {"source": {"volume_ul": 50.0, "composition": {"water": 50.0}}},
            }
        },
    )
    response = api_request(app, "POST", "/api/v1/runs", json=payload)
    assert response.status_code == 202
    fluid_state_id = response.json()["fluid_state_id"]

    _wait_for_state(app, "state-exec", "succeeded")
    assert seen_kwargs["fluid_state_id"] == fluid_state_id


def test_run_submission_resumes_existing_fluid_state(monkeypatch):
    from cubos_api.routers import gantry as gantry_router

    monkeypatch.setattr(gantry_router, "run_protocol_on_session", lambda **_kwargs: {"ok": True})
    app = create_app()

    created = api_request(
        app,
        "POST",
        "/api/v1/runs",
        json=_stateful_payload(
            "state-seed-for-resume",
            state={"initial_state": {"fluids": {"source": {"volume_ul": 20.0}}}},
        ),
    )
    fluid_state_id = created.json()["fluid_state_id"]
    _wait_for_state(app, "state-seed-for-resume", "succeeded")

    resumed = api_request(
        app,
        "POST",
        "/api/v1/runs",
        json=_stateful_payload(
            "state-resume", state={"fluid_state_id": fluid_state_id}
        ),
    )
    assert resumed.status_code == 202
    assert resumed.json()["fluid_state_id"] == fluid_state_id
    _wait_for_state(app, "state-resume", "succeeded")


def test_run_submission_unknown_fluid_state_id_is_404():
    app = create_app()
    response = api_request(
        app,
        "POST",
        "/api/v1/runs",
        json=_stateful_payload("state-missing", state={"fluid_state_id": 99999}),
    )
    assert response.status_code == 404


def test_run_submission_deck_fingerprint_mismatch_is_409(monkeypatch):
    from cubos_api.routers import gantry as gantry_router

    monkeypatch.setattr(gantry_router, "run_protocol_on_session", lambda **_kwargs: {"ok": True})
    app = create_app()

    created = api_request(
        app,
        "POST",
        "/api/v1/runs",
        json=_stateful_payload(
            "state-seed-for-mismatch",
            state={"initial_state": {"fluids": {"source": {"volume_ul": 20.0}}}},
        ),
    )
    fluid_state_id = created.json()["fluid_state_id"]
    _wait_for_state(app, "state-seed-for-mismatch", "succeeded")

    mismatched = api_request(
        app,
        "POST",
        "/api/v1/runs",
        json=_stateful_payload(
            "state-mismatch",
            state={"fluid_state_id": fluid_state_id},
            deck_yaml=OTHER_STATE_DECK_YAML,
        ),
    )
    assert mismatched.status_code == 409
    assert "fingerprint" in mismatched.text


def test_run_record_fluid_state_link_survives_restart(monkeypatch):
    """RunRecord (JSON-on-disk via RunStore) already survives a service
    restart; this proves the new `fluid_state_id` field rides along."""
    from cubos_api.routers import gantry as gantry_router

    monkeypatch.setattr(gantry_router, "run_protocol_on_session", lambda **_kwargs: {"ok": True})
    app = create_app()
    submitted = api_request(
        app,
        "POST",
        "/api/v1/runs",
        json=_stateful_payload(
            "state-restart",
            state={"initial_state": {"fluids": {"source": {"volume_ul": 15.0}}}},
        ),
    )
    fluid_state_id = submitted.json()["fluid_state_id"]
    _wait_for_state(app, "state-restart", "succeeded")

    # Simulate a service restart: drop the in-memory RunManager singleton so
    # the next request rebuilds it purely from what's on disk.
    reset_run_manager()
    reopened_app = create_app()
    reread = api_request(reopened_app, "GET", "/api/v1/runs/state-restart")
    assert reread.status_code == 200
    assert reread.json()["fluid_state_id"] == fluid_state_id


STEP_PROTOCOL_YAML = """\
protocol:
  - home: null
  - pause:
      seconds: 0
      reason: settling
  - home: null
"""


def _step_payload(run_id: str = "run-steps") -> dict:
    return {
        "run_id": run_id,
        "gantry_config": GANTRY_YAML,
        "deck_config": DECK_YAML,
        "protocol_yaml": STEP_PROTOCOL_YAML,
    }


def test_plan_returns_compiled_steps_with_summaries(monkeypatch):
    from cubos_api.routers import gantry as gantry_router

    monkeypatch.setattr(gantry_router, "run_protocol_on_session", lambda **_kw: {"ok": True})
    app = create_app()
    api_request(app, "POST", "/api/v1/runs", json=_step_payload())
    _wait_for_state(app, "run-steps", "succeeded")

    _resp = api_request(app, "GET", "/api/v1/runs/run-steps/plan")
    assert _resp.status_code == 200, _resp.json()
    plan = _resp.json()
    assert plan["run_id"] == "run-steps"
    assert [step["index"] for step in plan["steps"]] == [0, 1, 2]
    assert [step["command"] for step in plan["steps"]] == ["home", "pause", "home"]
    # Summaries come from the commands' own formatters, not the UI.
    assert plan["steps"][0]["summary"] == "all axes"
    assert "settling" in plan["steps"][1]["summary"]


def test_plan_is_available_after_the_run_finishes(monkeypatch):
    """The step view has to survive a reload and still render a finished run."""
    from cubos_api.routers import gantry as gantry_router

    monkeypatch.setattr(gantry_router, "run_protocol_on_session", lambda **_kw: {"ok": True})
    app = create_app()
    api_request(app, "POST", "/api/v1/runs", json=_step_payload())
    _wait_for_state(app, "run-steps", "succeeded")
    first = api_request(app, "GET", "/api/v1/runs/run-steps/plan").json()
    second = api_request(app, "GET", "/api/v1/runs/run-steps/plan").json()
    assert first == second


def test_plan_404s_for_an_unknown_run():
    app = create_app()
    response = api_request(app, "GET", "/api/v1/runs/nope/plan")
    assert response.status_code == 404


def test_plan_422s_on_an_uncompilable_protocol(monkeypatch):
    from cubos_api.routers import gantry as gantry_router

    monkeypatch.setattr(gantry_router, "run_protocol_on_session", lambda **_kw: {"ok": True})
    app = create_app()
    api_request(
        app,
        "POST",
        "/api/v1/runs",
        json={
            "run_id": "run-bad",
            "gantry_config": GANTRY_YAML,
            "deck_config": DECK_YAML,
            "protocol_yaml": "protocol:\n  - not_a_real_command: null\n",
        },
    )
    _wait_for_state(app, "run-bad", "failed", "succeeded")
    response = api_request(app, "GET", "/api/v1/runs/run-bad/plan")
    assert response.status_code == 422
    assert "not_a_real_command" in response.json()["detail"]


# A mock run really loads the gantry/deck and executes the engine, so it needs
# a valid (offline) machine config rather than the stub used elsewhere here.
MOCK_GANTRY_YAML = """\
serial_port: /dev/null
gantry_type: cub_xl
cnc:
  factory_z_travel_mm: 129.0
  y_axis_motion: head
  safe_z: 69.0
working_volume:
  x_min: 0.0
  x_max: 307.0
  y_min: 0.0
  y_max: 300.0
  z_min: 0.0
  z_max: 129.0
instruments:
  camera:
    type: camera
    vendor: mount_only
    offline: true
    offset_x: 0.0
    offset_y: 0.0
    depth: 0.0
"""


def test_mock_run_emits_step_events():
    """A mock run drives the same engine path, so it produces real step events."""
    app = create_app()
    payload = _step_payload("run-mock-steps")
    payload["gantry_config"] = MOCK_GANTRY_YAML
    payload["mock_mode"] = True
    api_request(app, "POST", "/api/v1/runs", json=payload)
    _wait_for_state(app, "run-mock-steps", "succeeded", "failed")

    events = api_request(app, "GET", "/api/v1/runs/run-mock-steps/events").json()["events"]
    step_events = [event for event in events if event["kind"] == "step"]
    assert step_events, "expected kind=step events from a mock run"

    started = [e["data"] for e in step_events if e["data"]["outcome"] == "started"]
    completed = [e["data"] for e in step_events if e["data"]["outcome"] == "completed"]
    assert [entry["index"] for entry in started] == [0, 1, 2]
    assert [entry["command"] for entry in started] == ["home", "pause", "home"]
    assert all(entry["duration_s"] is not None for entry in completed)
    # Step events report progress within a run, never a state transition.
    assert {event["state"] for event in step_events} == {"running"}
    # Sequence numbers stay dense and ordered across both event kinds.
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))


def test_lifecycle_events_still_default_to_kind_lifecycle(monkeypatch):
    from cubos_api.routers import gantry as gantry_router

    monkeypatch.setattr(gantry_router, "run_protocol_on_session", lambda **_kw: {"ok": True})
    app = create_app()
    api_request(app, "POST", "/api/v1/runs", json=_payload())
    _wait_for_state(app, "run-001", "succeeded")
    events = api_request(app, "GET", "/api/v1/runs/run-001/events").json()["events"]
    assert {event["kind"] for event in events} == {"lifecycle"}
    assert all(event["data"] is None for event in events)


def test_events_written_before_kind_existed_still_parse(tmp_path):
    """Backwards compatibility: `kind`/`data` are additive with defaults."""
    from cubos_api.models.runs import RunEvent

    legacy = '{"sequence":1,"timestamp":1.0,"state":"queued","message":"run accepted"}'
    event = RunEvent.model_validate_json(legacy)
    assert event.kind == "lifecycle"
    assert event.data is None
