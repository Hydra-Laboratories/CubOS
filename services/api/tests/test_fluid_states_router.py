"""Tests for the versioned fluid/tip/cap state resource (Feature 07)."""

from __future__ import annotations

from pathlib import Path

from cubos.data import DataStore

from tests.api_client import api_request
from cubos_api.app import create_app
from cubos_api.config import get_settings


DECK_YAML = """\
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
    role: stock
    solution: buffer

  waste:
    type: vial
    name: waste
    model_name: waste_vial
    height: 50.0
    diameter: 20.0
    location: {x: 40.0, y: 5.0, z: 20.0}
    capacity_ul: 500.0
    working_volume_ul: 400.0
    role: waste
"""


def _write_deck_config(monkeypatch, tmp_path: Path, filename: str = "state-deck.yaml") -> Path:
    config_dir = tmp_path / "configs"
    deck_dir = config_dir / "deck"
    deck_dir.mkdir(parents=True, exist_ok=True)
    path = deck_dir / filename
    path.write_text(DECK_YAML, encoding="utf-8")
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)
    return path


def _store() -> DataStore:
    return DataStore(get_settings().data_db_path)


def test_create_fluid_state_returns_summary(monkeypatch, tmp_path: Path):
    _write_deck_config(monkeypatch, tmp_path)
    app = create_app()

    response = api_request(
        app,
        "POST",
        "/api/v1/fluid-states",
        json={
            "deck_file": "state-deck.yaml",
            "label": "demo",
            "fluids": {"source": {"volume_ul": 100.0, "composition": {"buffer": 100.0}}},
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["label"] == "demo"
    assert body["container_count"] == 2
    assert len(body["deck_fingerprint"]) == 64
    assert isinstance(body["id"], int)


def test_create_fluid_state_404_for_missing_deck_file(monkeypatch, tmp_path: Path):
    _write_deck_config(monkeypatch, tmp_path)
    app = create_app()

    response = api_request(
        app, "POST", "/api/v1/fluid-states", json={"deck_file": "missing.yaml"}
    )

    assert response.status_code == 404


def test_list_and_get_fluid_state(monkeypatch, tmp_path: Path):
    _write_deck_config(monkeypatch, tmp_path)
    app = create_app()
    created = api_request(
        app,
        "POST",
        "/api/v1/fluid-states",
        json={
            "deck_file": "state-deck.yaml",
            "fluids": {"source": {"volume_ul": 100.0, "composition": {"buffer": 100.0}}},
        },
    ).json()
    state_id = created["id"]

    listed = api_request(app, "GET", "/api/v1/fluid-states")
    assert listed.status_code == 200
    assert any(row["id"] == state_id for row in listed.json())

    detail = api_request(app, "GET", f"/api/v1/fluid-states/{state_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["pending_operation_count"] == 0
    assert body["reconciliation_required_count"] == 0
    by_key = {c["labware_key"]: c for c in body["containers"]}
    assert by_key["source"]["role"] == "stock"
    assert by_key["source"]["solution"] == "buffer"
    assert by_key["source"]["current_volume_ul"] == 100.0
    assert by_key["source"]["composition"] == {"buffer": 100.0}
    assert by_key["waste"]["role"] == "waste"
    assert by_key["waste"]["current_volume_ul"] == 0.0


def test_get_fluid_state_404_for_unknown_id(monkeypatch, tmp_path: Path):
    _write_deck_config(monkeypatch, tmp_path)
    app = create_app()
    response = api_request(app, "GET", "/api/v1/fluid-states/999")
    assert response.status_code == 404


def test_get_containers_matches_detail_containers(monkeypatch, tmp_path: Path):
    _write_deck_config(monkeypatch, tmp_path)
    app = create_app()
    state_id = api_request(
        app, "POST", "/api/v1/fluid-states", json={"deck_file": "state-deck.yaml"}
    ).json()["id"]

    response = api_request(app, "GET", f"/api/v1/fluid-states/{state_id}/containers")
    assert response.status_code == 200
    keys = {c["labware_key"] for c in response.json()}
    assert keys == {"source", "waste"}


def test_get_tips_and_caps_shapes(monkeypatch, tmp_path: Path):
    _write_deck_config(monkeypatch, tmp_path)
    app = create_app()
    state_id = api_request(
        app, "POST", "/api/v1/fluid-states", json={"deck_file": "state-deck.yaml"}
    ).json()["id"]

    tips = api_request(app, "GET", f"/api/v1/fluid-states/{state_id}/tips")
    assert tips.status_code == 200
    tip_body = tips.json()
    assert tip_body["fluid_state_id"] == state_id
    assert tip_body["containers"] == []
    assert tip_body["pipette"]["pipette_key"] == "pipette"

    caps = api_request(app, "GET", f"/api/v1/fluid-states/{state_id}/caps")
    assert caps.status_code == 200
    cap_body = caps.json()
    assert cap_body["fluid_state_id"] == state_id
    # Neither vial declares `capped:` in the fixture deck, so cap tracking
    # is not engaged for either container (cubos.data.cap_state semantics).
    assert cap_body["containers"] == []


def _seed_reconciliation_required(state_id: int, operation_key: str = "indeterminate") -> None:
    store = _store()
    try:
        campaign_id = store.create_campaign("state api test", fluid_state_id=state_id)
        store.begin_fluid_transfer(
            state_id, operation_key, "source", "waste", 10.0, campaign_id=campaign_id
        )
        store.mark_fluid_reconciliation_required(
            operation_key, "process stopped after pipette actuation"
        )
    finally:
        store.close()


def test_operations_and_reconciliation_listing(monkeypatch, tmp_path: Path):
    _write_deck_config(monkeypatch, tmp_path)
    app = create_app()
    state_id = api_request(
        app,
        "POST",
        "/api/v1/fluid-states",
        json={"deck_file": "state-deck.yaml", "fluids": {"source": {"volume_ul": 100.0}}},
    ).json()["id"]
    _seed_reconciliation_required(state_id)

    operations = api_request(app, "GET", f"/api/v1/fluid-states/{state_id}/operations")
    assert operations.status_code == 200
    ops = operations.json()["operations"]
    assert len(ops) == 1
    assert ops[0]["domain"] == "fluid"
    assert ops[0]["status"] == "reconciliation_required"
    assert ops[0]["context"]["source"] == "source"

    reconciliation = api_request(app, "GET", f"/api/v1/fluid-states/{state_id}/reconciliation")
    assert reconciliation.status_code == 200
    items = reconciliation.json()["items"]
    assert len(items) == 1
    assert items[0]["operation_key"] == "indeterminate"


def test_resolve_reconciliation_records_operator_and_reason(monkeypatch, tmp_path: Path):
    _write_deck_config(monkeypatch, tmp_path)
    app = create_app()
    state_id = api_request(
        app,
        "POST",
        "/api/v1/fluid-states",
        json={"deck_file": "state-deck.yaml", "fluids": {"source": {"volume_ul": 100.0}}},
    ).json()["id"]
    _seed_reconciliation_required(state_id)

    response = api_request(
        app,
        "POST",
        f"/api/v1/fluid-states/{state_id}/reconciliation/resolve",
        json={
            "domain": "fluid",
            "operation_key": "indeterminate",
            "resolution": "applied",
            "operator": "alexc",
            "reason": "confirmed transfer completed via camera review",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "applied"
    assert "alexc" in body["detail"]
    assert "confirmed transfer completed" in body["detail"]

    reconciliation = api_request(app, "GET", f"/api/v1/fluid-states/{state_id}/reconciliation")
    assert reconciliation.json()["items"] == []


def test_resolve_reconciliation_requires_operator_and_reason(monkeypatch, tmp_path: Path):
    _write_deck_config(monkeypatch, tmp_path)
    app = create_app()
    state_id = api_request(
        app,
        "POST",
        "/api/v1/fluid-states",
        json={"deck_file": "state-deck.yaml", "fluids": {"source": {"volume_ul": 100.0}}},
    ).json()["id"]
    _seed_reconciliation_required(state_id)

    missing_operator = api_request(
        app,
        "POST",
        f"/api/v1/fluid-states/{state_id}/reconciliation/resolve",
        json={
            "domain": "fluid",
            "operation_key": "indeterminate",
            "resolution": "applied",
            "operator": "  ",
            "reason": "valid reason",
        },
    )
    assert missing_operator.status_code == 422

    missing_reason = api_request(
        app,
        "POST",
        f"/api/v1/fluid-states/{state_id}/reconciliation/resolve",
        json={
            "domain": "fluid",
            "operation_key": "indeterminate",
            "resolution": "applied",
            "operator": "alexc",
            "reason": "",
        },
    )
    assert missing_reason.status_code == 422


def test_resolve_reconciliation_404_for_unknown_operation(monkeypatch, tmp_path: Path):
    _write_deck_config(monkeypatch, tmp_path)
    app = create_app()
    state_id = api_request(
        app,
        "POST",
        "/api/v1/fluid-states",
        json={"deck_file": "state-deck.yaml", "fluids": {"source": {"volume_ul": 100.0}}},
    ).json()["id"]

    response = api_request(
        app,
        "POST",
        f"/api/v1/fluid-states/{state_id}/reconciliation/resolve",
        json={
            "domain": "fluid",
            "operation_key": "does-not-exist",
            "resolution": "applied",
            "operator": "alexc",
            "reason": "no such operation",
        },
    )
    assert response.status_code == 404


def test_resolve_reconciliation_invalid_resolution_is_400(monkeypatch, tmp_path: Path):
    _write_deck_config(monkeypatch, tmp_path)
    app = create_app()
    state_id = api_request(
        app,
        "POST",
        "/api/v1/fluid-states",
        json={"deck_file": "state-deck.yaml", "fluids": {"source": {"volume_ul": 100.0}}},
    ).json()["id"]
    _seed_reconciliation_required(state_id)

    response = api_request(
        app,
        "POST",
        f"/api/v1/fluid-states/{state_id}/reconciliation/resolve",
        json={
            "domain": "fluid",
            "operation_key": "indeterminate",
            "resolution": "not-a-real-resolution",
            "operator": "alexc",
            "reason": "typo test",
        },
    )
    assert response.status_code == 400
