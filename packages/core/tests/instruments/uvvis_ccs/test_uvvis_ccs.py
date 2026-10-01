import pytest
from unittest.mock import patch, MagicMock

from cubos.instruments.base_instrument import InstrumentError
from cubos.instruments.uvvis_ccs.models import UVVisSpectrum, NUM_PIXELS
from cubos.instruments.uvvis_ccs.exceptions import (
    UVVisCCSError,
    UVVisCCSConnectionError,
    UVVisCCSMeasurementError,
    UVVisCCSTimeoutError,
)
from cubos.instruments.uvvis_ccs.vendors.thorlabs import ThorlabsUVVisCCS, _synthetic_spectrum


# --- UVVisSpectrum model tests ------------------------------------------------


class TestUVVisSpectrum:


    def test_invalid_empty_wavelengths(self):
        spec = UVVisSpectrum(
            wavelengths=(),
            intensities=(),
            integration_time_s=0.24,
        )
        assert spec.is_valid is False

    def test_invalid_mismatched_lengths(self):
        spec = UVVisSpectrum(
            wavelengths=(400.0, 500.0),
            intensities=(0.1,),
            integration_time_s=0.24,
        )
        assert spec.is_valid is False

    def test_invalid_zero_integration_time(self):
        spec = UVVisSpectrum(
            wavelengths=(400.0,),
            intensities=(0.1,),
            integration_time_s=0.0,
        )
        assert spec.is_valid is False

    def test_invalid_negative_integration_time(self):
        spec = UVVisSpectrum(
            wavelengths=(400.0,),
            intensities=(0.1,),
            integration_time_s=-1.0,
        )
        assert spec.is_valid is False


# --- Exception hierarchy tests ------------------------------------------------


class TestExceptions:

    def test_base_error_is_instrument_error(self):
        assert issubclass(UVVisCCSError, InstrumentError)

    def test_connection_error_hierarchy(self):
        assert issubclass(UVVisCCSConnectionError, UVVisCCSError)
        err = UVVisCCSConnectionError("dll not found")
        assert isinstance(err, InstrumentError)

    def test_measurement_error_hierarchy(self):
        assert issubclass(UVVisCCSMeasurementError, UVVisCCSError)

    def test_timeout_error_hierarchy(self):
        assert issubclass(UVVisCCSTimeoutError, UVVisCCSError)


# --- Synthetic spectrum helper tests ------------------------------------------


class TestSyntheticSpectrum:

    def test_default_synthetic_spectrum(self):
        spec = _synthetic_spectrum()
        assert spec.is_valid is True
        assert spec.num_pixels == NUM_PIXELS
        assert spec.wavelengths[0] == pytest.approx(200.0)
        assert spec.wavelengths[-1] == pytest.approx(800.0)
        assert all(v == 0.5 for v in spec.intensities)


# --- Driver tests (mocked ctypes DLL) ----------------------------------------


class TestUVVisCCSDriver:

    def _make_mock_dll(self):
        """Build a mock DLL that simulates the Thorlabs TLCCS API."""
        dll = MagicMock()

        # tlccs_init succeeds (rc=0) and sets handle
        def fake_init(resource, id_query, reset, handle_ptr):
            handle_ptr._obj.value = 42
            return 0
        dll.tlccs_init.side_effect = fake_init

        # tlccs_getWavelengthData fills array with linear 200-800nm
        def fake_get_wavelength_data(handle, data_set, data, wmin, wmax):
            step = 600.0 / (NUM_PIXELS - 1)
            for i in range(NUM_PIXELS):
                data[i] = 200.0 + i * step
            return 0
        dll.tlccs_getWavelengthData.side_effect = fake_get_wavelength_data

        # Integration time
        dll.tlccs_setIntegrationTime.return_value = 0
        def fake_get_int_time(handle, t_ptr):
            t_ptr._obj.value = 0.24
            return 0
        dll.tlccs_getIntegrationTime.side_effect = fake_get_int_time

        # Status: idle and scan-ready
        def fake_get_status(handle, status_ptr):
            status_ptr._obj.value = 0x0002 | 0x0010  # idle + scan_ready
            return 0
        dll.tlccs_getDeviceStatus.side_effect = fake_get_status

        # Scan
        dll.tlccs_startScan.return_value = 0

        # getScanData fills with constant intensities
        def fake_get_scan_data(handle, data):
            for i in range(NUM_PIXELS):
                data[i] = 0.42
            return 0
        dll.tlccs_getScanData.side_effect = fake_get_scan_data

        dll.tlccs_close.return_value = 0

        return dll


    @patch("ctypes.cdll")
    def test_connect_raises_on_missing_dll(self, mock_cdll):
        mock_cdll.LoadLibrary.side_effect = OSError("not found")

        ccs = ThorlabsUVVisCCS(serial_number="TEST123", dll_path="missing.dll")
        with pytest.raises(UVVisCCSConnectionError, match="Failed to load DLL"):
            ccs.connect()

    @patch("ctypes.cdll")
    def test_connect_raises_on_init_failure(self, mock_cdll):
        dll = self._make_mock_dll()
        dll.tlccs_init.side_effect = lambda *args: -1  # nonzero = error
        mock_cdll.LoadLibrary.return_value = dll

        ccs = ThorlabsUVVisCCS(serial_number="BAD_SERIAL", dll_path="fake.dll")
        with pytest.raises(UVVisCCSConnectionError, match="tlccs_init failed"):
            ccs.connect()

    @patch("ctypes.cdll")
    def test_disconnect_calls_close(self, mock_cdll):
        dll = self._make_mock_dll()
        mock_cdll.LoadLibrary.return_value = dll

        ccs = ThorlabsUVVisCCS(serial_number="TEST123", dll_path="fake.dll")
        ccs.connect()
        ccs.disconnect()

        dll.tlccs_close.assert_called_once()
        assert ccs._handle is None

    @patch("ctypes.cdll")
    def test_disconnect_safe_when_not_connected(self, mock_cdll):
        ccs = ThorlabsUVVisCCS(serial_number="TEST123", dll_path="fake.dll")
        ccs.disconnect()  # should not raise


    def test_health_check_false_when_not_connected(self):
        ccs = ThorlabsUVVisCCS(serial_number="TEST123", dll_path="fake.dll")
        assert ccs.health_check() is False

    @patch("ctypes.cdll")
    def test_measure_returns_spectrum(self, mock_cdll):
        dll = self._make_mock_dll()
        mock_cdll.LoadLibrary.return_value = dll

        ccs = ThorlabsUVVisCCS(serial_number="TEST123", dll_path="fake.dll")
        ccs.connect()
        result = ccs.measure()

        assert isinstance(result, UVVisSpectrum)
        assert result.is_valid is True
        assert result.num_pixels == NUM_PIXELS
        assert result.integration_time_s == pytest.approx(0.24)
        assert result.wavelengths[0] == pytest.approx(200.0)
        assert all(v == pytest.approx(0.42) for v in result.intensities)

    @patch("ctypes.cdll")
    def test_measure_raises_on_scan_data_failure(self, mock_cdll):
        dll = self._make_mock_dll()
        dll.tlccs_getScanData.side_effect = lambda h, d: -1
        mock_cdll.LoadLibrary.return_value = dll

        ccs = ThorlabsUVVisCCS(serial_number="TEST123", dll_path="fake.dll")
        ccs.connect()

        with pytest.raises(UVVisCCSMeasurementError, match="getScanData failed"):
            ccs.measure()

    @patch("ctypes.cdll")
    def test_measure_raises_typed_error_on_start_scan_failure(self, mock_cdll):
        dll = self._make_mock_dll()
        dll.tlccs_startScan.return_value = -7
        mock_cdll.LoadLibrary.return_value = dll

        ccs = ThorlabsUVVisCCS(serial_number="TEST123", dll_path="fake.dll")
        ccs.connect()

        with pytest.raises(UVVisCCSError, match="tlccs_startScan failed"):
            ccs.measure()

    def test_measure_before_connect_raises_typed_error(self):
        ccs = ThorlabsUVVisCCS(serial_number="TEST123", dll_path="fake.dll")

        with pytest.raises(UVVisCCSError, match="not connected"):
            ccs.measure()

    def test_set_integration_time_before_connect_raises_typed_error(self):
        ccs = ThorlabsUVVisCCS(serial_number="TEST123", dll_path="fake.dll")

        with pytest.raises(UVVisCCSError, match="not connected"):
            ccs.set_integration_time(0.5)


# --- Offline ThorlabsUVVisCCS tests ---------------------------------------------------


class TestOfflineUVVisCCS:


    def test_connect_disconnect_cycle(self):
        ccs = ThorlabsUVVisCCS(offline=True)
        ccs.connect()
        assert ccs.health_check() is True
        ccs.disconnect()  # safe no-op in offline mode


    def test_set_integration_time_updates_state(self):
        ccs = ThorlabsUVVisCCS(offline=True)
        ccs.connect()
        ccs.set_integration_time(1.5)
        assert ccs.get_integration_time() == 1.5

    def test_disconnect_safe_when_not_connected(self):
        ccs = ThorlabsUVVisCCS(offline=True)
        ccs.disconnect()  # should not raise
