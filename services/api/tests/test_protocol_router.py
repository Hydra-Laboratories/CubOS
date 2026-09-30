"""Test protocol API endpoints."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cubos.gantry.session import (
    CalibrationBlockedError,
    GantryNotConnectedError,
    GantrySessionError,
    GantrySessionHealthCheckError,
    InterruptFeedHoldTimeoutError,
)
from cubos.validation.errors import BoundsViolation, SetupValidationError

from tests.api_client import api_request
from cubos_api.app import create_app
from cubos_api.config import get_settings
from cubos_api.services.yaml_io import read_yaml, write_yaml


@pytest.fixture()
def tmp_configs(monkeypatch):
    """Create a temp configs dir with a sample protocol."""
    with tempfile.TemporaryDirectory() as d:
        configs = Path(d) / "configs"
        configs.mkdir()

        protocol_data = {
            "positions": {
                "park": [10.0, 20.0, 30.0],
            },
            "protocol": [
                {"move": {"instrument": "pipette", "position": "plate_1.A1"}},
                {"aspirate": {"position": "plate_1.A1", "volume_ul": 100.0}},
            ],
        }
        write_yaml(configs / "test_protocol.yaml", protocol_data)
        write_yaml(configs / "deck.yaml", {"labware": {}})

        monkeypatch.setattr(get_settings(), "config_dir", Path(d) / "configs")
        yield configs


@pytest.fixture()
def client():
    return create_app()


def test_get_commands(client):
    response = api_request(client, "GET", "/api/v1/protocol/commands")
    assert response.status_code == 200
    commands = response.json()
    names = [command["name"] for command in commands]
    assert "move" in names
    assert "aspirate" in names
    assert "scan" in names
    assert len(names) >= 8


def test_get_single_command(client):
    response = api_request(client, "GET", "/api/v1/protocol/commands/aspirate")
    assert response.status_code == 200
    command = response.json()
    assert command["name"] == "aspirate"
    arg_names = [arg["name"] for arg in command["args"]]
    assert "position" in arg_names
    assert "volume_ul" in arg_names
    assert "speed" in arg_names


def test_get_unknown_command(client):
    response = api_request(client, "GET", "/api/v1/protocol/commands/nonexistent")
    assert response.status_code == 404


def test_list_protocol_configs(client, tmp_configs):
    response = api_request(client, "GET", "/api/v1/protocol/configs")
    assert response.status_code == 200
    configs = response.json()
    assert "test_protocol.yaml" in configs
    assert "deck.yaml" not in configs


def test_get_protocol(client, tmp_configs):
    response = api_request(client, "GET", "/api/v1/protocol/test_protocol.yaml")
    assert response.status_code == 200
    data = response.json()
    assert data["filename"] == "test_protocol.yaml"
    assert data["positions"] == {"park": [10.0, 20.0, 30.0]}
    assert len(data["steps"]) == 2
    assert data["steps"][0]["command"] == "move"
    assert data["steps"][1]["command"] == "aspirate"
    assert data["steps"][1]["args"]["volume_ul"] == 100.0


def test_get_protocol_errors(client, tmp_configs):
    missing = api_request(client, "GET", "/api/v1/protocol/missing.yaml")
    assert missing.status_code == 404

    write_yaml(tmp_configs / "not_protocol.yaml", {"not_protocol": []})
    invalid = api_request(client, "GET", "/api/v1/protocol/not_protocol.yaml")
    assert invalid.status_code == 400

    (tmp_configs / "bad.yaml").write_text("protocol: [", encoding="utf-8")
    bad_yaml = api_request(client, "GET", "/api/v1/protocol/bad.yaml")
    assert bad_yaml.status_code == 400

    (tmp_configs / "scalar.yaml").write_text("protocol\n", encoding="utf-8")
    scalar_yaml = api_request(client, "GET", "/api/v1/protocol/scalar.yaml")
    assert scalar_yaml.status_code == 400
    assert "is not a YAML mapping" in scalar_yaml.text


def test_get_protocol_rejects_malformed_steps(client, tmp_configs):
    write_yaml(
        tmp_configs / "malformed.yaml",
        {
            "protocol": [
                {
                    "move": {"instrument": "pipette", "position": "plate_1.A1"},
                    "aspirate": {"position": "plate_1.A1", "volume_ul": 100.0},
                },
            ],
        },
    )

    response = api_request(client, "GET", "/api/v1/protocol/malformed.yaml")

    assert response.status_code == 400
    assert "exactly one command" in response.text


def test_get_protocol_rejects_bare_string_step(client, tmp_configs):
    write_yaml(
        tmp_configs / "bare_string_step.yaml",
        {
            "protocol": [
                {"move": {"instrument": "pipette", "position": "plate_1.A1"}},
                "oops",
            ],
        },
    )

    response = api_request(client, "GET", "/api/v1/protocol/bare_string_step.yaml")

    assert response.status_code == 400
    assert "Input should be a valid dictionary" in response.text


def test_save_protocol(client, tmp_configs):
    body = {
        "positions": {"park": [10.0, 20.0, 30.0]},
        "protocol": [
            {
                "command": "move",
                "args": {"instrument": "uvvis", "position": "plate_1.A1"},
            },
        ],
    }
    response = api_request(client, "PUT", "/api/v1/protocol/new_protocol.yaml", json=body)
    assert response.status_code == 200
    assert (tmp_configs / "new_protocol.yaml").exists()
    assert read_yaml(tmp_configs / "new_protocol.yaml")["positions"] == body["positions"]


def test_save_protocol_omits_empty_positions(client, tmp_configs):
    body = {
        "protocol": [
            {
                "command": "move",
                "args": {"instrument": "uvvis", "position": "plate_1.A1"},
            },
        ],
    }

    response = api_request(client, "PUT", "/api/v1/protocol/no_positions.yaml", json=body)

    assert response.status_code == 200
    saved = read_yaml(tmp_configs / "no_positions.yaml")
    assert "positions" not in saved


def test_save_protocol_preserves_sidecar_top_level_keys_and_comments(client, tmp_configs):
    path = tmp_configs / "sidecar.yaml"
    path.write_text(
        """\
# protocol file comment
operator_notes:
  owner: lab
positions:
  old: [0.0, 0.0, 0.0]
protocol:
  - move:
      instrument: pipette
      position: plate_1.A1
""",
        encoding="utf-8",
    )
    body = {
        "positions": {"park": [10.0, 20.0, 30.0]},
        "protocol": [
            {
                "command": "move",
                "args": {"instrument": "uvvis", "position": "plate_1.A1"},
            },
        ],
    }

    response = api_request(client, "PUT", "/api/v1/protocol/sidecar.yaml", json=body)

    assert response.status_code == 200
    saved = read_yaml(path)
    assert saved["operator_notes"] == {"owner": "lab"}
    assert saved["positions"] == body["positions"]
    assert saved["protocol"] == [
        {"move": {"instrument": "uvvis", "position": "plate_1.A1"}}
    ]
    assert "# protocol file comment" in path.read_text(encoding="utf-8")


def test_protocol_get_put_roundtrip_preserves_sidecar_top_level_keys(client, tmp_configs):
    path = tmp_configs / "roundtrip_sidecar.yaml"
    write_yaml(
        path,
        {
            "operator_notes": {"owner": "lab"},
            "positions": {"park": [10.0, 20.0, 30.0]},
            "protocol": [
                {"move": {"instrument": "pipette", "position": "plate_1.A1"}},
            ],
        },
    )

    get_response = api_request(client, "GET", "/api/v1/protocol/roundtrip_sidecar.yaml")
    assert get_response.status_code == 200
    loaded = get_response.json()

    put_response = api_request(
        client,
        "PUT",
        "/api/v1/protocol/roundtrip_sidecar.yaml",
        json={"positions": loaded["positions"], "protocol": loaded["steps"]},
    )

    assert put_response.status_code == 200
    saved = read_yaml(path)
    assert saved["operator_notes"] == {"owner": "lab"}
    assert saved["positions"] == {"park": [10.0, 20.0, 30.0]}
    assert saved["protocol"] == [
        {"move": {"instrument": "pipette", "position": "plate_1.A1"}}
    ]


def test_validate_protocol_ok(client):
    body = {
        "protocol": [
            {"command": "move", "args": {"instrument": "pipette", "position": "plate_1.A1"}},
            {"command": "aspirate", "args": {"position": "plate_1.A1", "volume_ul": 100.0}},
        ]
    }
    response = api_request(client, "POST", "/api/v1/protocol/validate", json=body)
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is True
    assert data["errors"] == []


def test_validate_protocol_unknown_command(client):
    body = {
        "protocol": [
            {"command": "fly_away", "args": {}},
        ]
    }
    response = api_request(client, "POST", "/api/v1/protocol/validate", json=body)
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is False
    assert "Unknown command" in data["errors"][0]


def test_validate_protocol_missing_args(client):
    body = {
        "protocol": [
            {"command": "aspirate", "args": {"position": "plate_1.A1"}},
        ]
    }
    response = api_request(client, "POST", "/api/v1/protocol/validate", json=body)
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is False
    assert "volume_ul" in data["errors"][0]


def test_validate_protocol_unknown_args(client):
    body = {
        "protocol": [
            {"command": "move", "args": {"instrument": "p", "position": "a", "turbo": True}},
        ]
    }
    response = api_request(client, "POST", "/api/v1/protocol/validate", json=body)
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is False
    assert "turbo" in data["errors"][0]


def test_validate_setup_endpoint_maps_unexpected_errors(monkeypatch, tmp_path):
    from cubos_api.routers import protocol as protocol_router

    for subdir in ("gantry", "deck", "protocol"):
        (tmp_path / subdir).mkdir()

    monkeypatch.setattr(
        protocol_router,
        "run_setup_validation",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("validator exploded")),
    )
    monkeypatch.setattr(
        protocol_router,
        "get_settings",
        lambda: type("S", (), {"configs_dir": tmp_path})(),
    )

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/protocol/validate-setup",
        json={
            "gantry_file": "gantry.yaml",
            "deck_file": "deck.yaml",
            "protocol_file": "protocol.yaml",
        },
    )

    assert response.status_code == 500
    assert "validator exploded" in response.text


def test_validate_setup_endpoint_calls_cubos_validation(monkeypatch, tmp_path):
    from cubos_api.routers import protocol as protocol_router

    for subdir in ("gantry", "deck", "protocol"):
        (tmp_path / subdir).mkdir()

    observed: dict[str, tuple[str, str, str]] = {}

    class FakeResult:
        passed = True
        errors = ()
        output = "RESULT: PASS"

    def fake_run_setup_validation(gantry_path: str, deck_path: str, protocol_path: str):
        observed["paths"] = (gantry_path, deck_path, protocol_path)
        return FakeResult()

    monkeypatch.setattr(protocol_router, "run_setup_validation", fake_run_setup_validation)
    monkeypatch.setattr(
        protocol_router,
        "get_settings",
        lambda: type("S", (), {"configs_dir": tmp_path})(),
    )

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/protocol/validate-setup",
        json={
            "gantry_file": "gantry.yaml",
            "deck_file": "deck.yaml",
            "protocol_file": "protocol.yaml",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "valid": True,
        "errors": [],
        "output": "RESULT: PASS",
    }
    assert observed["paths"] == (
        str(tmp_path / "gantry" / "gantry.yaml"),
        str(tmp_path / "deck" / "deck.yaml"),
        str(tmp_path / "protocol" / "protocol.yaml"),
    )


def test_validate_setup_endpoint_returns_validation_errors(monkeypatch, tmp_path):
    from cubos_api.routers import protocol as protocol_router

    for subdir in ("gantry", "deck", "protocol"):
        (tmp_path / subdir).mkdir()

    class FakeResult:
        passed = False
        errors = ("park.location.target: gantry (-3.0, 50.0, 30.0) violates x_min=0.0",)
        output = "RESULT: FAIL"

    monkeypatch.setattr(
        protocol_router,
        "run_setup_validation",
        lambda *_args: FakeResult(),
    )
    monkeypatch.setattr(
        protocol_router,
        "get_settings",
        lambda: type("S", (), {"configs_dir": tmp_path})(),
    )

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/protocol/validate-setup",
        json={
            "gantry_file": "gantry.yaml",
            "deck_file": "deck.yaml",
            "protocol_file": "protocol.yaml",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is False
    assert "park.location.target" in data["errors"][0]
    assert data["output"] == "RESULT: FAIL"


def test_run_endpoint_delegates_to_gantry_session(monkeypatch, tmp_path):
    from cubos_api.routers import gantry as gantry_router
    from cubos_api.routers import protocol as protocol_router

    observed: dict[str, object] = {}

    def fake_run_protocol_on_session(**kwargs):
        observed.update(kwargs)
        return type(
            "Result",
            (),
            {"status": "ok", "steps_executed": 2, "campaign_id": 123},
        )()

    monkeypatch.setattr(gantry_router, "run_protocol_on_session", fake_run_protocol_on_session)
    for subdir in ("gantry", "deck", "protocol"):
        (tmp_path / subdir).mkdir()
    monkeypatch.setattr(
        protocol_router,
        "get_settings",
        lambda: type(
            "S",
            (),
            {"configs_dir": tmp_path, "data_db_path": tmp_path / "data.db"},
        )(),
    )

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/protocol/run",
        json={
            "gantry_file": "gantry.yaml",
            "deck_file": "deck.yaml",
            "protocol_file": "protocol.yaml",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "steps_executed": 2,
        "campaign_id": 123,
    }
    assert observed["gantry_path"] == str(tmp_path / "gantry" / "gantry.yaml")
    assert observed["deck_path"] == str(tmp_path / "deck" / "deck.yaml")
    assert observed["protocol_path"] == str(tmp_path / "protocol" / "protocol.yaml")
    assert observed["db_path"] == tmp_path / "data.db"


def test_run_endpoint_maps_session_connection_errors(monkeypatch, tmp_path):
    from cubos_api.routers import gantry as gantry_router
    from cubos_api.routers import protocol as protocol_router

    monkeypatch.setattr(
        gantry_router,
        "run_protocol_on_session",
        lambda **_kwargs: (_ for _ in ()).throw(GantryNotConnectedError("nope")),
    )
    monkeypatch.setattr(
        protocol_router,
        "get_settings",
        lambda: type("S", (), {"configs_dir": tmp_path, "data_db_path": tmp_path / "data.db"})(),
    )

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/protocol/run",
        json={
            "gantry_file": "gantry.yaml",
            "deck_file": "deck.yaml",
            "protocol_file": "protocol.yaml",
        },
    )

    assert response.status_code == 400
    assert "Gantry is not connected" in response.text


def test_run_endpoint_blocks_active_calibration_warning(monkeypatch, tmp_path):
    from cubos_api.routers import gantry as gantry_router
    from cubos_api.routers import protocol as protocol_router

    monkeypatch.setattr(
        gantry_router,
        "run_protocol_on_session",
        lambda **_kwargs: (_ for _ in ()).throw(
            CalibrationBlockedError("calibration warning is active")
        ),
    )
    monkeypatch.setattr(
        protocol_router,
        "get_settings",
        lambda: type("S", (), {"configs_dir": tmp_path, "data_db_path": tmp_path / "data.db"})(),
    )

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/protocol/run",
        json={
            "gantry_file": "gantry.yaml",
            "deck_file": "deck.yaml",
            "protocol_file": "protocol.yaml",
        },
    )

    assert response.status_code == 400
    assert "calibration warning is active" in response.text


def test_run_endpoint_error_mapping(monkeypatch, tmp_path):
    from cubos_api.routers import gantry as gantry_router
    from cubos_api.routers import protocol as protocol_router

    setup_error = SetupValidationError([
        BoundsViolation(
            labware_key="plate",
            position_id="A1",
            instrument_name=None,
            coordinate_type="gantry",
            x=-1.0,
            y=0.0,
            z=0.0,
            axis="x",
            bound_name="x_min",
            bound_value=0.0,
        )
    ])
    cases = [
        (GantrySessionHealthCheckError("not healthy"), 400, "Gantry is not connected"),
        (setup_error, 400, "x_min"),
        (GantrySessionError("session exploded"), 500, "Execution failed"),
        (RuntimeError("plain exploded"), 500, "Execution failed"),
    ]
    monkeypatch.setattr(
        protocol_router,
        "get_settings",
        lambda: type("S", (), {"configs_dir": tmp_path, "data_db_path": tmp_path / "data.db"})(),
    )
    for exc, status, text in cases:
        monkeypatch.setattr(
            gantry_router,
            "run_protocol_on_session",
            lambda **_kwargs: (_ for _ in ()).throw(exc),
        )
        response = api_request(
            create_app(),
            "POST",
            "/api/v1/protocol/run",
            json={
                "gantry_file": "gantry.yaml",
                "deck_file": "deck.yaml",
                "protocol_file": "protocol.yaml",
            },
        )
        assert response.status_code == status
        assert text in response.text


def test_run_status_endpoint_reflects_state(monkeypatch):
    from cubos_api.routers import gantry as gantry_router

    idle = api_request(create_app(), "GET", "/api/v1/protocol/run-status")
    assert idle.status_code == 200
    assert idle.json() == {"active": False, "protocol_file": None}

    gantry_router.begin_run(protocol_file="foo.yaml")
    try:
        active = api_request(create_app(), "GET", "/api/v1/protocol/run-status")
        assert active.status_code == 200
        assert active.json() == {"active": True, "protocol_file": "foo.yaml"}
    finally:
        gantry_router.end_run()


def test_run_endpoint_returns_409_when_run_already_active(monkeypatch, tmp_path):
    from cubos_api.routers import gantry as gantry_router
    from cubos_api.routers import protocol as protocol_router

    for subdir in ("gantry", "deck", "protocol"):
        (tmp_path / subdir).mkdir()
    monkeypatch.setattr(
        protocol_router,
        "get_settings",
        lambda: type(
            "S",
            (),
            {"configs_dir": tmp_path, "data_db_path": tmp_path / "data.db"},
        )(),
    )
    gantry_router.begin_run(protocol_file="already_running.yaml")
    try:
        response = api_request(
            create_app(),
            "POST",
            "/api/v1/protocol/run",
            json={
                "gantry_file": "gantry.yaml",
                "deck_file": "deck.yaml",
                "protocol_file": "protocol.yaml",
            },
        )
        assert response.status_code == 409
        assert "already in progress" in response.text
    finally:
        gantry_router.end_run()


def test_run_endpoint_clears_gate_after_run_raises(monkeypatch, tmp_path):
    from cubos_api.routers import gantry as gantry_router
    from cubos_api.routers import protocol as protocol_router

    for subdir in ("gantry", "deck", "protocol"):
        (tmp_path / subdir).mkdir()
    monkeypatch.setattr(
        protocol_router,
        "get_settings",
        lambda: type(
            "S",
            (),
            {"configs_dir": tmp_path, "data_db_path": tmp_path / "data.db"},
        )(),
    )
    monkeypatch.setattr(
        gantry_router,
        "run_protocol_on_session",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/protocol/run",
        json={
            "gantry_file": "gantry.yaml",
            "deck_file": "deck.yaml",
            "protocol_file": "protocol.yaml",
        },
    )

    assert response.status_code == 500
    assert gantry_router.run_active() is False


def test_run_endpoint_clears_gate_after_success(monkeypatch, tmp_path):
    from cubos_api.routers import gantry as gantry_router
    from cubos_api.routers import protocol as protocol_router

    for subdir in ("gantry", "deck", "protocol"):
        (tmp_path / subdir).mkdir()
    monkeypatch.setattr(
        protocol_router,
        "get_settings",
        lambda: type(
            "S",
            (),
            {"configs_dir": tmp_path, "data_db_path": tmp_path / "data.db"},
        )(),
    )
    monkeypatch.setattr(
        gantry_router,
        "run_protocol_on_session",
        lambda **_kwargs: type(
            "Result", (), {"status": "ok", "steps_executed": 1, "campaign_id": 1}
        )(),
    )

    response = api_request(
        create_app(),
        "POST",
        "/api/v1/protocol/run",
        json={
            "gantry_file": "gantry.yaml",
            "deck_file": "deck.yaml",
            "protocol_file": "protocol.yaml",
        },
    )

    assert response.status_code == 200
    assert gantry_router.run_active() is False


def test_cancel_endpoint_available_while_run_active(monkeypatch):
    from cubos_api.routers import gantry as gantry_router

    interrupt = MagicMock()
    monkeypatch.setattr(gantry_router, "request_feed_hold_interrupt", interrupt)
    gantry_router.begin_run(protocol_file="running.yaml")
    try:
        response = api_request(create_app(), "POST", "/api/v1/protocol/cancel")
        assert response.status_code == 200
    finally:
        gantry_router.end_run()


def test_cancel_endpoint_requests_session_interrupt(monkeypatch):
    from cubos_api.routers import gantry as gantry_router

    interrupt = MagicMock()
    monkeypatch.setattr(gantry_router, "request_feed_hold_interrupt", interrupt)

    response = api_request(create_app(), "POST", "/api/v1/protocol/cancel")

    assert response.status_code == 200
    assert response.json() == {"status": "cancel_requested"}
    interrupt.assert_called_once()


def test_cancel_endpoint_treats_feed_hold_timeout_as_requested(monkeypatch):
    from cubos_api.routers import gantry as gantry_router

    def timeout():
        raise InterruptFeedHoldTimeoutError("sent but not acknowledged")

    monkeypatch.setattr(gantry_router, "request_feed_hold_interrupt", timeout)

    response = api_request(create_app(), "POST", "/api/v1/protocol/cancel")

    assert response.status_code == 200
    assert response.json() == {
        "status": "cancel_requested",
        "warning": "sent but not acknowledged",
    }


def test_cancel_endpoint_requires_connected_gantry(monkeypatch):
    from cubos_api.routers import gantry as gantry_router

    def fail():
        raise GantryNotConnectedError("Gantry not connected")

    monkeypatch.setattr(gantry_router, "request_feed_hold_interrupt", fail)

    response = api_request(create_app(), "POST", "/api/v1/protocol/cancel")

    assert response.status_code == 400
    assert "Gantry is not connected" in response.text


def test_cancel_endpoint_maps_unexpected_errors(monkeypatch):
    from cubos_api.routers import gantry as gantry_router

    def fail():
        raise RuntimeError("serial failed")

    monkeypatch.setattr(gantry_router, "request_feed_hold_interrupt", fail)

    response = api_request(create_app(), "POST", "/api/v1/protocol/cancel")

    assert response.status_code == 500
    assert "Cancel failed" in response.text


def test_delete_protocol_removes_file_and_404s_when_missing(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs"
    protocol_dir = config_dir / "protocol"
    protocol_dir.mkdir(parents=True)
    (protocol_dir / "old.yaml").write_text("protocol:\n  - home:\n", encoding="utf-8")
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)
    app = create_app()

    response = api_request(app, "DELETE", "/api/v1/protocol/old.yaml")
    assert response.status_code == 200
    assert response.json() == {"status": "deleted", "filename": "old.yaml"}
    assert not (protocol_dir / "old.yaml").exists()
    assert api_request(app, "DELETE", "/api/v1/protocol/old.yaml").status_code == 404


def test_delete_protocol_refused_while_run_active(monkeypatch, tmp_path):
    from cubos_api.routers import gantry as gantry_router

    config_dir = tmp_path / "configs"
    protocol_dir = config_dir / "protocol"
    protocol_dir.mkdir(parents=True)
    (protocol_dir / "busy.yaml").write_text("protocol:\n  - home:\n", encoding="utf-8")
    monkeypatch.setattr(get_settings(), "config_dir", config_dir)
    monkeypatch.setattr(gantry_router, "run_active", lambda: True)

    assert api_request(create_app(), "DELETE", "/api/v1/protocol/busy.yaml").status_code == 409
    assert (protocol_dir / "busy.yaml").exists()
