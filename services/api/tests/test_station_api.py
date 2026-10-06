"""Offline contracts for external clients using the CubOS station boundary."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cubos_api.app import create_app
from cubos_api.config import get_settings
from cubos_api.models.runs import RunRecord
from cubos_api.services.run_manager import get_run_manager, reset_run_manager
from cubos_api.services.run_store import RunStore
from tests.api_client import api_request
from tests.test_runs_router import STATE_DECK_YAML, _payload


def reserve(app, owner="external-learning"):
    response = api_request(app, "POST", "/api/v1/station/reservation", json={"owner": owner})
    assert response.status_code == 201, response.text
    return response.json()["reservation_token"]


def test_reservation_persists_without_leaking_token_and_blocks_unrelated_writes(monkeypatch):
    app = create_app()
    token = reserve(app)
    state = api_request(app, "GET", "/api/v1/station/reservation").json()
    assert state == {"owner": "external-learning", "reserved": True}
    assert token not in json.dumps(state)
    reset_run_manager()
    assert api_request(app, "GET", "/api/v1/station/reservation").json() == state
    assert api_request(app, "POST", "/api/v1/gantry/home").status_code == 409
    assert api_request(app, "POST", "/api/v1/runs", json=_payload()).status_code == 409
    assert api_request(app, "DELETE", "/api/v1/station/reservation", headers={"X-CubOS-Reservation": "wrong"}).status_code == 409
    released = api_request(app, "DELETE", "/api/v1/station/reservation", headers={"X-CubOS-Reservation": token})
    assert released.status_code == 200
    assert released.json() == {"owner": None, "reserved": False}


def test_reserved_run_submission_accepts_token_in_body_and_emergency_cancel(monkeypatch):
    from cubos_api.routers import gantry
    app = create_app()
    token = reserve(app)
    manager = get_run_manager()
    monkeypatch.setattr(manager, "_execute", lambda run_id: None)
    payload = {**_payload(), "reservation_token": token}
    submitted = api_request(app, "POST", "/api/v1/runs", json=payload)
    assert submitted.status_code == 202, submitted.text
    assert token not in json.dumps(submitted.json())
    assert api_request(app, "DELETE", "/api/v1/station/reservation", headers={"X-CubOS-Reservation": token}).status_code == 409
    monkeypatch.setattr(gantry, "request_feed_hold_interrupt", lambda: None)
    cancelled = api_request(app, "POST", "/api/v1/runs/run-001/cancel")
    assert cancelled.status_code == 202, cancelled.text
    assert cancelled.json()["state"] == "cancel_requested"


def test_reservation_authentication_is_enforced():
    get_settings().api_token = "secret"
    app = create_app()
    assert api_request(app, "POST", "/api/v1/station/reservation", json={"owner": "client"}).status_code == 401
    response = api_request(app, "POST", "/api/v1/station/reservation", json={"owner": "client"}, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 201


def test_operator_release_requires_exact_owner_and_no_active_run():
    app = create_app()
    reserve(app)
    route = "/api/v1/station/reservation/operator-release"
    assert api_request(app, "POST", route, json={"owner": "external-learning", "confirmation": "yes"}).status_code == 409
    manager = get_run_manager()
    manager._active_run_id = "active"
    body = {"owner": "external-learning", "confirmation": "release external-learning"}
    assert api_request(app, "POST", route, json=body).status_code == 409
    manager._active_run_id = None
    assert api_request(app, "POST", route, json=body).status_code == 200


def test_category_config_snapshots_do_not_collide(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "config_dir", tmp_path)
    for category in ("gantry", "deck", "protocol"):
        (tmp_path / category).mkdir()
        (tmp_path / category / "same.yaml").write_text(f"{category}: true\n")
    app = create_app()
    for category in ("gantry", "deck", "protocol"):
        response = api_request(app, "GET", f"/api/v1/configs/{category}/same.yaml/raw")
        assert response.json() == {"content": f"{category}: true\n"}
    assert api_request(app, "GET", "/api/v1/configs/deck/missing.yaml/raw").status_code == 404


def test_inline_validation_does_not_submit_or_create_state(monkeypatch):
    from cubos_api.routers import runs
    mock = MagicMock(return_value=SimpleNamespace(passed=True, errors=[], output="validated"))
    monkeypatch.setattr(runs, "run_setup_validation", mock)
    app = create_app()
    body = {**_payload(), "mock_mode": True}
    response = api_request(app, "POST", "/api/v1/runs/validate", json=body)
    assert response.json() == {"valid": True, "errors": [], "output": "validated"}
    assert get_run_manager().get("run-001") is None
    assert not get_settings().data_db_path.exists()
    invalid = api_request(app, "POST", "/api/v1/runs/validate", json={**body, "protocol_yaml": "nonsense"})
    assert invalid.json()["valid"] is False
    assert mock.call_count == 1


def test_state_validation_returns_current_inventory_and_deck_dead_volumes(tmp_path):
    from cubos.data import DataStore
    from cubos.deck import load_deck_from_yaml
    deck_path = tmp_path / "deck.yaml"
    deck_path.write_text(STATE_DECK_YAML)
    store = DataStore(get_settings().data_db_path)
    try:
        state_id = store.create_fluid_state(str(deck_path), load_deck_from_yaml(deck_path), initial_fluids={"source": {"volume_ul": 300, "composition": {"water": 300}}})
    finally:
        store.close()
    response = api_request(create_app(), "POST", "/api/v1/station/state/validate", json={"deck_yaml": STATE_DECK_YAML, "fluid_state_id": state_id})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["fluids"]["id"] == state_id
    assert body["tips"]["fluid_state_id"] == state_id
    assert body["dead_volumes"] == {"source": 0.0, "waste": 0.0}
    source = next(item for item in body["fluids"]["containers"] if item["labware_key"] == "source")
    assert source["current_volume_ul"] == 300


def test_measurement_images_are_immutable_downloadable_evidence(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    raw = images / "capture.tiff"
    preview = images / "capture.png"
    raw.write_bytes(b"saved raw pixels")
    preview.write_bytes(b"annotated pixels")
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    store = RunStore(get_settings().run_dir)
    record = RunRecord(run_id="evidence", state="succeeded", created_at=1.0)
    store.create(record, gantry_yaml="g", deck_yaml="d", protocol_yaml="p")
    result = {"results": [{"image_path": str(raw), "annotated_preview_path": str(preview), "frame_metadata": {"image_sha256": digest}}]}
    store.collect_measurement_evidence(record, result, allowed_root=images)
    store.write(record)
    evidence = record.metadata["evidence_artifacts"]
    assert evidence[0]["result_path"] == "results.0"
    assert evidence[0]["sha256"] == digest
    raw.write_bytes(b"later pixels")
    response = api_request(create_app(), "GET", f"/api/v1/runs/evidence/artifacts/{evidence[0]['artifact']}")
    assert response.status_code == 200
    assert response.content == b"saved raw pixels"
    assert store.artifact_path("evidence", "../capture.tiff") is None


@pytest.mark.parametrize("reason", ["outside", "digest", "missing"])
def test_evidence_collection_rejects_untrusted_or_changed_images(tmp_path, reason):
    images = tmp_path / "images"
    images.mkdir()
    source = (tmp_path if reason == "outside" else images) / "capture.tiff"
    if reason != "missing":
        source.write_bytes(b"image")
    record = RunRecord(run_id="failure", state="running", created_at=1.0)
    store = RunStore(tmp_path / "runs")
    store.create(record, gantry_yaml="g", deck_yaml="d", protocol_yaml="p")
    result = [{"image_path": str(source), "frame_metadata": {"image_sha256": "changed"}}]
    with pytest.raises((ValueError, FileNotFoundError)):
        store.collect_measurement_evidence(record, result, allowed_root=images)


def test_virtual_tip_validation_rejects_restored_tip_and_changed_attachment(monkeypatch):
    from cubos_api.routers import runs
    manager = get_run_manager()
    actual = {"fluid_state_id": 1, "containers": [{"rack_key": "tips", "slot_id": "A1", "status": "consumed"}], "pipette": {"tip_extension_mm": None}}
    monkeypatch.setattr(manager, "_resolve_run_state", lambda deck, state: 1)
    store = MagicMock()
    store.get_tip_snapshot.return_value = actual
    monkeypatch.setattr(runs, "DataStore", lambda path: store)
    validator = MagicMock(return_value=SimpleNamespace(passed=True, errors=[], output="ok"))
    monkeypatch.setattr(runs, "run_setup_validation", validator)
    app = create_app()
    virtual = {**actual, "containers": [{"rack_key": "tips", "slot_id": "A1", "status": "available"}]}
    body = {**_payload(), "mock_mode": True, "tip_snapshot": virtual}
    response = api_request(app, "POST", "/api/v1/runs/validate", json=body)
    assert response.json()["valid"] is False
    assert "consumed tips" in response.json()["errors"][0]
    assert validator.call_count == 0
    body["tip_snapshot"] = {**actual, "pipette": {"tip_extension_mm": 50}}
    response = api_request(app, "POST", "/api/v1/runs/validate", json=body)
    assert response.json()["valid"] is False
    assert "physical pipette" in response.json()["errors"][0]
    body["tip_snapshot"] = actual
    assert api_request(app, "POST", "/api/v1/runs/validate", json=body).json()["valid"] is True


def test_run_validation_reports_unexpected_validator_failure(monkeypatch):
    from cubos_api.routers import runs
    def failed(*args, **kwargs):
        raise RuntimeError("validator unavailable")
    monkeypatch.setattr(runs, "run_setup_validation", failed)
    body = {**_payload(), "mock_mode": True}
    response = api_request(create_app(), "POST", "/api/v1/runs/validate", json=body)
    assert response.json() == {"valid": False, "errors": ["RuntimeError: validator unavailable"], "output": ""}
