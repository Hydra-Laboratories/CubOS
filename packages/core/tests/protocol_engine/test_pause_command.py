"""Tests for pause and breakpoint protocol commands."""

from __future__ import annotations

import importlib
import time
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

    def test_pause_is_registered(self):
        assert "pause" in CommandRegistry.instance().command_names

    def test_pause_sleeps_for_duration(self):
        from cubos.protocol_engine.commands.pause import pause

        ctx = _make_context()
        with patch("cubos.protocol_engine.commands.pause.time.sleep") as mock_sleep:
            pause(ctx, seconds=5.0)
            mock_sleep.assert_called_once_with(5.0)

    def test_pause_logs_message(self):
        from cubos.protocol_engine.commands.pause import pause

        ctx = _make_context()
        with patch("cubos.protocol_engine.commands.pause.time.sleep"):
            pause(ctx, seconds=10.0)
        ctx.logger.info.assert_called()

    def test_pause_with_reason(self):
        from cubos.protocol_engine.commands.pause import pause

        ctx = _make_context()
        with patch("cubos.protocol_engine.commands.pause.time.sleep"):
            pause(ctx, seconds=3.0, reason="waiting for reaction")
        # Verify reason is included in the log
        log_call_args = str(ctx.logger.info.call_args_list)
        assert "waiting for reaction" in log_call_args

    def test_pause_default_reason(self):
        from cubos.protocol_engine.commands.pause import pause

        ctx = _make_context()
        with patch("cubos.protocol_engine.commands.pause.time.sleep"):
            pause(ctx, seconds=1.0)
        # Should not raise

    def test_pause_zero_seconds(self):
        from cubos.protocol_engine.commands.pause import pause

        ctx = _make_context()
        with patch("cubos.protocol_engine.commands.pause.time.sleep") as mock_sleep:
            pause(ctx, seconds=0.0)
            mock_sleep.assert_called_once_with(0.0)

    def test_photo_pause_emits_completion_after_sleep(self):
        from cubos.protocol_engine.commands.pause import photo_pause

        ctx = _make_context()
        timeline = []
        ctx.notify_step.side_effect = lambda *args, **kwargs: timeline.append((args, kwargs))
        with patch(
            "cubos.protocol_engine.commands.pause.time.sleep",
            side_effect=lambda seconds: timeline.append(("sleep", seconds)),
        ) as mock_sleep:
            photo_pause(
                ctx, settle_seconds=2.0, capture_hold_seconds=2.0, well="plate.A4",
            )

        assert mock_sleep.call_args_list == [((2.0,),), ((2.0,),)]
        assert timeline == [
            ("sleep", 2.0),
            (("photo_pause_completed",), {
                "seconds": 2.0,
                "capture_hold_seconds": 2.0,
                "well": "plate.A4",
            }),
            ("sleep", 2.0),
        ]
        ctx.notify_step.assert_called_once_with(
            "photo_pause_completed", seconds=2.0, capture_hold_seconds=2.0,
            well="plate.A4",
        )


# ─── Breakpoint command ──────────────────────────────────────────────────────


class TestBreakpointCommand:

    def test_breakpoint_is_registered(self):
        assert "breakpoint" in CommandRegistry.instance().command_names

    def test_breakpoint_waits_for_input(self):
        from cubos.protocol_engine.commands.pause import breakpoint_cmd

        ctx = _make_context()
        with patch("cubos.protocol_engine.commands.pause.sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="") as mock_input:
            breakpoint_cmd(ctx)
            mock_input.assert_called_once()

    def test_breakpoint_with_message(self):
        from cubos.protocol_engine.commands.pause import breakpoint_cmd

        ctx = _make_context()
        with patch("cubos.protocol_engine.commands.pause.sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="") as mock_input:
            breakpoint_cmd(ctx, message="Check plate alignment")
            call_arg = mock_input.call_args[0][0]
            assert "Check plate alignment" in call_arg

    def test_breakpoint_default_message(self):
        from cubos.protocol_engine.commands.pause import breakpoint_cmd

        ctx = _make_context()
        with patch("cubos.protocol_engine.commands.pause.sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="") as mock_input:
            breakpoint_cmd(ctx)
            call_arg = mock_input.call_args[0][0]
            assert "Press Enter" in call_arg

    def test_breakpoint_logs(self):
        from cubos.protocol_engine.commands.pause import breakpoint_cmd

        ctx = _make_context()
        with patch("cubos.protocol_engine.commands.pause.sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value=""):
            breakpoint_cmd(ctx, message="test")
        ctx.logger.info.assert_called()

    def test_breakpoint_non_tty_logs_and_continues_without_input(self):
        from cubos.protocol_engine.commands.pause import breakpoint_cmd

        ctx = _make_context()
        with patch("cubos.protocol_engine.commands.pause.sys.stdin.isatty", return_value=False), \
             patch("builtins.input") as mock_input:
            breakpoint_cmd(ctx, message="headless")

        mock_input.assert_not_called()
        ctx.logger.warning.assert_called()
