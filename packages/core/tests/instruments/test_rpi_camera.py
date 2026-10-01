import pytest

from cubos.instruments.camera.vendors.mount_only import MountOnlyCamera
from cubos.instruments.camera.vendors.raspberry_pi import RaspberryPiCamera
from cubos.instruments.mounted_tool.vendors.mount_only import MountOnlyTool


def test_rpi_camera_capture_is_not_implemented():
    camera = RaspberryPiCamera(offline=True)

    with pytest.raises(NotImplementedError, match="capture is not implemented"):
        camera.capture()


@pytest.mark.parametrize("offline", [True, False])
def test_rpi_camera_connect_disconnect_are_base_class_noops(offline):
    camera = RaspberryPiCamera(offline=offline)

    camera.connect()
    camera.disconnect()

    assert camera.health_check() is True


def test_mount_only_camera_models_non_contact_mount_without_capture():
    camera = MountOnlyCamera(offset_x=1.0, offset_y=2.0, depth=3.0)

    camera.connect()
    assert camera.health_check() is True
    camera.disconnect()
    with pytest.raises(NotImplementedError, match="does not implement image capture"):
        camera.capture()


def test_mount_only_tool_models_contact_mount_without_actuation():
    tool = MountOnlyTool(offset_x=1.0, offset_y=2.0, depth=3.0, offline=True)

    tool.connect()
    assert tool.health_check() is True
    tool.disconnect()
    with pytest.raises(NotImplementedError, match="does not implement actuation"):
        tool.actuate(force=1.0)
