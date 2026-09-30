from __future__ import annotations

import pytest

from cubos_client import (
    ProtocolBundle,
    StationClient,
    StationRequestError,
    api_token_from_sources,
)


class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, *responses: FakeResponse):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


BUNDLE = ProtocolBundle(
    gantry_config="name: cub\n",
    deck_config="labware: {}\n",
    protocol_yaml="protocol:\n  - home: null\n",
)


def test_health_uses_versioned_endpoint():
    session = FakeSession(FakeResponse(200, {"status": "ok"}))
    client = StationClient("http://cub.local:8742/", session=session)
    assert client.health(timeout=3) == {"status": "ok"}
    assert session.requests[0][1] == "http://cub.local:8742/api/v1/health"


def test_submit_run_sends_bundle_token_and_202_contract():
    session = FakeSession(FakeResponse(202, {"run_id": "run-1", "state": "queued"}))
    client = StationClient("http://cub", api_token="secret", session=session)
    response = client.submit_run(
        BUNDLE,
        run_id="run-1",
        metadata={"sample": "A1"},
        timeout=10,
    )
    assert response["state"] == "queued"
    method, url, kwargs = session.requests[0]
    assert (method, url) == ("POST", "http://cub/api/v1/runs")
    assert kwargs["headers"] == {"Authorization": "Bearer secret"}
    assert kwargs["json"] == {
        "run_id": "run-1",
        "gantry_config": BUNDLE.gantry_config,
        "deck_config": BUNDLE.deck_config,
        "protocol_yaml": BUNDLE.protocol_yaml,
        "mock_mode": False,
        "metadata": {"sample": "A1"},
    }


def test_run_protocol_polls_until_success_and_preserves_results():
    terminal = {
        "run_id": "run-1",
        "state": "succeeded",
        "result": {
            "campaign_id": 42,
            "results": [{"well": "A1", "measurements": [{"raw_force_n": 0.4}]}],
        },
    }
    session = FakeSession(
        FakeResponse(202, {"run_id": "run-1", "state": "queued"}),
        FakeResponse(200, {"run_id": "run-1", "state": "running"}),
        FakeResponse(200, terminal),
    )
    client = StationClient("http://cub", session=session)
    assert client.run_protocol(BUNDLE, run_id="run-1", timeout=5, poll_interval=0) == terminal
    assert [request[0] for request in session.requests] == ["POST", "GET", "GET"]


def test_failed_run_raises_with_server_error():
    session = FakeSession(
        FakeResponse(202, {"run_id": "run-1", "state": "queued"}),
        FakeResponse(200, {"run_id": "run-1", "state": "failed", "error": "boom"}),
    )
    client = StationClient("http://cub", session=session)
    with pytest.raises(StationRequestError, match="boom"):
        client.run_protocol(BUNDLE, run_id="run-1", timeout=5, poll_interval=0)


def test_cancel_events_and_artifacts_use_run_resource_paths():
    session = FakeSession(
        FakeResponse(202, {"run_id": "run-1", "state": "cancel_requested"}),
        FakeResponse(200, {"run_id": "run-1", "events": []}),
        FakeResponse(200, {"run_id": "run-1", "artifacts": ["result.json"]}),
    )
    client = StationClient("http://cub", session=session)
    client.cancel_run("run-1")
    client.events("run-1", after=4)
    client.artifacts("run-1")
    assert [request[1] for request in session.requests] == [
        "http://cub/api/v1/runs/run-1/cancel",
        "http://cub/api/v1/runs/run-1/events?after=4",
        "http://cub/api/v1/runs/run-1/artifacts",
    ]


def test_token_file_takes_precedence_over_environment(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("CUBOS_API_TOKEN", "from-env")
    assert api_token_from_sources(token_file) == "from-file"


def test_unexpected_status_is_an_error():
    session = FakeSession(FakeResponse(200, {"run_id": "run-1"}))
    client = StationClient("http://cub", session=session)
    with pytest.raises(StationRequestError, match="HTTP 200"):
        client.submit_run(BUNDLE, run_id="run-1", timeout=3)


# ── Feature 07: fluid/tip/cap state resource ─────────────────────────────


def test_submit_run_with_initial_state_sends_state_seed():
    session = FakeSession(FakeResponse(202, {"run_id": "run-1", "state": "queued"}))
    client = StationClient("http://cub", session=session)
    client.submit_run(
        BUNDLE,
        run_id="run-1",
        initial_state={"fluids": {"source": {"volume_ul": 50.0}}},
        timeout=10,
    )
    _method, _url, kwargs = session.requests[0]
    assert kwargs["json"]["state"] == {
        "initial_state": {"fluids": {"source": {"volume_ul": 50.0}}}
    }


def test_submit_run_with_fluid_state_id_sends_resume_selection():
    session = FakeSession(FakeResponse(202, {"run_id": "run-1", "state": "queued"}))
    client = StationClient("http://cub", session=session)
    client.submit_run(BUNDLE, run_id="run-1", fluid_state_id=7, timeout=10)
    _method, _url, kwargs = session.requests[0]
    assert kwargs["json"]["state"] == {"fluid_state_id": 7}


def test_submit_run_rejects_both_initial_state_and_fluid_state_id():
    session = FakeSession()
    client = StationClient("http://cub", session=session)
    with pytest.raises(StationRequestError, match="at most one"):
        client.submit_run(
            BUNDLE,
            run_id="run-1",
            initial_state={"fluids": {}},
            fluid_state_id=7,
            timeout=10,
        )
    assert session.requests == []


def test_create_fluid_state_posts_deck_reference_and_seed():
    session = FakeSession(
        FakeResponse(201, {"id": 1, "label": "demo", "container_count": 2})
    )
    client = StationClient("http://cub", session=session)
    response = client.create_fluid_state(
        deck_file="asmi_deck.yaml",
        label="demo",
        fluids={"source": {"volume_ul": 100.0}},
        timeout=5,
    )
    assert response["id"] == 1
    method, url, kwargs = session.requests[0]
    assert (method, url) == ("POST", "http://cub/api/v1/fluid-states")
    assert kwargs["json"] == {
        "deck_file": "asmi_deck.yaml",
        "label": "demo",
        "fluids": {"source": {"volume_ul": 100.0}},
    }


def test_list_fluid_states_returns_list_of_summaries():
    session = FakeSession(FakeResponse(200, [{"id": 1}, {"id": 2}]))
    client = StationClient("http://cub", session=session)
    response = client.list_fluid_states(timeout=5)
    assert response == [{"id": 1}, {"id": 2}]
    assert session.requests[0][1] == "http://cub/api/v1/fluid-states"


def test_get_fluid_state_and_containers_use_resource_paths():
    session = FakeSession(
        FakeResponse(200, {"id": 1, "containers": []}),
        FakeResponse(200, [{"labware_key": "source"}]),
    )
    client = StationClient("http://cub", session=session)
    client.get_fluid_state(1, timeout=5)
    client.get_fluid_containers(1, timeout=5)
    assert [request[1] for request in session.requests] == [
        "http://cub/api/v1/fluid-states/1",
        "http://cub/api/v1/fluid-states/1/containers",
    ]


def test_get_tip_and_cap_state_use_resource_paths():
    session = FakeSession(
        FakeResponse(200, {"fluid_state_id": 1, "containers": [], "pipette": {}}),
        FakeResponse(200, {"fluid_state_id": 1, "containers": []}),
    )
    client = StationClient("http://cub", session=session)
    client.get_tip_state(1, timeout=5)
    client.get_cap_state(1, timeout=5)
    assert [request[1] for request in session.requests] == [
        "http://cub/api/v1/fluid-states/1/tips",
        "http://cub/api/v1/fluid-states/1/caps",
    ]


def test_get_pending_operations_and_reconciliation_items():
    session = FakeSession(
        FakeResponse(200, {"fluid_state_id": 1, "operations": []}),
        FakeResponse(200, {"fluid_state_id": 1, "items": []}),
    )
    client = StationClient("http://cub", session=session)
    client.get_pending_operations(1, timeout=5)
    client.get_reconciliation_items(1, timeout=5)
    assert [request[1] for request in session.requests] == [
        "http://cub/api/v1/fluid-states/1/operations?pending_only=true",
        "http://cub/api/v1/fluid-states/1/reconciliation",
    ]


def test_resolve_reconciliation_sends_operator_and_reason():
    session = FakeSession(
        FakeResponse(
            200,
            {
                "domain": "fluid",
                "operation_key": "op-1",
                "status": "applied",
                "detail": "[alexc] confirmed",
            },
        )
    )
    client = StationClient("http://cub", session=session)
    response = client.resolve_reconciliation(
        1,
        domain="fluid",
        operation_key="op-1",
        resolution="applied",
        operator="alexc",
        reason="confirmed",
        timeout=5,
    )
    assert response["status"] == "applied"
    method, url, kwargs = session.requests[0]
    assert (method, url) == (
        "POST",
        "http://cub/api/v1/fluid-states/1/reconciliation/resolve",
    )
    assert kwargs["json"]["operator"] == "alexc"
    assert kwargs["json"]["reason"] == "confirmed"
    assert kwargs["json"]["domain"] == "fluid"
