"""Test deck API endpoints against CubOS deck loader behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.api_client import api_request
from cubos_api.app import create_app
from cubos_api.config import get_settings
from cubos_api.routers import deck as deck_router


@pytest.fixture(autouse=True)
def restore_config_dir():
    original = get_settings().config_dir
    yield
    get_settings().config_dir = original


def _preview_well_plate_body() -> dict:
    return {
        "name": "Preview Plate",
        "rows": 8,
        "columns": 12,
        "calibration": {
            "a1": {"x": 347.0, "y": 42.0, "z": 30.0},
            "a2": {"x": 338.0, "y": 42.0, "z": 30.0},
        },
        "x_offset": 9.0,
        "y_offset": 9.0,
    }


def test_list_deck_configs(monkeypatch, tmp_path: Path):
    config_dir = tmp_path / "configs"
    deck_dir = config_dir / "deck"
    deck_dir.mkdir(parents=True)
    (deck_dir / "deck.yaml").write_text("labware: {}\n", encoding="utf-8")
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)

    response = api_request(create_app(), "GET", "/api/v1/deck/configs")

    assert response.status_code == 200
    assert response.json() == ["deck.yaml"]


def test_get_deck_returns_holder_geometry_and_nested_positions(monkeypatch, tmp_path: Path):
    config_dir = tmp_path / "configs"
    deck_dir = config_dir / "deck"
    deck_dir.mkdir(parents=True)
    (deck_dir / "panda_deck.yaml").write_text(
        """\
labware:
  rack_a:
    type: tip_rack
    name: Rack A
    model_name: panda_2x2_tip_rack
    rows: 2
    columns: 2
    pickup_z: 30.0
    drop_z: 24.0
    tip_length: 59.3
    calibration:
      a1: { x: 10.0, y: 20.0 }
      a2: { x: 19.0, y: 20.0 }
    x_offset: 9.0
    y_offset: 9.0

  well_plate_holder:
    type: well_plate_holder
    name: Plate Holder
    location:
      x: 100.0
      y: 120.0
      z: 40.0
    well_plate:
      name: Panda Plate
      model_name: panda_96_wellplate
      rows: 2
      columns: 2
      calibration:
        a1:
          x: 100.0
          y: 120.0
        a2:
          x: 109.0
          y: 120.0
      x_offset: 9.0
      y_offset: 9.0

  vial_holder:
    type: vial_holder
    name: Panda Vials
    location:
      x: 30.0
      y: 60.0
      z: 8.0
    vials:
      vial_1:
        name: Sample 1
        model_name: 20ml_vial
        height: 57.0
        diameter: 28.0
        location:
          x: 30.0
          y: 60.0
        capacity_ul: 20000.0
        working_volume_ul: 15000.0
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)
    app = create_app()

    response = api_request(app, "GET", "/api/v1/deck/panda_deck.yaml")

    assert response.status_code == 200
    payload = response.json()
    items = {item["key"]: item for item in payload["labware"]}

    rack = items["rack_a"]
    assert rack["geometry"] == {"length": 9.0, "width": 9.0, "height": 6.0}
    assert rack["positions"]["A1"] == {"x": 10.0, "y": 20.0, "z": 30.0}
    assert rack["positions"]["B2"] == {"x": 19.0, "y": 11.0, "z": 30.0}

    plate_holder = items["well_plate_holder"]
    assert plate_holder["geometry"] == {"length": 100.0, "width": 155.0, "height": 14.8}
    assert plate_holder["location"] == {"x": 100.0, "y": 120.0, "z": 40.0}
    assert plate_holder["positions"]["plate"] == {"x": 100.0, "y": 120.0, "z": 45.0}
    assert plate_holder["positions"]["plate.B2"] == {"x": 109.0, "y": 111.0, "z": 45.0}

    vial_holder = items["vial_holder"]
    assert vial_holder["geometry"] == {"length": 36.2, "width": 300.2, "height": 35.1}
    assert vial_holder["location"] == {"x": 30.0, "y": 60.0, "z": 8.0}
    assert vial_holder["positions"]["vial_1"] == {"x": 30.0, "y": 60.0, "z": 26.0}


def test_get_deck_normalizes_current_well_plate_editor_fields(monkeypatch, tmp_path: Path):
    config_dir = tmp_path / "configs"
    deck_dir = config_dir / "deck"
    deck_dir.mkdir(parents=True)
    (deck_dir / "asmi_deck.yaml").write_text(
        """\
labware:
  plate:
    load_name: sbs_96_wellplate
    name: asmi_96_well_deck_origin
    model_name: asmi_96_well_deck_origin
    calibration:
      a1: {x: 347.0, y: 42.0, z: 30.0}
      a2: {x: 338.0, y: 42.0, z: 30.0}
    x_offset: 9.0
    y_offset: 9.0
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)
    app = create_app()

    response = api_request(app, "GET", "/api/v1/deck/asmi_deck.yaml")

    assert response.status_code == 200
    plate = response.json()["labware"][0]["config"]
    assert plate["rows"] == 8
    assert plate["columns"] == 12
    assert plate["length"] == 127.76
    assert plate["width"] == 85.47
    assert plate["x_offset"] == 9.0
    assert plate["y_offset"] == 9.0


def test_get_deck_uses_public_resolve_load_names(monkeypatch, tmp_path: Path):
    config_dir = tmp_path / "configs"
    deck_dir = config_dir / "deck"
    deck_dir.mkdir(parents=True)
    (deck_dir / "deck.yaml").write_text(
        """\
labware:
  vial:
    type: vial
    name: Sample
    model_name: 20ml_vial
    height: 57.0
    diameter: 28.0
    location: {x: 1.0, y: 2.0, z: 3.0}
    capacity_ul: 20000.0
    working_volume_ul: 15000.0
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)
    original = deck_router.resolve_load_names
    calls: list[dict] = []

    def wrapped(raw: dict) -> dict:
        calls.append(raw)
        return original(raw)

    monkeypatch.setattr(deck_router, "resolve_load_names", wrapped)

    response = api_request(create_app(), "GET", "/api/v1/deck/deck.yaml")

    assert response.status_code == 200
    assert calls


def test_preview_wells_derives_well_plate_coordinates():
    response = api_request(
        create_app(),
        "POST",
        "/api/v1/deck/preview-wells",
        json=_preview_well_plate_body(),
    )

    assert response.status_code == 200
    wells = response.json()
    assert wells["A1"] == {"x": 347.0, "y": 42.0, "z": 30.0}
    assert wells["B2"] == {"x": 338.0, "y": 51.0, "z": 30.0}
    assert len(wells) == 96


def test_preview_wells_uses_public_derive_wells_preview(monkeypatch):
    calls: list[tuple[object, float]] = []

    def fake_derive(entry: object, *, resolved_z: float):
        calls.append((entry, resolved_z))
        return {"A1": SimpleNamespace(x=1.2345, y=2.3456, z=3.4567)}

    monkeypatch.setattr(deck_router, "derive_wells_preview", fake_derive)

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/deck/preview-wells",
        json=_preview_well_plate_body(),
    )

    assert response.status_code == 200
    assert response.json() == {"A1": {"x": 1.234, "y": 2.346, "z": 3.457}}
    assert calls[0][1] == 30.0


def _preview_tip_rack_body() -> dict:
    return {
        "type": "tip_rack",
        "name": "Preview Rack",
        "rows": 2,
        "columns": 2,
        "pickup_z": 43.0,
        "tip_length": 59.3,
        "calibration": {
            "a1": {"x": 168.0, "y": 58.0},
            "a2": {"x": 177.0, "y": 58.0},
        },
        "x_offset": 9.0,
        "y_offset": 9.0,
    }


def test_preview_wells_derives_tip_rack_positions_at_pickup_z():
    response = api_request(
        create_app(),
        "POST",
        "/api/v1/deck/preview-wells",
        json=_preview_tip_rack_body(),
    )

    assert response.status_code == 200
    tips = response.json()
    assert tips["A1"] == {"x": 168.0, "y": 58.0, "z": 43.0}
    assert tips["B2"] == {"x": 177.0, "y": 49.0, "z": 43.0}
    assert len(tips) == 4


def test_preview_wells_rejects_invalid_tip_rack():
    body = _preview_tip_rack_body()
    del body["pickup_z"]

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/deck/preview-wells",
        json=body,
    )

    assert response.status_code == 400
    assert "pickup_z" in response.json()["detail"]


def test_preview_wells_requires_a1_z():
    body = _preview_well_plate_body()
    del body["calibration"]["a1"]["z"]

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/deck/preview-wells",
        json=body,
    )

    assert response.status_code == 400
    assert "Calibration A1 must include z" in response.json()["detail"]


def test_preview_wells_rejects_garbage_body():
    response = api_request(
        create_app(),
        "POST",
        "/api/v1/deck/preview-wells",
        json={"calibration": "nope"},
    )

    assert response.status_code == 400


def test_get_deck_returns_400_for_invalid_yaml(monkeypatch, tmp_path: Path):
    config_dir = tmp_path / "configs"
    deck_dir = config_dir / "deck"
    deck_dir.mkdir(parents=True)
    (deck_dir / "broken.yaml").write_text("labware: [\n", encoding="utf-8")
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)

    response = api_request(create_app(), "GET", "/api/v1/deck/broken.yaml")

    assert response.status_code == 400
    assert "Invalid YAML" in response.text


def test_get_deck_returns_404_for_missing_config(monkeypatch, tmp_path: Path):
    config_dir = tmp_path / "configs"
    (config_dir / "deck").mkdir(parents=True)
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)

    response = api_request(create_app(), "GET", "/api/v1/deck/missing.yaml")

    assert response.status_code == 404
    assert "Config not found: missing.yaml" in response.text


def test_deck_routes_reject_traversal_filenames(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(get_settings(), "config_dir", tmp_path)

    get_response = api_request(create_app(), "GET", "/api/v1/deck/..%5Cescape.yaml")
    put_response = api_request(
        create_app(),
        "PUT",
        "/api/v1/deck/..%5Cescape.yaml",
        json={"labware": {}},
    )

    assert get_response.status_code == 400
    assert put_response.status_code == 400


def test_put_deck_validates_current_vial_fields_before_overwriting(monkeypatch, tmp_path: Path):
    from cubos_api.services.yaml_io import read_yaml

    config_dir = tmp_path / "configs"
    deck_dir = config_dir / "deck"
    deck_dir.mkdir(parents=True)
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)

    response = api_request(
        create_app(),
        "PUT",
        "/api/v1/deck/qa_vial.yaml",
        json={
            "labware": {
                "vial_1": {
                    "type": "vial",
                    "name": "QA vial",
                    "model_name": "",
                    "height": 66.75,
                    "diameter": 28.0,
                    "location": {"x": 30.0, "y": 40.0, "z": 20.0},
                    "capacity_ul": 1500.0,
                    "working_volume_ul": 1200.0,
                }
            }
        },
    )

    assert response.status_code == 200
    vial = response.json()["labware"][0]["config"]
    assert vial["height"] == 66.75
    assert vial["diameter"] == 28.0

    saved = read_yaml(deck_dir / "qa_vial.yaml")
    assert saved["labware"]["vial_1"]["height"] == 66.75
    assert saved["labware"]["vial_1"]["diameter"] == 28.0


def test_put_deck_validates_before_overwriting_existing_file(monkeypatch, tmp_path: Path):
    config_dir = tmp_path / "configs"
    deck_dir = config_dir / "deck"
    deck_dir.mkdir(parents=True)
    path = deck_dir / "existing.yaml"
    path.write_text(
        """\
labware:
  vial_1:
    type: vial
    name: Existing vial
    model_name: ''
    height: 20.0
    diameter: 10.0
    location: {x: 1.0, y: 2.0, z: 3.0}
    capacity_ul: 100.0
    working_volume_ul: 50.0
""",
        encoding="utf-8",
    )
    original = path.read_text(encoding="utf-8")
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)

    response = api_request(
        create_app(),
        "PUT",
        "/api/v1/deck/existing.yaml",
        json={
            "labware": {
                "vial_1": {
                    "type": "vial",
                    "name": "Invalid vial",
                    "model_name": "",
                    "height": 20.0,
                    "diameter": 10.0,
                    "location": {"x": 1.0, "y": 2.0, "z": 3.0},
                    "capacity_ul": 100.0,
                    "working_volume_ul": 150.0,
                }
            }
        },
    )

    assert response.status_code == 400
    assert path.read_text(encoding="utf-8") == original


def test_deck_serializers_handle_missing_values():
    assert deck_router._serialize_point(None) is None
    assert deck_router._serialize_geometry(None) is None


def test_normalize_labware_config_handles_sparse_inputs():
    class NamedLabware:
        name = "Resolved name"
        model_name = "resolved_model"

    class AnonymousLabware:
        pass

    resolved = deck_router._normalize_labware_config([], NamedLabware(), "slot_a")
    fallback = deck_router._normalize_labware_config({}, AnonymousLabware(), "slot_b")
    config: dict[str, object] = {}

    deck_router._set_default(config, "rows", 8)

    assert resolved["name"] == "Resolved name"
    assert resolved["model_name"] == "resolved_model"
    assert fallback["name"] == "slot_b"
    assert config["rows"] == 8


def test_normalize_labware_config_uses_public_labware_schema_mapping(monkeypatch):
    class Schema:
        model_fields = {"resolved_field": object()}

    class Labware:
        name = "Resolved"
        resolved_field = 42

    monkeypatch.setattr(deck_router, "LABWARE_YAML_ENTRY_MODELS", {"custom": Schema})

    resolved = deck_router._normalize_labware_config(
        {"type": "custom"},
        Labware(),
        "slot",
    )

    assert resolved["resolved_field"] == 42


def test_delete_deck_removes_file_and_404s_when_missing(monkeypatch, tmp_path: Path):
    config_dir = tmp_path / "configs"
    deck_dir = config_dir / "deck"
    deck_dir.mkdir(parents=True)
    (deck_dir / "old.yaml").write_text("labware: {}\n", encoding="utf-8")
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)
    app = create_app()

    response = api_request(app, "DELETE", "/api/v1/deck/old.yaml")
    assert response.status_code == 200
    assert response.json() == {"status": "deleted", "filename": "old.yaml"}
    assert not (deck_dir / "old.yaml").exists()

    assert api_request(app, "DELETE", "/api/v1/deck/old.yaml").status_code == 404
    assert api_request(app, "DELETE", "/api/v1/deck/%2e%2e").status_code in (400, 404)


def test_delete_deck_refused_while_run_active(monkeypatch, tmp_path: Path):
    from cubos_api.routers import gantry as gantry_router

    config_dir = tmp_path / "configs"
    deck_dir = config_dir / "deck"
    deck_dir.mkdir(parents=True)
    (deck_dir / "busy.yaml").write_text("labware: {}\n", encoding="utf-8")
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)
    monkeypatch.setattr(gantry_router, "run_active", lambda: True)

    response = api_request(create_app(), "DELETE", "/api/v1/deck/busy.yaml")
    assert response.status_code == 409
    assert (deck_dir / "busy.yaml").exists()
