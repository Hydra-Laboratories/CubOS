"""Tests for the CubOS appliance update API."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from cubos_api.app import create_app
from cubos_api.config import get_settings
from cubos_api.routers import system
from cubos_api.services import updater
from cubos_api.services.run_manager import get_run_manager
from tests.api_client import api_request


CURRENT_SHA = "1" * 40
LATEST_SHA = "2" * 40
CURRENT_TAG = "v2026.08.03"
LATEST_TAG = "v2026.08.10"


@pytest.fixture(autouse=True)
def _isolate_updater(tmp_path, monkeypatch):
    repo = tmp_path / "CubOS"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(updater, "REPO_DIR", repo)
    settings = get_settings()
    original_repo = settings.update_repo_dir
    original_script = settings.update_script
    original_mode = settings.update_mode
    settings.update_repo_dir = None
    settings.update_script = None
    settings.update_mode = "tag"
    updater.reset_update_cache()
    yield
    updater.reset_update_cache()
    settings.update_repo_dir = original_repo
    settings.update_script = original_script
    settings.update_mode = original_mode


def _git_runner(*, behind: int = 2, latest_tag: str | None = LATEST_TAG):
    """Simulate git for tag mode, the default (`CUBOS_UPDATE_MODE=tag`)."""
    calls: list[tuple[str, ...]] = []

    def run(command: list[str]) -> str:
        calls.append(tuple(command))
        args = command[3:]
        if args == ["rev-parse", "HEAD"]:
            return CURRENT_SHA
        if args[:3] == ["fetch", "--quiet", "--tags"]:
            return ""
        if args == ["tag", "--list"]:
            tags = [CURRENT_TAG]
            if latest_tag is not None:
                tags.append(latest_tag)
            return "\n".join(tags)
        if args == ["describe", "--tags"]:
            return CURRENT_TAG
        if args[:3] == ["rev-list", "-n", "1"]:
            return LATEST_SHA if behind else CURRENT_SHA
        if args[:2] == ["rev-list", "--count"]:
            return str(behind)
        if args[:2] == ["log", "--oneline"]:
            return (
                "2222222 second change\n1111111 first change"
                if behind
                else ""
            )
        raise AssertionError(f"unexpected git command: {command}")

    return run, calls


def _branch_git_runner(*, behind: int = 2):
    """Simulate git for the legacy branch mode (`CUBOS_UPDATE_MODE=branch`)."""
    calls: list[tuple[str, ...]] = []

    def run(command: list[str]) -> str:
        calls.append(tuple(command))
        args = command[3:]
        if args == ["rev-parse", "HEAD"]:
            return CURRENT_SHA
        if args[:2] == ["fetch", "--quiet"]:
            return ""
        if args == ["rev-parse", "origin/main"]:
            return LATEST_SHA if behind else CURRENT_SHA
        if args[:2] == ["rev-list", "--count"]:
            return str(behind)
        if args[:2] == ["log", "--oneline"]:
            return (
                "2222222 second change\n1111111 first change"
                if behind
                else ""
            )
        raise AssertionError(f"unexpected git command: {command}")

    return run, calls


@pytest.mark.parametrize(("behind", "available"), [(2, True), (0, False)])
def test_get_update_status_reports_availability(monkeypatch, behind, available):
    run, _ = _git_runner(behind=behind)
    monkeypatch.setattr(updater, "_run_git", run)

    response = api_request(create_app(), "GET", "/api/v1/system/update")

    assert response.status_code == 200
    body = response.json()
    assert body["current_sha"] == CURRENT_SHA
    assert body["latest_sha"] == (LATEST_SHA if behind else CURRENT_SHA)
    assert body["commits_behind"] == behind
    assert body["update_available"] is available
    assert body["summary"] == (
        ["2222222 second change", "1111111 first change"] if behind else []
    )
    assert body["error"] is None
    assert body["current_tag"] == CURRENT_TAG
    assert body["latest_tag"] == LATEST_TAG


def test_get_update_status_reports_no_release_tags_yet(monkeypatch):
    """A repo with no calver tags at all (before the first weekly release)."""

    def run(command: list[str]) -> str:
        args = command[3:]
        if args == ["rev-parse", "HEAD"]:
            return CURRENT_SHA
        if args[:3] == ["fetch", "--quiet", "--tags"]:
            return ""
        if args == ["tag", "--list"]:
            return ""
        if args == ["describe", "--tags"]:
            raise subprocess.CalledProcessError(128, ["git"], stderr="No tags can describe")
        raise AssertionError(f"unexpected git command: {command}")

    monkeypatch.setattr(updater, "_run_git", run)

    response = api_request(create_app(), "GET", "/api/v1/system/update")

    assert response.status_code == 200
    body = response.json()
    assert body["update_available"] is False
    assert body["latest_tag"] is None
    assert body["current_tag"] is None
    assert body["error"] is None


def test_get_update_status_ignores_non_calver_tags(monkeypatch):
    run, _ = _git_runner(behind=1)

    def with_extra_tags(command: list[str]) -> str:
        args = command[3:]
        if args == ["tag", "--list"]:
            return f"{CURRENT_TAG}\n{LATEST_TAG}\nrelease-candidate\nv1"
        return run(command)

    monkeypatch.setattr(updater, "_run_git", with_extra_tags)

    response = api_request(create_app(), "GET", "/api/v1/system/update")

    assert response.json()["latest_tag"] == LATEST_TAG


def test_get_update_status_branch_mode_ignores_tags(monkeypatch):
    settings = get_settings()
    settings.update_mode = "branch"
    run, _ = _branch_git_runner(behind=2)
    monkeypatch.setattr(updater, "_run_git", run)

    response = api_request(create_app(), "GET", "/api/v1/system/update")

    assert response.status_code == 200
    body = response.json()
    assert body["current_sha"] == CURRENT_SHA
    assert body["latest_sha"] == LATEST_SHA
    assert body["commits_behind"] == 2
    assert body["update_available"] is True
    assert body["current_tag"] is None
    assert body["latest_tag"] is None


def test_get_update_status_caches_and_refresh_refetches(monkeypatch):
    run, calls = _git_runner()
    monkeypatch.setattr(updater, "_run_git", run)

    api_request(create_app(), "GET", "/api/v1/system/update")
    api_request(create_app(), "GET", "/api/v1/system/update")
    assert sum("fetch" in call for call in calls) == 1

    api_request(create_app(), "GET", "/api/v1/system/update?refresh=true")
    assert sum("fetch" in call for call in calls) == 2


def test_get_update_status_returns_git_failure_as_data(monkeypatch):
    def fail(_command: list[str]) -> str:
        raise subprocess.CalledProcessError(1, ["git"], stderr="network unavailable")

    monkeypatch.setattr(updater, "_run_git", fail)

    response = api_request(create_app(), "GET", "/api/v1/system/update")

    assert response.status_code == 200
    assert response.json()["update_available"] is False
    assert "network unavailable" in response.json()["error"]


def test_apply_update_rejects_active_run(monkeypatch):
    monkeypatch.setattr(
        system,
        "get_run_manager",
        lambda: SimpleNamespace(active_run_id="run-123"),
    )

    response = api_request(
        create_app(), "POST", "/api/v1/system/update/apply", json={}
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "cannot update while run run-123 is active"}


def test_apply_update_rejects_when_already_current(monkeypatch):
    monkeypatch.setattr(system, "get_run_manager", lambda: SimpleNamespace(active_run_id=None))
    monkeypatch.setattr(
        updater,
        "check_for_update",
        lambda: updater.UpdateStatus(
            current_sha=CURRENT_SHA,
            latest_sha=CURRENT_SHA,
            commits_behind=0,
            update_available=False,
            checked_at=1.0,
            summary=[],
            error=None,
        ),
    )

    response = api_request(
        create_app(), "POST", "/api/v1/system/update/apply", json={}
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "already up to date"}


def test_apply_update_launches_latest_sha(monkeypatch):
    monkeypatch.setattr(system, "get_run_manager", lambda: SimpleNamespace(active_run_id=None))
    status = updater.UpdateStatus(
        current_sha=CURRENT_SHA,
        latest_sha=LATEST_SHA,
        commits_behind=2,
        update_available=True,
        checked_at=1.0,
        summary=[],
        error=None,
    )
    monkeypatch.setattr(updater, "check_for_update", lambda: status)
    launched: list[str] = []
    monkeypatch.setattr(updater, "apply_update", launched.append)

    response = api_request(
        create_app(), "POST", "/api/v1/system/update/apply", json={}
    )

    assert response.status_code == 202
    assert response.json() == {"status": "updating", "target_sha": LATEST_SHA}
    assert launched == [LATEST_SHA]


def test_apply_update_returns_503_when_script_is_missing(monkeypatch):
    monkeypatch.setattr(system, "get_run_manager", lambda: SimpleNamespace(active_run_id=None))
    status = updater.UpdateStatus(
        current_sha=CURRENT_SHA,
        latest_sha=LATEST_SHA,
        commits_behind=1,
        update_available=True,
        checked_at=1.0,
        summary=[],
        error=None,
    )
    monkeypatch.setattr(updater, "check_for_update", lambda: status)

    def fail(_target_sha: str) -> None:
        raise updater.UpdateLaunchError("update script not found")

    monkeypatch.setattr(updater, "apply_update", fail)

    response = api_request(
        create_app(), "POST", "/api/v1/system/update/apply", json={}
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "update script not found"}


def test_systemd_launch_runs_updater_as_service_user(monkeypatch, tmp_path):
    """The transient unit must never run repo code as root."""
    script = tmp_path / "update.sh"
    script.write_text("#!/bin/bash\n")
    settings = get_settings()
    monkeypatch.setattr(settings, "update_script", script)
    monkeypatch.setattr(updater.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(updater.getpass, "getuser", lambda: "cub")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda command, **kwargs: commands.append(command),
    )

    updater.apply_update(LATEST_SHA)

    assert commands == [
        [
            "sudo",
            "systemd-run",
            "--uid=cub",
            "--unit=cubos-update",
            "--collect",
            str(script),
            LATEST_SHA,
        ]
    ]


def test_run_manager_exposes_active_run_id():
    manager = get_run_manager()
    assert manager.active_run_id is None
    with manager._lock:
        manager._active_run_id = "run-456"
    assert manager.active_run_id == "run-456"
