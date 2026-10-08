"""Single-reader, reference-counted frame acquisition for live cameras."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Hashable

from cubos.instruments.camera.exceptions import (
    CameraCaptureError,
    CameraConnectionError,
)


@dataclass(frozen=True)
class CameraFrame:
    """One immutable reference to the newest acquired camera frame."""

    frame_id: int
    received_at: float
    data: Any
    width: int
    height: int
    configuration_revision: int
    capture_metadata: dict[str, Any]


class FrameBroker:
    """Own one capture handle and keep only its newest frame in memory."""

    def __init__(
        self,
        capture: Any,
        *,
        clock: Callable[[], float] = time.time,
        retry_interval_s: float = 0.02,
        metadata_reader: Callable[[Any], dict[str, Any]] | None = None,
    ) -> None:
        self._capture = capture
        self._clock = clock
        self._retry_interval_s = retry_interval_s
        self._metadata_reader = metadata_reader
        self._condition = threading.Condition()
        self._capture_lock = threading.Lock()
        self._stop = threading.Event()
        self._latest: CameraFrame | None = None
        self._last_error: str | None = None
        self._configuration_revision = 0
        self._thread = threading.Thread(
            target=self._read_loop,
            name="cubos-camera-frame-broker",
            daemon=True,
        )
        self._thread.start()

    @property
    def last_error(self) -> str | None:
        with self._condition:
            return self._last_error

    def is_open(self) -> bool:
        try:
            return not self._stop.is_set() and bool(self._capture.isOpened())
        except Exception:
            return False

    def latest(
        self,
        *,
        after_frame_id: int | None = None,
        timeout_s: float = 0.0,
    ) -> CameraFrame:
        """Return the newest frame, optionally waiting for a newer frame."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._condition:
            while self._latest is None or (
                after_frame_id is not None
                and self._latest.frame_id <= after_frame_id
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    detail = f": {self._last_error}" if self._last_error else ""
                    raise CameraCaptureError(
                        f"No fresh frame was available within {timeout_s:.2f}s{detail}"
                    )
                self._condition.wait(remaining)
            return self._latest

    def get_property(self, property_id: int) -> float:
        with self._capture_lock:
            return float(self._capture.get(property_id))

    def set_property(self, property_id: int, value: float) -> bool:
        with self._capture_lock:
            accepted = bool(self._capture.set(property_id, value))
            if accepted:
                self._configuration_revision += 1
            return accepted

    @property
    def configuration_revision(self) -> int:
        with self._capture_lock:
            return self._configuration_revision

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self._thread.join(timeout=0.5)
        if self._thread.is_alive():
            self._capture.release()
            self._thread.join(timeout=0.5)
        else:
            with self._capture_lock:
                self._capture.release()
        with self._condition:
            self._condition.notify_all()

    def _read_loop(self) -> None:
        frame_id = 0
        while not self._stop.is_set():
            try:
                with self._capture_lock:
                    ok, frame = self._capture.read()
                    capture_metadata = (
                        self._metadata_reader(self._capture)
                        if ok and frame is not None and self._metadata_reader is not None
                        else {}
                    )
                    configuration_revision = self._configuration_revision
            except Exception as exc:
                ok, frame = False, None
                error = f"{type(exc).__name__}: {exc}"
            else:
                error = None if ok and frame is not None else "camera read returned no frame"

            if ok and frame is not None:
                frame_id += 1
                height, width = _frame_dimensions(frame, self._capture)
                snapshot = CameraFrame(
                    frame_id=frame_id,
                    received_at=self._clock(),
                    data=frame,
                    width=width,
                    height=height,
                    configuration_revision=configuration_revision,
                    capture_metadata=capture_metadata,
                )
                with self._condition:
                    self._latest = snapshot
                    self._last_error = None
                    self._condition.notify_all()
                continue

            with self._condition:
                self._last_error = error
                self._condition.notify_all()
            self._stop.wait(self._retry_interval_s)


def _frame_dimensions(frame: Any, capture: Any) -> tuple[int, int]:
    shape = getattr(frame, "shape", None)
    if shape is not None and len(shape) >= 2:
        return int(shape[0]), int(shape[1])
    try:
        return int(capture.get(4)), int(capture.get(3))
    except Exception:
        return 0, 0


class FrameLease:
    """A caller's reference to a shared frame broker."""

    def __init__(self, key: Hashable, broker: FrameBroker) -> None:
        self.key = key
        self._broker = broker
        self._released = False

    @property
    def last_error(self) -> str | None:
        return self._broker.last_error

    def is_open(self) -> bool:
        return not self._released and self._broker.is_open()

    def latest(
        self,
        *,
        after_frame_id: int | None = None,
        timeout_s: float = 0.0,
    ) -> CameraFrame:
        if self._released:
            raise CameraCaptureError("Camera frame lease has been released.")
        return self._broker.latest(
            after_frame_id=after_frame_id,
            timeout_s=timeout_s,
        )

    def get_property(self, property_id: int) -> float:
        if self._released:
            raise CameraCaptureError("Camera frame lease has been released.")
        return self._broker.get_property(property_id)

    def set_property(self, property_id: int, value: float) -> bool:
        if self._released:
            raise CameraCaptureError("Camera frame lease has been released.")
        return self._broker.set_property(property_id, value)

    @property
    def configuration_revision(self) -> int:
        if self._released:
            raise CameraCaptureError("Camera frame lease has been released.")
        return self._broker.configuration_revision

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        release_frame_broker(self.key, self._broker)


@dataclass
class _RegistryEntry:
    broker: FrameBroker
    references: int
    configuration: Hashable | None


_registry: dict[Hashable, _RegistryEntry] = {}
_registry_lock = threading.Lock()


def acquire_frame_broker(
    key: Hashable,
    open_capture: Callable[[], Any],
    *,
    configuration: Hashable | None = None,
    metadata_reader: Callable[[Any], dict[str, Any]] | None = None,
) -> FrameLease:
    """Acquire one reference to the shared capture owner for *key*."""
    with _registry_lock:
        entry = _registry.get(key)
        if entry is None:
            broker = FrameBroker(
                open_capture(),
                metadata_reader=metadata_reader,
            )
            entry = _RegistryEntry(
                broker=broker,
                references=0,
                configuration=configuration,
            )
            _registry[key] = entry
        elif entry.configuration != configuration:
            raise CameraConnectionError(
                f"Camera {key!r} is already open with a different capture "
                "configuration. Stop the existing monitor before changing "
                "resolution or pixel format."
            )
        entry.references += 1
        return FrameLease(key, entry.broker)


def release_frame_broker(key: Hashable, broker: FrameBroker) -> None:
    should_close = False
    with _registry_lock:
        entry = _registry.get(key)
        if entry is None or entry.broker is not broker:
            return
        entry.references -= 1
        if entry.references <= 0:
            del _registry[key]
            should_close = True
    if should_close:
        broker.close()


def reset_frame_brokers() -> None:
    """Release all capture handles, primarily for process shutdown and tests."""
    with _registry_lock:
        brokers = [entry.broker for entry in _registry.values()]
        _registry.clear()
    for broker in brokers:
        broker.close()


def active_frame_broker_keys() -> tuple[Hashable, ...]:
    """Return a point-in-time inventory without acquiring any device."""
    with _registry_lock:
        return tuple(_registry)


__all__ = [
    "CameraFrame",
    "FrameLease",
    "active_frame_broker_keys",
    "acquire_frame_broker",
    "reset_frame_brokers",
]
