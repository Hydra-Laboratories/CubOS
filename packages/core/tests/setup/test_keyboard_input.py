"""Tests for raw keyboard helpers."""

from __future__ import annotations

import pytest

from cubos.tools import keyboard_input


class _FakeStdin:
    def __init__(self, chars: str):
        self._chars = iter(chars)

    def read(self, _count: int) -> str:
        return next(self._chars)

    def fileno(self) -> int:
        return 3


def test_unix_raw_ctrl_c_raises_keyboard_interrupt(monkeypatch):
    monkeypatch.setattr(keyboard_input.sys, "stdin", _FakeStdin("\x03"))

    with pytest.raises(KeyboardInterrupt):
        keyboard_input._unix_read_one_key()


def test_unix_raw_lone_escape_returns_without_waiting_for_more_keys(monkeypatch):
    monkeypatch.setattr(keyboard_input.sys, "stdin", _FakeStdin("\x1b"))
    monkeypatch.setattr(keyboard_input.select, "select", lambda *_args: ([], [], []))

    assert keyboard_input._unix_read_one_key() == "\x1b"


@pytest.mark.parametrize(
    ("chars", "expected"),
    [
        ("\x1b[A", "UP"),
        ("\x1b[B", "DOWN"),
        ("\x1b[C", "RIGHT"),
        ("\x1b[D", "LEFT"),
        ("\x1b[1~", "\x1b"),
        ("q", "Q"),
    ],
)
def test_unix_raw_key_sequences(monkeypatch, chars, expected):
    availability = [True] * (len(chars) - 1) + [False]
    monkeypatch.setattr(keyboard_input.sys, "stdin", _FakeStdin(chars))
    monkeypatch.setattr(
        keyboard_input.select,
        "select",
        lambda *_args: ([keyboard_input.sys.stdin], [], []) if availability.pop(0) else ([], [], []),
    )

    assert keyboard_input._unix_read_one_key() == expected


def test_keypress_batch_preserves_next_different_key():
    keyboard_input._PENDING_KEYS.clear()
    keys = iter(["RIGHT", "\r"])
    first_availability = iter([True])

    first = keyboard_input._read_keypress_batch_impl(
        lambda: next(keys),
        lambda _timeout_s: next(first_availability),
        0.03,
    )
    second = keyboard_input._read_keypress_batch_impl(
        lambda: next(keys),
        lambda _timeout_s: False,
        0.03,
    )

    assert first == ("RIGHT", 1)
    assert second == ("\r", 1)


def test_unix_flush_stdin_discards_pending_and_buffered_keys(monkeypatch):
    keyboard_input._PENDING_KEYS.extend(["LEFT"])
    stdin = _FakeStdin("abc")
    availability = [True, True, True, False]
    monkeypatch.setattr(keyboard_input.sys, "stdin", stdin)
    monkeypatch.setattr(keyboard_input.termios, "tcgetattr", lambda _fd: ["old"])
    monkeypatch.setattr(keyboard_input.termios, "tcsetattr", lambda *_args: None)
    monkeypatch.setattr(keyboard_input.tty, "setraw", lambda _fd: None)
    monkeypatch.setattr(
        keyboard_input.select,
        "select",
        lambda *_args: ([stdin], [], []) if availability.pop(0) else ([], [], []),
    )

    keyboard_input._unix_flush_stdin()

    assert list(keyboard_input._PENDING_KEYS) == []


def test_read_keypress_restores_terminal(monkeypatch):
    stdin = _FakeStdin("z")
    calls: list[tuple] = []
    monkeypatch.setattr(keyboard_input.sys, "stdin", stdin)
    monkeypatch.setattr(keyboard_input.termios, "tcgetattr", lambda fd: ["old", fd])
    monkeypatch.setattr(keyboard_input.termios, "tcsetattr", lambda *args: calls.append(args))
    monkeypatch.setattr(keyboard_input.tty, "setraw", lambda fd: calls.append(("raw", fd)))

    assert keyboard_input.read_keypress() == "Z"
    assert ("raw", 3) in calls
    assert calls[-1][0] == 3


class _FakeMsvcrt:
    def __init__(self, keys: list[bytes]):
        self.keys = keys

    def getch(self) -> bytes:
        return self.keys.pop(0)

    def kbhit(self) -> bool:
        return bool(self.keys)


def test_windows_read_one_key_ctrl_c(monkeypatch):
    monkeypatch.setattr(keyboard_input, "msvcrt", _FakeMsvcrt([b"\x03"]), raising=False)

    with pytest.raises(KeyboardInterrupt):
        keyboard_input._windows_read_one_key()


def test_windows_flush_stdin_discards_buffer(monkeypatch):
    fake = _FakeMsvcrt([b"a", b"b"])
    keyboard_input._PENDING_KEYS.extend(["UP"])
    monkeypatch.setattr(keyboard_input, "msvcrt", fake, raising=False)

    keyboard_input._windows_flush_stdin()

    assert fake.keys == []
    assert list(keyboard_input._PENDING_KEYS) == []


def test_windows_read_keypress_batch_counts_repeats(monkeypatch):
    fake = _FakeMsvcrt([b"x", b"x"])
    monkeypatch.setattr(keyboard_input, "msvcrt", fake, raising=False)

    assert keyboard_input._windows_read_keypress_batch() == ("X", 2)


def test_public_read_keypress_and_batch_route_to_windows(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(keyboard_input, "_IS_WINDOWS", True)
    monkeypatch.setattr(keyboard_input, "_windows_read_one_key", lambda: calls.append("one") or "Q")
    monkeypatch.setattr(keyboard_input, "_windows_read_keypress_batch", lambda: calls.append("batch") or ("Q", 1))
    monkeypatch.setattr(keyboard_input, "_windows_flush_stdin", lambda: calls.append("flush"))

    assert keyboard_input.read_keypress() == "Q"
    assert keyboard_input.read_keypress_batch() == ("Q", 1)
    keyboard_input.flush_stdin()
    assert calls == ["one", "batch", "flush"]
