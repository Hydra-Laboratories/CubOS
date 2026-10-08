from __future__ import annotations

import threading
import time

import pytest

from cubos.instruments.camera.exceptions import (
    CameraCaptureError,
    CameraConnectionError,
)
from cubos.instruments.camera.frame_broker import (
    acquire_frame_broker,
    reset_frame_brokers,
)


class FakeFrame:
    shape = (600, 800, 3)

    def __init__(self, value: int) -> None:
        self.value = value


class FakeCapture:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.released = False
        self.read_count = 0
        self.values: dict[int, float] = {}
        self.release_event = threading.Event()

    def isOpened(self) -> bool:
        return not self.released

    def read(self):
        if self.fail:
            time.sleep(0.005)
            return False, None
        self.read_count += 1
        time.sleep(0.001)
        return True, FakeFrame(self.read_count)

    def release(self) -> None:
        self.released = True
        self.release_event.set()

    def get(self, property_id: int) -> float:
        return self.values.get(property_id, 0.0)

    def set(self, property_id: int, value: float) -> bool:
        self.values[property_id] = value
        return True


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_frame_brokers()
    yield
    reset_frame_brokers()


def test_one_capture_owner_is_shared_until_last_lease_releases():
    captures: list[FakeCapture] = []

    def open_capture() -> FakeCapture:
        capture = FakeCapture()
        captures.append(capture)
        return capture

    first = acquire_frame_broker(("opencv", 0), open_capture)
    second = acquire_frame_broker(("opencv", 0), open_capture)
    first_frame = first.latest(timeout_s=0.5)
    second_frame = second.latest(timeout_s=0.5)

    assert len(captures) == 1
    assert first_frame.frame_id > 0
    assert second_frame.frame_id >= first_frame.frame_id
    assert second_frame.width == 800
    assert second_frame.height == 600
    assert second_frame.received_at > 0

    first.release()
    assert captures[0].released is False
    assert second.is_open() is True

    second.release()
    assert captures[0].release_event.wait(0.5)


def test_wait_for_newer_frame_does_not_consume_other_readers_frame():
    capture = FakeCapture()
    first = acquire_frame_broker("camera", lambda: capture)
    second = acquire_frame_broker("camera", lambda: capture)
    initial = first.latest(timeout_s=0.5)

    newer = first.latest(after_frame_id=initial.frame_id, timeout_s=0.5)
    observed = second.latest(timeout_s=0.5)

    assert newer.frame_id > initial.frame_id
    assert observed.frame_id >= newer.frame_id
    first.release()
    second.release()


def test_property_access_is_serialized_through_shared_owner():
    capture = FakeCapture()
    lease = acquire_frame_broker("camera", lambda: capture)

    assert lease.set_property(15, -6.0) is True
    assert lease.get_property(15) == -6.0
    lease.release()


def test_existing_device_rejects_conflicting_capture_configuration():
    first = acquire_frame_broker(
        "camera",
        FakeCapture,
        configuration=((1280, 720), "MJPG"),
    )

    with pytest.raises(CameraConnectionError, match="different capture configuration"):
        acquire_frame_broker(
            "camera",
            FakeCapture,
            configuration=((800, 600), "YUYV"),
        )

    first.release()


def test_failed_reader_reports_a_bounded_freshness_error():
    capture = FakeCapture(fail=True)
    lease = acquire_frame_broker("camera", lambda: capture)

    with pytest.raises(CameraCaptureError, match="No fresh frame"):
        lease.latest(timeout_s=0.03)

    assert lease.last_error == "camera read returned no frame"
    lease.release()


def test_released_lease_cannot_read_shared_camera():
    lease = acquire_frame_broker("camera", FakeCapture)
    lease.latest(timeout_s=0.5)
    lease.release()

    with pytest.raises(CameraCaptureError, match="released"):
        lease.latest()
