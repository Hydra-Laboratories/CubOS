"""Tests for pause and breakpoint protocol commands."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cubos.protocol_engine.registry import CommandRegistry


@pytest.fixture(autouse=True)
def _ensure_commands_registered():
    if "pause" not in CommandRegistry.instance().command_names:
        import cubos.protocol_engine.commands.pause  # noqa: F401


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.logger = MagicMock()
    return ctx


# ─── Pause command ────────────────────────────────────────────────────────────


class TestPauseCommand:


    def test_pause_with_reason(self):
        from cubos.protocol_engine.commands.pause import pause

        ctx = _make_context()
        with patch("cubos.protocol_engine.commands.pause.time.sleep"):
            pause(ctx, seconds=3.0, reason="waiting for reaction")
        # Verify reason is included in the log
        log_call_args = str(ctx.logger.info.call_args_list)
        assert "waiting for reaction" in log_call_args

        # Should not raise


# ─── Breakpoint command ──────────────────────────────────────────────────────


class TestBreakpointCommand:


    def test_breakpoint_non_tty_logs_and_continues_without_input(self):
        from cubos.protocol_engine.commands.pause import breakpoint_cmd

        ctx = _make_context()
        with patch("cubos.protocol_engine.commands.pause.sys.stdin.isatty", return_value=False), \
             patch("builtins.input") as mock_input:
            breakpoint_cmd(ctx, message="headless")

        mock_input.assert_not_called()
        ctx.logger.warning.assert_called()
