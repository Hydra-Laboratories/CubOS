"""Tests for the step-execution observability seam.

The load-bearing guarantee here is the *safety* one: an observer is advisory,
so a broken or hostile observer must not be able to change what a protocol
does. Everything else is reporting fidelity.
"""

import logging
from unittest.mock import MagicMock

import pytest

from cubos.protocol_engine.observer import notify
from cubos.protocol_engine.protocol import Protocol
from cubos.protocol_engine.runtime import ProtocolContext, ProtocolStep


class RecordingObserver:
    """Captures hook calls as ``(hook, index, command, substep)`` tuples."""

    def __init__(self):
        self.calls = []

    def _record(self, hook):
        def hook_fn(*, index, command, substep, **extra):
            self.calls.append((hook, index, command, substep))
            self.extra = extra
        return hook_fn

    def __getattr__(self, name):
        if name.startswith("step_"):
            return self._record(name)
        raise AttributeError(name)


class ExplodingObserver:
    """Raises on every hook — the adversary for the safety guarantee."""

    def __getattr__(self, name):
        def hook_fn(**_kwargs):
            raise RuntimeError(f"observer exploded in {name}")
        return hook_fn


def _context(observer=None):
    return ProtocolContext(
        gantry=MagicMock(),
        deck=MagicMock(),
        logger=logging.getLogger("test_step_observer"),
        step_observer=observer,
    )


def _protocol(*handlers):
    steps = [
        ProtocolStep(
            index=index, command_name=f"cmd{index}", handler=handler, args={},
        )
        for index, handler in enumerate(handlers)
    ]
    return Protocol(steps=steps)


class TestNotify:

    def test_none_observer_is_a_noop(self):
        notify(None, "step_started", index=0, command="x", substep=None)

    def test_swallows_observer_exceptions(self):
        notify(ExplodingObserver(), "step_started", index=0, command="x", substep=None)

    def test_swallows_missing_hook(self):
        class Partial:
            def step_started(self, **_kwargs):
                pass

        notify(Partial(), "step_completed", index=0, command="x", substep=None)

    def test_forwards_keyword_arguments(self):
        observer = MagicMock()
        notify(observer, "step_completed", index=2, command="mix", substep="leg1")
        observer.step_completed.assert_called_once_with(
            index=2, command="mix", substep="leg1",
        )


class TestStepEvents:

    def test_started_then_completed_for_each_step(self):
        observer = RecordingObserver()
        protocol = _protocol(MagicMock(), MagicMock(), MagicMock())
        protocol.execute(_context(observer))
        assert observer.calls == [
            ("step_started", 0, "cmd0", None),
            ("step_completed", 0, "cmd0", None),
            ("step_started", 1, "cmd1", None),
            ("step_completed", 1, "cmd1", None),
            ("step_started", 2, "cmd2", None),
            ("step_completed", 2, "cmd2", None),
        ]

    def test_failure_emits_step_failed_and_still_raises(self):
        observer = RecordingObserver()

        def boom(_context):
            raise ValueError("nope")

        protocol = _protocol(MagicMock(), boom, MagicMock())
        with pytest.raises(ValueError, match="nope"):
            protocol.execute(_context(observer))
        assert observer.calls[-1] == ("step_failed", 1, "cmd1", None)
        # The third step never ran, so it must not appear at all.
        assert not [call for call in observer.calls if call[1] == 2]

    def test_failed_carries_the_exception_message(self):
        captured = {}

        class Observer:
            def step_started(self, **_kwargs):
                pass

            def step_failed(self, *, error, **_kwargs):
                captured["error"] = error

        def boom(_context):
            raise ValueError("plunger stalled")

        with pytest.raises(ValueError):
            _protocol(boom).execute(_context(Observer()))
        assert captured["error"] == "ValueError: plunger stalled"

    def test_completed_reports_a_duration(self):
        captured = {}

        class Observer:
            def step_started(self, **_kwargs):
                pass

            def step_completed(self, *, duration_s, **_kwargs):
                captured["duration_s"] = duration_s

        _protocol(MagicMock()).execute(_context(Observer()))
        assert captured["duration_s"] >= 0.0

    def test_no_observer_changes_nothing(self):
        handler = MagicMock(return_value="value")
        results = _protocol(handler).execute(_context(None))
        assert results == ["value"]
        handler.assert_called_once()


class TestObserverCannotBreakRuns:

    def test_exploding_observer_does_not_change_results(self):
        handlers = [MagicMock(return_value=index) for index in range(3)]
        with_observer = _protocol(*handlers).execute(_context(ExplodingObserver()))
        assert with_observer == [0, 1, 2]

    def test_exploding_observer_does_not_mask_a_real_failure(self):
        def boom(_context):
            raise ValueError("real failure")

        # The protocol's own exception must survive, not be replaced by the
        # observer's — otherwise a broken UI would rewrite hardware errors.
        with pytest.raises(ValueError, match="real failure"):
            _protocol(boom).execute(_context(ExplodingObserver()))

    def test_exploding_observer_restores_step_scope(self):
        context = _context(ExplodingObserver())
        _protocol(MagicMock()).execute(context)
        assert context.active_step_index is None
        assert context.active_step_command is None
        assert context.active_substep is None


class TestNotifyStep:

    def test_noop_outside_a_step(self):
        observer = MagicMock()
        context = _context(observer)
        context.notify_step("step_started")
        observer.step_started.assert_not_called()

    def test_uses_the_active_substep(self):
        observer = RecordingObserver()
        context = _context(observer)

        def handler(ctx):
            ctx.active_substep = "leg2"
            ctx.notify_step("step_skipped", reason="already applied")

        _protocol(handler).execute(context)
        assert ("step_skipped", 0, "cmd0", "leg2") in observer.calls


class TestSubstepScope:
    """`_substep_scope` is the single emission point for every compound
    command (serial_transfer, flush_pipette, rinse_well, clear_well), so
    exercising it directly covers all of them."""

    def test_emits_nested_substep_names(self):
        from cubos.protocol_engine.commands.pipette import _substep_scope

        observer = RecordingObserver()
        context = _context(observer)

        def handler(ctx):
            with _substep_scope(ctx, "leg0"):
                with _substep_scope(ctx, "fill"):
                    pass

        _protocol(handler).execute(context)
        substeps = [call[3] for call in observer.calls]
        assert "leg0" in substeps
        assert "leg0:fill" in substeps

    def test_restores_the_previous_substep_on_failure(self):
        from cubos.protocol_engine.commands.pipette import _substep_scope

        observer = RecordingObserver()
        context = _context(observer)

        def handler(ctx):
            with _substep_scope(ctx, "leg0"):
                raise ValueError("leg failed")

        with pytest.raises(ValueError, match="leg failed"):
            _protocol(handler).execute(context)
        assert ("step_failed", 0, "cmd0", "leg0") in observer.calls
        assert context.active_substep is None
