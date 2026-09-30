"""Tests for the FLIR and OpenCV camera vendors (offline + hardware paths)."""

import struct
import sys
from types import SimpleNamespace

import pytest

from cubos.instruments.camera.exceptions import (
    CameraCaptureError,
    CameraConfigError,
    CameraConnectionError,
)
from cubos.instruments.camera.interface import CameraInstrument
from cubos.instruments.camera.placeholder import write_placeholder_png
from cubos.instruments.camera.vendors.flir import FlirCamera
from cubos.instruments.camera.vendors.opencv import OpenCVCamera
from cubos.instruments.registry import get_instrument_class


def _assert_valid_png(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert width > 0 and height > 0


class TestPlaceholder:
    def test_writes_decodable_png(self, tmp_path):
        target = write_placeholder_png(tmp_path / "sub" / "img.png")
        _assert_valid_png(target)


class TestRegistry:
    def test_vendors_resolve(self):
        assert get_instrument_class("camera", "flir") is FlirCamera
        assert get_instrument_class("camera", "opencv") is OpenCVCamera
        assert issubclass(FlirCamera, CameraInstrument)
        assert issubclass(OpenCVCamera, CameraInstrument)


@pytest.mark.parametrize("vendor_cls", [FlirCamera, OpenCVCamera])
class TestOfflineCapture:
    def test_offline_lifecycle(self, vendor_cls):
        camera = vendor_cls(offline=True)
        camera.connect()
        assert camera.health_check() is True
        camera.disconnect()

    def test_offline_capture_writes_real_file(self, vendor_cls, tmp_path):
        camera = vendor_cls(offline=True)
        camera.connect()
        saved = camera.capture(save_path=str(tmp_path / "shot.png"))
        assert saved == str(tmp_path / "shot.png")
        _assert_valid_png(tmp_path / "shot.png")

    def test_capture_requires_save_path(self, vendor_cls):
        camera = vendor_cls(offline=True)
        with pytest.raises(CameraCaptureError, match="save_path"):
            camera.capture()


class TestHardwareGuards:
    def test_flir_connect_without_pyspin_raises_import_error(self):
        if FlirCamera.is_available():
            pytest.skip("PySpin installed in this environment")
        camera = FlirCamera(offline=False)
        with pytest.raises(ImportError):
            camera.connect()

    def test_capture_before_connect_raises(self):
        camera = FlirCamera(offline=False)
        with pytest.raises(CameraCaptureError, match="not connected"):
            camera.capture(save_path="/tmp/never.png")

    def test_unknown_backend_rejected(self):
        with pytest.raises(CameraConfigError, match="Unknown FLIR backend"):
            FlirCamera(backend="webrtc")

    def test_gentl_backend_offline_lifecycle(self, tmp_path):
        # FLIR/Point Grey is the vendor; pyspin vs gentl is just the SDK
        # binding, so the offline placeholder path must work identically
        # for both.
        camera = FlirCamera(backend="gentl", offline=True)
        camera.connect()
        assert camera.health_check() is True
        saved = camera.capture(save_path=str(tmp_path / "shot.png"))
        _assert_valid_png(tmp_path / "shot.png")
        camera.disconnect()

    def test_gentl_connect_without_harvesters_raises_import_error(self):
        if FlirCamera.is_available(backend="gentl"):
            pytest.skip("harvesters installed in this environment")
        camera = FlirCamera(backend="gentl", offline=False)
        with pytest.raises(ImportError):
            camera.connect()

    def test_gentl_requires_cti_path(self, monkeypatch):
        monkeypatch.delenv("SPINNAKER_GENTL64_CTI", raising=False)
        monkeypatch.setitem(sys.modules, "harvesters", SimpleNamespace())
        monkeypatch.setitem(sys.modules, "harvesters.core", SimpleNamespace(Harvester=lambda: None))
        camera = FlirCamera(backend="gentl", offline=False)
        with pytest.raises(CameraConfigError, match="gentl_cti_path"):
            camera.connect()


# --- Hardware paths via fake SDK modules --------------------------------------
#
# PySpin (proprietary) and cv2 (optional extra) are imported lazily inside the
# vendors, so injecting fakes into sys.modules exercises the full connect/
# capture/teardown paths without either dependency installed.


class FakeSpinnakerException(Exception):
    pass


class FakeImage:
    def __init__(self, incomplete=False):
        self.incomplete = incomplete
        self.released = False

    def IsIncomplete(self):
        return self.incomplete

    def GetImageStatus(self):
        return "IMAGE_NO_DATA"

    def GetNDArray(self):
        return "NDARRAY"

    def Release(self):
        self.released = True


class FakeNodeEntry:
    def GetValue(self):
        return 42


class FakePixelFormatNode:
    def __init__(self):
        self.set_values = []

    def GetEntryByName(self, name):
        return FakeNodeEntry()

    def SetIntValue(self, value):
        self.set_values.append(value)


class FakeSpinCamera:
    def __init__(self, image=None, deinit_raises=False):
        self.image = image or FakeImage()
        self.deinit_raises = deinit_raises
        self.initialized = False
        self.acquiring = False
        self.pixel_format = FakePixelFormatNode()

    def Init(self):
        self.initialized = True

    def DeInit(self):
        if self.deinit_raises:
            raise FakeSpinnakerException("deinit boom")
        self.initialized = False

    def GetNodeMap(self):
        return SimpleNamespace(GetNode=lambda name: self.pixel_format)

    def BeginAcquisition(self):
        self.acquiring = True

    def EndAcquisition(self):
        self.acquiring = False

    def GetNextImage(self, timeout_ms):
        return self.image


class FakeCameraList:
    def __init__(self, cameras):
        self.cameras = cameras
        self.cleared = False

    def GetSize(self):
        return len(self.cameras)

    def __getitem__(self, index):
        return self.cameras[index]

    def Clear(self):
        self.cleared = True


class FakeSystem:
    def __init__(self, camera_list):
        self.camera_list = camera_list
        self.released = False

    def GetCameras(self):
        return self.camera_list

    def ReleaseInstance(self):
        self.released = True


def _fake_pyspin(cameras):
    system = FakeSystem(FakeCameraList(cameras))
    module = SimpleNamespace(
        System=SimpleNamespace(GetInstance=lambda: system),
        SpinnakerException=FakeSpinnakerException,
        CEnumerationPtr=lambda node: node,
        IsAvailable=lambda node: True,
        IsWritable=lambda node: True,
    )
    return module, system


class FakeCv2:
    COLOR_RGB2BGR = 4
    CAP_PROP_FRAME_WIDTH = 3
    CAP_PROP_FRAME_HEIGHT = 4

    def __init__(self, capture_factory=None):
        self.written = {}
        self.capture_factory = capture_factory

    def cvtColor(self, image, code):
        return ("BGR", image)

    def imwrite(self, path, image):
        from pathlib import Path

        Path(path).write_bytes(b"fake-image")
        self.written[path] = image
        return True

    def VideoCapture(self, index):
        return self.capture_factory(index)


class FakeVideoCapture:
    def __init__(self, opened=True, frame="FRAME"):
        self.opened = opened
        self.frame = frame
        self.released = False
        self.props = {}

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        self.props[prop] = value

    def read(self):
        if self.frame is None:
            return False, None
        return True, self.frame

    def release(self):
        self.released = True


class TestFlirHardwarePath:
    def test_missing_pyspin_raises_import_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "PySpin", None)
        camera = FlirCamera(offline=False)
        with pytest.raises(ImportError):
            camera.connect()
        assert FlirCamera.is_available() is False

    def test_connect_capture_disconnect(self, monkeypatch, tmp_path):
        pyspin, system = _fake_pyspin([FakeSpinCamera()])
        monkeypatch.setitem(sys.modules, "PySpin", pyspin)
        monkeypatch.setitem(sys.modules, "cv2", FakeCv2())

        camera = FlirCamera(camera_id=0, offline=False)
        camera.connect()
        assert camera.health_check() is True
        assert FlirCamera.is_available() is True

        saved = camera.capture(save_path=str(tmp_path / "shot.png"))
        assert saved == str(tmp_path / "shot.png")
        assert (tmp_path / "shot.png").read_bytes() == b"fake-image"
        spin_camera = system.camera_list.cameras[0]
        assert spin_camera.pixel_format.set_values == [42]
        assert spin_camera.image.released is True
        assert spin_camera.acquiring is False

        camera.disconnect()
        assert camera.health_check() is False
        assert system.released is True
        assert system.camera_list.cleared is True

    def test_connect_no_cameras(self, monkeypatch):
        pyspin, _ = _fake_pyspin([])
        monkeypatch.setitem(sys.modules, "PySpin", pyspin)
        camera = FlirCamera(offline=False)
        with pytest.raises(CameraConnectionError, match="No FLIR cameras"):
            camera.connect()

    def test_connect_out_of_range_id_falls_back_to_first(self, monkeypatch):
        pyspin, system = _fake_pyspin([FakeSpinCamera()])
        monkeypatch.setitem(sys.modules, "PySpin", pyspin)
        camera = FlirCamera(camera_id=7, offline=False)
        camera.connect()
        assert system.camera_list.cameras[0].initialized is True

    def test_connect_wraps_spinnaker_exception(self, monkeypatch):
        pyspin, system = _fake_pyspin([FakeSpinCamera()])

        def exploding_get_cameras():
            raise FakeSpinnakerException("usb gone")

        system.GetCameras = exploding_get_cameras
        monkeypatch.setitem(sys.modules, "PySpin", pyspin)
        camera = FlirCamera(offline=False)
        with pytest.raises(CameraConnectionError, match="usb gone"):
            camera.connect()

    def test_capture_incomplete_image(self, monkeypatch, tmp_path):
        spin_camera = FakeSpinCamera(image=FakeImage(incomplete=True))
        pyspin, _ = _fake_pyspin([spin_camera])
        monkeypatch.setitem(sys.modules, "PySpin", pyspin)
        monkeypatch.setitem(sys.modules, "cv2", FakeCv2())
        camera = FlirCamera(offline=False)
        camera.connect()
        with pytest.raises(CameraCaptureError, match="incomplete"):
            camera.capture(save_path=str(tmp_path / "x.png"))
        assert spin_camera.image.released is True
        assert spin_camera.acquiring is False

    def test_capture_spinnaker_error(self, monkeypatch, tmp_path):
        spin_camera = FakeSpinCamera()

        def exploding_next_image(timeout_ms):
            raise FakeSpinnakerException("grab failed")

        spin_camera.GetNextImage = exploding_next_image
        pyspin, _ = _fake_pyspin([spin_camera])
        monkeypatch.setitem(sys.modules, "PySpin", pyspin)
        monkeypatch.setitem(sys.modules, "cv2", FakeCv2())
        camera = FlirCamera(offline=False)
        camera.connect()
        with pytest.raises(CameraCaptureError, match="grab failed"):
            camera.capture(save_path=str(tmp_path / "x.png"))

    def test_disconnect_survives_deinit_error(self, monkeypatch):
        pyspin, system = _fake_pyspin([FakeSpinCamera(deinit_raises=True)])
        monkeypatch.setitem(sys.modules, "PySpin", pyspin)
        camera = FlirCamera(offline=False)
        camera.connect()
        camera.disconnect()
        assert system.released is True

    def test_missing_cv2_raises_import_error(self, monkeypatch, tmp_path):
        pyspin, _ = _fake_pyspin([FakeSpinCamera()])
        monkeypatch.setitem(sys.modules, "PySpin", pyspin)
        monkeypatch.setitem(sys.modules, "cv2", None)
        camera = FlirCamera(offline=False)
        camera.connect()
        with pytest.raises(ImportError):
            camera.capture(save_path=str(tmp_path / "x.png"))


class FakePixelFormatValue:
    def __init__(self):
        self.value = None


class FakeGentlNodeMap:
    def __init__(self):
        self.PixelFormat = FakePixelFormatValue()


class FakeGentlRemoteDevice:
    def __init__(self):
        self.node_map = FakeGentlNodeMap()


class FakeGentlComponent:
    def __init__(self, height=2, width=3, data="RAW_DATA"):
        self.height = height
        self.width = width
        self.data = FakeGentlArray(data)


class FakeGentlArray:
    """Stands in for the numpy array ``component.data`` really is."""

    def __init__(self, marker):
        self.marker = marker

    def reshape(self, height, width, channels):
        return FakeGentlArray((self.marker, "reshaped", height, width, channels))

    def copy(self):
        return (self.marker, "copied")


class FakeGentlPayload:
    def __init__(self, components):
        self.components = components


class FakeGentlBuffer:
    def __init__(self, components):
        self.payload = FakeGentlPayload(components)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeImageAcquirer:
    def __init__(self, buffer=None, fetch_raises=None):
        self.remote_device = FakeGentlRemoteDevice()
        self.buffer = buffer or FakeGentlBuffer([FakeGentlComponent()])
        self.fetch_raises = fetch_raises
        self.started = False
        self.destroyed = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def fetch(self, timeout=None):
        if self.fetch_raises:
            raise self.fetch_raises
        return self.buffer

    def destroy(self):
        self.destroyed = True


class FakeHarvester:
    def __init__(self, device_info_list=None, acquirer=None):
        self.device_info_list = device_info_list if device_info_list is not None else ["cam0"]
        self.acquirer = acquirer or FakeImageAcquirer()
        self.files_added = []
        self.reset_called = False

    def add_file(self, path):
        self.files_added.append(path)

    def update(self):
        pass

    def create(self, index):
        return self.acquirer

    def reset(self):
        self.reset_called = True


def _install_fake_harvesters(monkeypatch, harvester):
    """Stub both `harvesters` and `harvesters.core` — `import harvesters.core`
    needs the parent package resolvable too, not just the submodule."""
    core = SimpleNamespace(Harvester=lambda: harvester)
    monkeypatch.setitem(sys.modules, "harvesters", SimpleNamespace(core=core))
    monkeypatch.setitem(sys.modules, "harvesters.core", core)


class TestFlirGentlHardwarePath:
    def test_missing_harvesters_raises_import_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "harvesters", None)
        monkeypatch.setitem(sys.modules, "harvesters.core", None)
        camera = FlirCamera(backend="gentl", offline=False)
        with pytest.raises(ImportError):
            camera.connect()
        assert FlirCamera.is_available(backend="gentl") is False

    def test_connect_capture_disconnect(self, monkeypatch, tmp_path):
        acquirer = FakeImageAcquirer()
        harvester = FakeHarvester(acquirer=acquirer)
        _install_fake_harvesters(monkeypatch, harvester)
        monkeypatch.setitem(sys.modules, "cv2", FakeCv2())

        camera = FlirCamera(
            camera_id=0, backend="gentl", gentl_cti_path="/opt/spinnaker/Spinnaker_GenTL.cti",
            offline=False,
        )
        camera.connect()
        assert camera.health_check() is True
        assert FlirCamera.is_available(backend="gentl") is True
        assert acquirer.remote_device.node_map.PixelFormat.value == "RGB8"
        assert harvester.files_added == ["/opt/spinnaker/Spinnaker_GenTL.cti"]

        saved = camera.capture(save_path=str(tmp_path / "shot.png"))
        assert saved == str(tmp_path / "shot.png")
        assert (tmp_path / "shot.png").read_bytes() == b"fake-image"
        assert acquirer.started is False  # stop() ran after fetch

        camera.disconnect()
        assert camera.health_check() is False
        assert acquirer.destroyed is True
        assert harvester.reset_called is True

    def test_connect_no_cameras(self, monkeypatch):
        harvester = FakeHarvester(device_info_list=[])
        _install_fake_harvesters(monkeypatch, harvester)
        camera = FlirCamera(
            backend="gentl", gentl_cti_path="/opt/spinnaker/Spinnaker_GenTL.cti", offline=False,
        )
        with pytest.raises(CameraConnectionError, match="No FLIR cameras"):
            camera.connect()
        assert harvester.reset_called is True

    def test_connect_out_of_range_id_falls_back_to_first(self, monkeypatch):
        acquirer = FakeImageAcquirer()
        harvester = FakeHarvester(device_info_list=["cam0"], acquirer=acquirer)
        _install_fake_harvesters(monkeypatch, harvester)
        camera = FlirCamera(
            camera_id=7, backend="gentl", gentl_cti_path="/opt/spinnaker/Spinnaker_GenTL.cti",
            offline=False,
        )
        camera.connect()
        assert camera.health_check() is True

    def test_connect_uses_env_var_when_no_explicit_path(self, monkeypatch):
        monkeypatch.setenv("SPINNAKER_GENTL64_CTI", "/env/Spinnaker_GenTL.cti")
        harvester = FakeHarvester()
        _install_fake_harvesters(monkeypatch, harvester)
        camera = FlirCamera(backend="gentl", offline=False)
        camera.connect()
        assert harvester.files_added == ["/env/Spinnaker_GenTL.cti"]

    def test_capture_fetch_error(self, monkeypatch, tmp_path):
        acquirer = FakeImageAcquirer(fetch_raises=RuntimeError("usb gone"))
        harvester = FakeHarvester(acquirer=acquirer)
        _install_fake_harvesters(monkeypatch, harvester)
        monkeypatch.setitem(sys.modules, "cv2", FakeCv2())
        camera = FlirCamera(
            backend="gentl", gentl_cti_path="/opt/spinnaker/Spinnaker_GenTL.cti", offline=False,
        )
        camera.connect()
        with pytest.raises(CameraCaptureError, match="usb gone"):
            camera.capture(save_path=str(tmp_path / "x.png"))
        assert acquirer.started is False  # stop() still ran

    def test_disconnect_survives_teardown_errors(self, monkeypatch):
        class ExplodingAcquirer(FakeImageAcquirer):
            def destroy(self):
                raise RuntimeError("destroy boom")

        harvester = FakeHarvester(acquirer=ExplodingAcquirer())
        _install_fake_harvesters(monkeypatch, harvester)
        camera = FlirCamera(
            backend="gentl", gentl_cti_path="/opt/spinnaker/Spinnaker_GenTL.cti", offline=False,
        )
        camera.connect()
        camera.disconnect()  # must not raise
        assert harvester.reset_called is True


class TestOpenCVHardwarePath:
    def test_missing_cv2_raises_import_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "cv2", None)
        camera = OpenCVCamera(camera_id=0, offline=False)
        with pytest.raises(ImportError):
            camera.connect()

    def test_connect_capture_disconnect(self, monkeypatch, tmp_path):
        capture = FakeVideoCapture()
        cv2 = FakeCv2(capture_factory=lambda index: capture)
        monkeypatch.setitem(sys.modules, "cv2", cv2)
        camera = OpenCVCamera(camera_id=2, offline=False)
        camera.connect()
        assert camera.health_check() is True
        assert capture.props == {FakeCv2.CAP_PROP_FRAME_WIDTH: 1280,
                                 FakeCv2.CAP_PROP_FRAME_HEIGHT: 720}
        saved = camera.capture(save_path=str(tmp_path / "web.png"))
        assert (tmp_path / "web.png").read_bytes() == b"fake-image"
        assert saved == str(tmp_path / "web.png")
        camera.disconnect()
        assert capture.released is True
        assert camera.health_check() is False

    def test_auto_detect_scans_indexes(self, monkeypatch):
        captures = {0: FakeVideoCapture(opened=False), 1: FakeVideoCapture()}
        cv2 = FakeCv2(capture_factory=lambda index: captures.setdefault(
            index, FakeVideoCapture()))
        monkeypatch.setitem(sys.modules, "cv2", cv2)
        camera = OpenCVCamera(offline=False)  # camera_id -1 -> auto-detect
        camera.connect()
        assert camera.camera_id == 1

    def test_auto_detect_none_found(self, monkeypatch):
        cv2 = FakeCv2(capture_factory=lambda index: FakeVideoCapture(opened=False))
        monkeypatch.setitem(sys.modules, "cv2", cv2)
        camera = OpenCVCamera(offline=False)
        with pytest.raises(CameraConnectionError, match="No responsive webcam"):
            camera.connect()

    def test_connect_failure_on_explicit_index(self, monkeypatch):
        cv2 = FakeCv2(capture_factory=lambda index: FakeVideoCapture(opened=False))
        monkeypatch.setitem(sys.modules, "cv2", cv2)
        camera = OpenCVCamera(camera_id=3, offline=False)
        with pytest.raises(CameraConnectionError, match="index 3"):
            camera.connect()

    def test_read_failure_raises(self, monkeypatch, tmp_path):
        capture = FakeVideoCapture(frame=None)
        cv2 = FakeCv2(capture_factory=lambda index: capture)
        monkeypatch.setitem(sys.modules, "cv2", cv2)
        camera = OpenCVCamera(camera_id=0, offline=False)
        camera.connect()
        with pytest.raises(CameraCaptureError, match="Failed to capture"):
            camera.capture(save_path=str(tmp_path / "x.png"))
