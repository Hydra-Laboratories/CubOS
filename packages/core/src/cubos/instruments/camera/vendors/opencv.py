"""USB webcam camera driver via OpenCV (optional opencv-python dependency)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from cubos.instruments.camera.exceptions import (
    CameraCaptureError,
    CameraConnectionError,
)
from cubos.instruments.camera.interface import CameraInstrument
from cubos.instruments.camera.placeholder import write_placeholder_png

_AUTO_DETECT_MAX_INDEX = 10


class OpenCVCamera(CameraInstrument):
    """USB webcam driven through ``cv2.VideoCapture``.

    ``camera_id`` < 0 (the default) auto-detects the first responsive
    device index at connect time.
    """

    def __init__(
        self,
        camera_id: int = -1,
        resolution_width: int = 1280,
        resolution_height: int = 720,
        name: Optional[str] = None,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
        depth: float = 0.0,
        offline: bool = False,
        **kwargs,
    ):
        super().__init__(
            name=name,
            offset_x=offset_x,
            offset_y=offset_y,
            depth=depth,
            offline=offline,
        )
        self.camera_id = camera_id
        self.resolution = (resolution_width, resolution_height)
        self._capture = None

    # ── BaseInstrument interface ──────────────────────────────────────────

    def connect(self) -> None:
        if self._offline:
            self.logger.info("OpenCV camera connected (offline)")
            return
        import cv2

        camera_id = self.camera_id
        if camera_id < 0:
            camera_id = self._detect_camera(cv2)
        capture = cv2.VideoCapture(camera_id)
        if not capture.isOpened():
            capture.release()
            raise CameraConnectionError(
                f"Cannot open webcam at index {camera_id}."
            )
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
        self._capture = capture
        self.camera_id = camera_id
        self.logger.info("Connected to webcam ID %d", camera_id)

    def disconnect(self) -> None:
        if self._offline:
            self.logger.info("OpenCV camera disconnected (offline)")
            return
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        self.logger.info("Disconnected from webcam")

    def health_check(self) -> bool:
        if self._offline:
            return True
        return self._capture is not None and self._capture.isOpened()

    # ── CameraInstrument interface ────────────────────────────────────────

    def capture(self, *args: Any, save_path: str = "", **kwargs: Any) -> str:
        """Capture one frame and save it to *save_path*; return the path."""
        if not save_path:
            raise CameraCaptureError("capture requires a save_path.")
        if self._offline:
            return str(write_placeholder_png(save_path))
        if self._capture is None or not self._capture.isOpened():
            raise CameraCaptureError("Cannot capture image: camera not connected.")
        import cv2

        ok, frame = self._capture.read()
        if not ok:
            raise CameraCaptureError("Failed to capture image from webcam.")
        target = Path(save_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(target), frame):
            raise CameraCaptureError(f"Failed to write image to {target}")
        self.logger.info("Image saved to %s", target)
        return str(target)

    # ── Private helpers ───────────────────────────────────────────────────

    def _detect_camera(self, cv2) -> int:
        for index in range(_AUTO_DETECT_MAX_INDEX):
            candidate = cv2.VideoCapture(index)
            if candidate.isOpened():
                candidate.release()
                self.logger.info("Auto-detected webcam at index %d", index)
                return index
            candidate.release()
        raise CameraConnectionError(
            f"No responsive webcam found in indexes 0..{_AUTO_DETECT_MAX_INDEX - 1}."
        )
