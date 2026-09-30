"""Tests for the GRBL driver logger setup.

Regression guard: the driver subsystem must never silently discard errors.
Both helpers must attach a real file handler and, for the mill logger, a
console ERROR handler — even when no explicit ``path_to_logs`` is provided.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cubos.gantry.gantry_driver import logger as logger_module
from cubos.gantry.gantry_driver.logger import (
    GANTRY_LOG_DIR_ENV,
    set_up_command_logger,
    set_up_mill_logger,
)


@pytest.fixture(autouse=True)
def _isolate_loggers(tmp_path, monkeypatch):
    """Each test gets a clean logger state and tmp default log dir."""
    monkeypatch.setattr(logger_module, "DEFAULT_LOG_DIR", tmp_path / "default")
    # Env override must not leak in from the ambient environment (e.g. the
    # appliance image sets CUBOS_GANTRY_LOG_DIR) — tests exercise the default.
    monkeypatch.delenv(GANTRY_LOG_DIR_ENV, raising=False)
    for name in ("grbl_cnc_mill", "grbl_cnc_mill_cmds"):
        existing = logging.getLogger(name)
        for handler in list(existing.handlers):
            existing.removeHandler(handler)
            handler.close()
    yield
    for name in ("grbl_cnc_mill", "grbl_cnc_mill_cmds"):
        existing = logging.getLogger(name)
        for handler in list(existing.handlers):
            existing.removeHandler(handler)
            handler.close()


def _file_handlers(logger: logging.Logger) -> list[logging.FileHandler]:
    return [h for h in logger.handlers if isinstance(h, logging.FileHandler)]


def _console_handlers(logger: logging.Logger) -> list[logging.StreamHandler]:
    return [
        h
        for h in logger.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
    ]


def test_mill_logger_attaches_file_and_console_when_no_path(tmp_path, monkeypatch):
    monkeypatch.setattr(logger_module, "DEFAULT_LOG_DIR", tmp_path / "default")
    logger = set_up_mill_logger()
    file_handlers = _file_handlers(logger)
    assert len(file_handlers) == 1
    assert Path(file_handlers[0].baseFilename).name == "mill_control.log"
    console = _console_handlers(logger)
    assert len(console) == 1
    assert console[0].level <= logging.ERROR


def test_mill_logger_uses_explicit_path(tmp_path):
    logger = set_up_mill_logger(tmp_path / "explicit")
    file_handlers = _file_handlers(logger)
    assert any(
        Path(h.baseFilename).parent == tmp_path / "explicit" for h in file_handlers
    )


def test_mill_logger_is_idempotent(tmp_path):
    set_up_mill_logger(tmp_path)
    set_up_mill_logger(tmp_path)
    logger = logging.getLogger("grbl_cnc_mill")
    assert len(_file_handlers(logger)) == 1
    assert len(_console_handlers(logger)) == 1


def test_command_logger_attaches_file_when_no_path(tmp_path, monkeypatch):
    monkeypatch.setattr(logger_module, "DEFAULT_LOG_DIR", tmp_path / "default")
    logger = set_up_command_logger()
    file_handlers = _file_handlers(logger)
    assert len(file_handlers) == 1
    assert Path(file_handlers[0].baseFilename).name == "command.log"


def test_command_logger_is_idempotent(tmp_path):
    set_up_command_logger(tmp_path)
    set_up_command_logger(tmp_path)
    logger = logging.getLogger("grbl_cnc_mill_cmds")
    assert len(_file_handlers(logger)) == 1


def test_mill_logger_writes_error_to_file(tmp_path):
    logger = set_up_mill_logger(tmp_path)
    logger.error("alarm:1 hard limit")
    for handler in _file_handlers(logger):
        handler.flush()
    contents = (tmp_path / "mill_control.log").read_text()
    assert "alarm:1 hard limit" in contents


def test_command_logger_writes_command_to_file(tmp_path):
    logger = set_up_command_logger(tmp_path)
    logger.debug("G01 X10 F3000")
    for handler in _file_handlers(logger):
        handler.flush()
    contents = (tmp_path / "command.log").read_text()
    assert "G01 X10 F3000" in contents


def test_env_override_directs_default_log_dir(tmp_path, monkeypatch):
    override = tmp_path / "env-logs"
    monkeypatch.setenv(GANTRY_LOG_DIR_ENV, str(override))
    logger = set_up_mill_logger()
    file_handlers = _file_handlers(logger)
    assert len(file_handlers) == 1
    assert Path(file_handlers[0].baseFilename).parent == override


def test_unwritable_home_does_not_break_connect(tmp_path, monkeypatch):
    """Regression: an unwritable default HOME (e.g. ``/nonexistent`` in a
    hardened container) must not raise when the gantry connects — logging
    degrades to a temp dir instead of crashing set_up_*_logger."""
    unwritable = Path("/nonexistent/.cubos/logs/gantry")
    monkeypatch.setattr(logger_module, "DEFAULT_LOG_DIR", unwritable)
    fallback_root = tmp_path / "tmp"
    fallback_root.mkdir()
    monkeypatch.setattr(logger_module.tempfile, "gettempdir", lambda: str(fallback_root))

    # Must not raise, and must still attach a working file handler.
    mill = set_up_mill_logger()
    cmds = set_up_command_logger()

    mill_files = _file_handlers(mill)
    cmd_files = _file_handlers(cmds)
    assert len(mill_files) == 1
    assert len(cmd_files) == 1
    expected_dir = fallback_root / "cubos" / "logs" / "gantry"
    assert Path(mill_files[0].baseFilename).parent == expected_dir
    assert Path(cmd_files[0].baseFilename).parent == expected_dir

    # And the fallback log is actually writable.
    mill.error("alarm:2 soft limit")
    for handler in mill_files:
        handler.flush()
    assert "alarm:2 soft limit" in (expected_dir / "mill_control.log").read_text()
