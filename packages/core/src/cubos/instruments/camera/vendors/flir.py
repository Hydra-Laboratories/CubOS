"""FLIR camera driver, saving frames through OpenCV.

FLIR/Point Grey hardware is one vendor; PySpin and GenTL are two different
SDK bindings for talking to it, selected via ``backend``, not two different
vendors:

- ``backend="pyspin"`` (default): Spinnaker SDK's proprietary Python
  bindings. Ships with the Spinnaker SDK (manual install from Teledyne
  FLIR, not pip-installable) and is tied to a single CPython ABI per
  release (e.g. the 4.2.0.46 wheel is cp310-only) — unusable from a venv
  on a different Python minor version.
- ``backend="gentl"``: the standard GenICam/GenTL producer library that
  ships alongside Spinnaker (``Spinnaker_GenTL.cti``), driven through the
  pure-Python ``harvesters`` package. The producer is a plain C shared
  library with no Python ABI coupling, so this works from any CPython
  version the SDK's own Spinnaker package is installed for — the one to
  use when PySpin's wheel doesn't match the running interpreter.

Both backends are imported lazily, so offline use needs neither.
"""

from __future__ import annotations

import gc
import os
from pathlib import Path
from typing import Any, Optional

from cubos.instruments.camera.exceptions import (
    CameraCaptureError,
    CameraConfigError,
    CameraConnectionError,
)
from cubos.instruments.camera.interface import CameraInstrument
from cubos.instruments.camera.placeholder import write_placeholder_png

_PYSPIN_GRAB_TIMEOUT_MS = 1000
_GENTL_GRAB_TIMEOUT_S = 3.0
_BACKENDS = ("pyspin", "gentl")


class FlirCamera(CameraInstrument):
    """FLIR/Point Grey camera, via PySpin or GenTL (see module docstring)."""

    def __init__(
        self,
        camera_id: int = 0,
        backend: str = "pyspin",
        gentl_cti_path: Optional[str] = None,
        name: Optional[str] = None,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
        depth: float = 0.0,
        offline: bool = False,
        **kwargs,
    ):
        if backend not in _BACKENDS:
            raise CameraConfigError(
                f"Unknown FLIR backend {backend!r}; expected one of {_BACKENDS}."
            )
        super().__init__(
            name=name,
            offset_x=offset_x,
            offset_y=offset_y,
            depth=depth,
            offline=offline,
        )
        self.camera_id = camera_id
        self.backend = backend
        self.gentl_cti_path = gentl_cti_path
        # pyspin state
        self._system = None
        self._camera_list = None
        self._camera = None
        # gentl state
        self._harvester = None
        self._acquirer = None
        self._connected = False

    @staticmethod
    def is_available(backend: str = "pyspin") -> bool:
        try:
            if backend == "pyspin":
                import PySpin  # noqa: F401
            elif backend == "gentl":
                import harvesters.core  # noqa: F401
            else:
                return False
        except ImportError:
            return False
        return True

    # ── BaseInstrument interface ──────────────────────────────────────────

    def connect(self) -> None:
        if self._offline:
            self._connected = True
            self.logger.info("FLIR camera connected (offline)")
            return
        if self.backend == "pyspin":
            self._connect_pyspin()
        else:
            self._connect_gentl()
        self._connected = True
        self.logger.info(
            "Connected to FLIR camera ID %d via %s", self.camera_id, self.backend
        )

    def disconnect(self) -> None:
        self._connected = False
        if self._offline:
            self.logger.info("FLIR camera disconnected (offline)")
            return
        if self.backend == "pyspin":
            self._release_pyspin_handles()
        else:
            self._release_gentl_handles()
        self.logger.info("Disconnected from FLIR camera")

    def health_check(self) -> bool:
        if self._offline:
            return True
        if self.backend == "pyspin":
            return self._connected and self._camera is not None
        return self._connected and self._acquirer is not None

    # ── CameraInstrument interface ────────────────────────────────────────

    def capture(self, *args: Any, save_path: str = "", **kwargs: Any) -> str:
        """Capture one frame and save it to *save_path*; return the path."""
        if not save_path:
            raise CameraCaptureError("capture requires a save_path.")
        if self._offline:
            return str(write_placeholder_png(save_path))
        if not self._connected:
            raise CameraCaptureError("Cannot capture image: camera not connected.")

        if self.backend == "pyspin":
            image_rgb = self._capture_pyspin()
        else:
            image_rgb = self._capture_gentl()

        import cv2

        target = Path(save_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(target), cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)):
            raise CameraCaptureError(f"Failed to write image to {target}")
        self.logger.info("Image saved to %s", target)
        return str(target)

    # ── PySpin backend ────────────────────────────────────────────────────

    def _connect_pyspin(self) -> None:
        if self._camera is not None:
            return
        import PySpin

        try:
            self._system = PySpin.System.GetInstance()
            self._camera_list = self._system.GetCameras()
            count = self._camera_list.GetSize()
            if count == 0:
                self._release_pyspin_handles()
                raise CameraConnectionError("No FLIR cameras found.")
            if self.camera_id < count:
                self._camera = self._camera_list[self.camera_id]
            else:
                self.logger.warning(
                    "Camera ID %d out of range (%d found); using first camera",
                    self.camera_id, count,
                )
                self._camera = self._camera_list[0]
            self._camera.Init()
        except PySpin.SpinnakerException as exc:
            self._release_pyspin_handles()
            raise CameraConnectionError(
                f"Error connecting to FLIR camera: {exc}"
            ) from exc

    def _capture_pyspin(self):
        import cv2  # noqa: F401 - capture() imports it too; keep the ImportError local
        import PySpin

        if self._camera is None:
            raise CameraCaptureError("Cannot capture image: camera not connected.")
        try:
            # Use the camera's built-in RGB8 color processing.
            nodemap = self._camera.GetNodeMap()
            pixel_format = PySpin.CEnumerationPtr(nodemap.GetNode("PixelFormat"))
            if PySpin.IsAvailable(pixel_format) and PySpin.IsWritable(pixel_format):
                rgb8_entry = pixel_format.GetEntryByName("RGB8")
                if rgb8_entry and PySpin.IsAvailable(rgb8_entry):
                    pixel_format.SetIntValue(rgb8_entry.GetValue())

            self._camera.BeginAcquisition()
            try:
                image_result = self._camera.GetNextImage(_PYSPIN_GRAB_TIMEOUT_MS)
                try:
                    if image_result.IsIncomplete():
                        raise CameraCaptureError(
                            "Image incomplete with status "
                            f"{image_result.GetImageStatus()}"
                        )
                    return image_result.GetNDArray()
                finally:
                    image_result.Release()
            finally:
                try:
                    self._camera.EndAcquisition()
                except PySpin.SpinnakerException:
                    pass
        except PySpin.SpinnakerException as exc:
            raise CameraCaptureError(
                f"Error capturing image from FLIR camera: {exc}"
            ) from exc

    def _release_pyspin_handles(self) -> None:
        """Tear down PySpin handles in strict reverse order.

        PySpin segfaults if the system instance is released while camera or
        list handles are alive — the ``del`` calls and final ``gc.collect()``
        are load-bearing.
        """
        try:
            import PySpin
        except ImportError:
            return
        if self._camera is not None:
            try:
                self._camera.DeInit()
            except PySpin.SpinnakerException as exc:
                self.logger.warning("Error de-initializing camera: %s", exc)
            finally:
                del self._camera
                self._camera = None
        if self._camera_list is not None:
            try:
                self._camera_list.Clear()
            except PySpin.SpinnakerException as exc:
                self.logger.warning("Error clearing camera list: %s", exc)
            finally:
                del self._camera_list
                self._camera_list = None
        if self._system is not None:
            try:
                self._system.ReleaseInstance()
            except PySpin.SpinnakerException as exc:
                self.logger.warning("Error releasing system instance: %s", exc)
            finally:
                del self._system
                self._system = None
        gc.collect()

    # ── GenTL backend ─────────────────────────────────────────────────────

    def _resolve_cti_path(self) -> str:
        cti_path = self.gentl_cti_path or os.environ.get("SPINNAKER_GENTL64_CTI")
        if not cti_path:
            raise CameraConfigError(
                "FLIR backend=gentl needs a GenTL producer (.cti) path — set "
                "gentl_cti_path in the instrument config or the "
                "SPINNAKER_GENTL64_CTI environment variable (the Spinnaker "
                "installer sets this by default, e.g. "
                "/opt/spinnaker/lib/spinnaker-gentl/Spinnaker_GenTL.cti)."
            )
        return cti_path

    def _connect_gentl(self) -> None:
        if self._acquirer is not None:
            return
        from harvesters.core import Harvester

        cti_path = self._resolve_cti_path()
        harvester = Harvester()
        try:
            harvester.add_file(cti_path)
            harvester.update()
            count = len(harvester.device_info_list)
            if count == 0:
                raise CameraConnectionError("No FLIR cameras found.")
            index = self.camera_id
            if index >= count:
                self.logger.warning(
                    "Camera ID %d out of range (%d found); using first camera",
                    self.camera_id, count,
                )
                index = 0
            acquirer = harvester.create(index)
            acquirer.remote_device.node_map.PixelFormat.value = "RGB8"
        except Exception as exc:
            harvester.reset()
            raise CameraConnectionError(
                f"Error connecting to FLIR camera via GenTL: {exc}"
            ) from exc
        self._harvester = harvester
        self._acquirer = acquirer

    def _capture_gentl(self):
        if self._acquirer is None:
            raise CameraCaptureError("Cannot capture image: camera not connected.")
        try:
            self._acquirer.start()
        except Exception as exc:
            raise CameraCaptureError(
                f"Error capturing image from FLIR camera: {exc}"
            ) from exc
        try:
            with self._acquirer.fetch(timeout=_GENTL_GRAB_TIMEOUT_S) as buffer:
                component = buffer.payload.components[0]
                return component.data.reshape(
                    component.height, component.width, 3
                ).copy()
        except Exception as exc:
            raise CameraCaptureError(
                f"Error capturing image from FLIR camera: {exc}"
            ) from exc
        finally:
            self._acquirer.stop()

    def _release_gentl_handles(self) -> None:
        if self._acquirer is not None:
            try:
                self._acquirer.destroy()
            except Exception as exc:  # noqa: BLE001 - teardown must not raise
                self.logger.warning("Error destroying GenTL image acquirer: %s", exc)
            finally:
                self._acquirer = None
        if self._harvester is not None:
            try:
                self._harvester.reset()
            except Exception as exc:  # noqa: BLE001 - teardown must not raise
                self.logger.warning("Error resetting GenTL harvester: %s", exc)
            finally:
                self._harvester = None
