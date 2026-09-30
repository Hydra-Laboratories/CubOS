import textwrap

import pytest

import cubos.instruments.registry as registry_module
from cubos.instruments.asmi.models import ASMIStatus, MeasurementResult
from cubos.instruments.asmi.interface import ASMIInstrument
from cubos.instruments.base_instrument import BaseInstrument
from cubos.instruments.pipette.models import AspirateResult, PipetteStatus
from cubos.instruments.pipette.interface import PipetteInstrument
from cubos.instruments.registry import (
    FieldSpec,
    config_fields,
    get_calibration_mode,
    get_instrument_class,
    get_instrument_interface,
    get_supported_types,
    get_supported_vendors,
    list_measurement_method_params,
    list_measurement_methods,
    load_registry,
    validate_instrument,
)
from cubos.gantry.instrument_loader import build_instrumented_gantry


EXPECTED_TYPES = [
    "asmi",
    "camera",
    "capper",
    "filmetrics",
    "lighting",
    "mounted_tool",
    "pipette",
    "potentiostat",
    "uv_curing",
    "uvvis_ccs",
]


class FakeCustomerASMI(ASMIInstrument):
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def health_check(self) -> bool:
        return True

    def measure(self, n_samples: int = 1) -> MeasurementResult:
        return MeasurementResult(readings=(), mean_n=0.0, std_n=0.0, timestamp=0.0)

    def get_status(self) -> ASMIStatus:
        return ASMIStatus(is_connected=True, sensor_description="fake")

    def get_force_reading(self) -> float:
        return 0.0

    def get_baseline_force(self, samples: int = 10) -> tuple[float, float]:
        return (0.0, 0.0)

    def indentation(self, gantry, **kwargs) -> dict:
        return {}


class FakeCustomerPipette(PipetteInstrument):
    def __init__(
        self,
        customer_gain: float = 1.0,
        name: str | None = None,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
        depth: float = 0.0,
        offline: bool = False,
    ):
        super().__init__(
            name=name,
            offset_x=offset_x,
            offset_y=offset_y,
            depth=depth,
            offline=offline,
        )
        self.customer_gain = customer_gain

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def health_check(self) -> bool:
        return True

    @property
    def attached_tip_extension(self) -> float:
        return 0.0

    def set_attached_tip_extension(self, extension_mm: float) -> None:
        pass

    def clear_attached_tip_extension(self) -> None:
        pass

    def home(self) -> None:
        pass

    def prime(self, speed: float = 50.0) -> None:
        pass

    def aspirate(self, volume_ul: float, speed: float = 50.0) -> AspirateResult:
        return AspirateResult(success=True, volume_ul=volume_ul, position_mm=0.0)

    def dispense(self, volume_ul: float, speed: float = 50.0) -> AspirateResult:
        return AspirateResult(success=True, volume_ul=volume_ul, position_mm=0.0)

    def blowout(self, speed: float = 50.0) -> None:
        pass

    def pick_up_tip(self, speed: float = 50.0) -> None:
        pass

    def drop_tip(self, speed: float = 50.0) -> None:
        pass

    def get_status(self) -> PipetteStatus:
        return PipetteStatus(
            is_homed=True,
            position_mm=0.0,
            max_volume=300.0,
            has_tip=False,
            is_primed=True,
        )


class IncompleteCustomerASMI(ASMIInstrument):
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def health_check(self) -> bool:
        return True


class _EntryPointGroup:
    def __init__(self, entries):
        self._entries = entries

    def select(self, *, group: str):
        if group == "cubos.instrument_registries":
            return self._entries
        return []


class _EntryPoint:
    name = "customer_registry"

    def __init__(self, registry):
        self._registry = registry

    def load(self):
        return self._registry


@pytest.fixture(autouse=True)
def _reset_registry(monkeypatch):
    monkeypatch.delenv("CUBOS_INSTRUMENT_REGISTRY_PATHS", raising=False)
    monkeypatch.setattr(
        registry_module.importlib_metadata,
        "entry_points",
        lambda: _EntryPointGroup([]),
    )
    registry_module._cache = None
    yield
    registry_module._cache = None


class TestLoadRegistry:

    def test_returns_all_instrument_types(self):
        registry = load_registry()
        assert sorted(registry["instruments"].keys()) == EXPECTED_TYPES

    def test_each_entry_has_interface_calibration_mode_and_vendors(self):
        registry = load_registry()
        for type_key, entry in registry["instruments"].items():
            assert "interface" in entry, f"{type_key} missing interface"
            assert entry.get("calibration_mode") in {"contact", "non_contact", "follow_camera"}
            assert "vendors" in entry, f"{type_key} missing vendors"
            assert len(entry["vendors"]) > 0, f"{type_key} has empty vendors"
            for vendor_key, vendor in entry["vendors"].items():
                assert "module" in vendor, f"{type_key}/{vendor_key} missing module"
                assert "class_name" in vendor, (
                    f"{type_key}/{vendor_key} missing class_name"
                )

    def test_overlay_adds_vendor_for_existing_type(self, tmp_path, monkeypatch):
        overlay = tmp_path / "instrument_registry.yaml"
        overlay.write_text(
            textwrap.dedent(
                """
                instruments:
                  asmi:
                    vendors:
                      customer_xyz:
                        module: tests.instruments.test_registry
                        class_name: FakeCustomerASMI
                """
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CUBOS_INSTRUMENT_REGISTRY_PATHS", str(overlay))
        registry_module._cache = None

        assert "customer_xyz" in get_supported_vendors("asmi")
        assert get_instrument_class("asmi", "customer_xyz") is FakeCustomerASMI

    def test_overlay_duplicate_yaml_key_names_file_and_key(self, tmp_path, monkeypatch):
        overlay = tmp_path / "instrument_registry.yaml"
        overlay.write_text(
            textwrap.dedent(
                """
                instruments:
                  asmi:
                    vendors:
                      customer_xyz:
                        module: tests.instruments.test_registry
                        class_name: FakeCustomerASMI
                  asmi:
                    vendors:
                      customer_abc:
                        module: tests.instruments.test_registry
                        class_name: FakeCustomerASMI
                """
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CUBOS_INSTRUMENT_REGISTRY_PATHS", str(overlay))
        registry_module._cache = None

        with pytest.raises(Exception) as exc_info:
            load_registry()

        message = str(exc_info.value)
        assert str(overlay) in message
        assert "duplicate YAML key 'asmi'" in message

    def test_overlay_vendor_must_implement_interface(self, tmp_path, monkeypatch):
        overlay = tmp_path / "instrument_registry.yaml"
        overlay.write_text(
            textwrap.dedent(
                """
                instruments:
                  asmi:
                    vendors:
                      incomplete:
                        module: tests.instruments.test_registry
                        class_name: IncompleteCustomerASMI
                """
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CUBOS_INSTRUMENT_REGISTRY_PATHS", str(overlay))
        registry_module._cache = None

        with pytest.raises(TypeError, match="missing required interface methods"):
            get_instrument_class("asmi", "incomplete")

    def test_entry_point_adds_vendor(self, monkeypatch):
        monkeypatch.setattr(
            registry_module.importlib_metadata,
            "entry_points",
            lambda: _EntryPointGroup([
                _EntryPoint({
                    "instruments": {
                        "pipette": {
                            "vendors": {
                                "customer_xyz": {
                                    "module": "tests.instruments.test_registry",
                                    "class_name": "FakeCustomerPipette",
                                }
                            }
                        }
                    }
                })
            ]),
        )
        registry_module._cache = None

        assert "customer_xyz" in get_supported_vendors("pipette")
        assert get_instrument_class("pipette", "customer_xyz") is FakeCustomerPipette

    def test_unknown_driver_yaml_key_names_key_and_suggestion(self):
        with pytest.raises(ValueError) as exc_info:
            build_instrumented_gantry(
                {
                    "force_probe": {
                        "type": "asmi",
                        "vendor": "vernier",
                        "dept": 58.0,
                    }
                },
                gantry=object(),
            )

        message = str(exc_info.value)
        assert "force_probe" in message
        assert "asmi/vernier" in message
        assert "dept" in message
        assert "depth" in message

    def test_known_driver_yaml_kwargs_still_pass(self):
        board = build_instrumented_gantry(
            {
                "force_probe": {
                    "type": "asmi",
                    "vendor": "vernier",
                    "depth": 58.0,
                    "sensor_channels": [1],
                    "offline": True,
                }
            },
            gantry=object(),
        )

        instrument = board.instruments["force_probe"]
        assert instrument.depth == 58.0
        assert instrument._sensor_channels == [1]

    def test_external_registry_driver_signature_kwargs_pass(self, monkeypatch):
        monkeypatch.setattr(
            registry_module.importlib_metadata,
            "entry_points",
            lambda: _EntryPointGroup([
                _EntryPoint({
                    "instruments": {
                        "pipette": {
                            "vendors": {
                                "customer_xyz": {
                                    "module": "tests.instruments.test_registry",
                                    "class_name": "FakeCustomerPipette",
                                }
                            }
                        }
                    }
                })
            ]),
        )
        registry_module._cache = None

        board = build_instrumented_gantry(
            {
                "pipette": {
                    "type": "pipette",
                    "vendor": "customer_xyz",
                    "customer_gain": 2.5,
                    "offline": True,
                }
            },
            gantry=object(),
        )

        assert board.instruments["pipette"].customer_gain == 2.5

    def test_unknown_vendor_error_lists_pair_tried_and_available_pairs(self):
        with pytest.raises(ValueError) as exc_info:
            validate_instrument("asmi", "missing_vendor")

        message = str(exc_info.value)
        assert "asmi/missing_vendor" in message
        assert "asmi/vernier" in message

    def test_duplicate_vendor_without_override_raises(self, tmp_path, monkeypatch):
        overlay = tmp_path / "instrument_registry.yaml"
        overlay.write_text(
            textwrap.dedent(
                """
                instruments:
                  asmi:
                    vendors:
                      vernier:
                        module: tests.instruments.test_registry
                        class_name: FakeCustomerASMI
                """
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CUBOS_INSTRUMENT_REGISTRY_PATHS", str(overlay))
        registry_module._cache = None

        with pytest.raises(ValueError, match="already registered"):
            load_registry()

    def test_duplicate_vendor_with_override_replaces(self, tmp_path, monkeypatch):
        overlay = tmp_path / "instrument_registry.yaml"
        overlay.write_text(
            textwrap.dedent(
                """
                instruments:
                  asmi:
                    vendors:
                      vernier:
                        override: true
                        module: tests.instruments.test_registry
                        class_name: FakeCustomerASMI
                """
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CUBOS_INSTRUMENT_REGISTRY_PATHS", str(overlay))
        registry_module._cache = None

        assert get_instrument_class("asmi", "vernier") is FakeCustomerASMI


class TestGetSupportedTypes:

    def test_returns_sorted_list(self):
        assert get_supported_types() == EXPECTED_TYPES


class TestGetSupportedVendors:

    def test_asmi_vendors(self):
        assert get_supported_vendors("asmi") == ["vernier"]

    def test_camera_vendors(self):
        assert get_supported_vendors("camera") == ["flir", "mount_only", "opencv", "raspberry_pi"]

    def test_filmetrics_vendors(self):
        assert get_supported_vendors("filmetrics") == ["kla"]

    def test_lighting_vendors(self):
        assert get_supported_vendors("lighting") == ["pawduino"]

    def test_mounted_tool_vendors(self):
        assert get_supported_vendors("mounted_tool") == ["mount_only"]

    def test_pipette_vendors(self):
        assert get_supported_vendors("pipette") == ["opentrons", "sartorius"]

    def test_potentiostat_vendors(self):
        assert get_supported_vendors("potentiostat") == ["admiral", "emstat"]

    def test_uv_curing_vendors(self):
        assert get_supported_vendors("uv_curing") == ["excelitas"]

    def test_uvvis_ccs_vendors(self):
        assert get_supported_vendors("uvvis_ccs") == ["thorlabs"]

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown instrument type"):
            get_supported_vendors("nonexistent")


class TestGetCalibrationMode:
    def test_camera_is_non_contact(self):
        assert get_calibration_mode("camera") == "non_contact"

    def test_lighting_follows_camera(self):
        assert get_calibration_mode("lighting") == "follow_camera"

    def test_contact_is_default_for_regular_instruments(self):
        assert get_calibration_mode("asmi") == "contact"


class TestConfigFields:

    def test_returns_driver_signature_fields_with_choices(self):
        fields = config_fields("pipette", "opentrons")

        assert FieldSpec(
            name="pipette_model",
            type="str",
            required=False,
            default="p300_single_gen2",
            choices=(
                "flex_1channel_1000",
                "flex_1channel_50",
                "flex_8channel_1000",
                "flex_8channel_50",
                "flex_96channel_1000",
                "p1000_single_gen2",
                "p20_multi_gen2",
                "p20_single_gen2",
                "p300_multi_gen2",
                "p300_single_gen2",
            ),
        ) in fields
        assert FieldSpec(
            name="baud_rate",
            type="int",
            required=False,
            default=115200,
            choices=None,
        ) in fields
        assert "offset_x" not in {field.name for field in fields}

    def test_config_fields_unknown_vendor_raises_clear_error(self):
        with pytest.raises(ValueError, match="asmi/missing_vendor"):
            config_fields("asmi", "missing_vendor")

    def test_external_driver_signature_fields_are_reflected(self, monkeypatch):
        monkeypatch.setattr(
            registry_module.importlib_metadata,
            "entry_points",
            lambda: _EntryPointGroup([
                _EntryPoint({
                    "instruments": {
                        "pipette": {
                            "vendors": {
                                "customer_xyz": {
                                    "module": "tests.instruments.test_registry",
                                    "class_name": "FakeCustomerPipette",
                                }
                            }
                        }
                    }
                })
            ]),
        )
        registry_module._cache = None

        fields = config_fields("pipette", "customer_xyz")

        assert FieldSpec(
            name="customer_gain",
            type="float",
            required=False,
            default=1.0,
            choices=None,
        ) in fields


class TestListMeasurementMethods:

    def test_returns_methods_using_protocol_measurement_types(self):
        assert list_measurement_methods("asmi") == ["indentation"]
        assert list_measurement_methods("filmetrics") == ["measure"]
        assert list_measurement_methods("uv_curing") == ["measure", "cure"]
        assert list_measurement_methods("uvvis_ccs") == ["measure"]
        assert list_measurement_methods("potentiostat") == [
            "run_CA",
            "run_CP",
            "run_CV",
            "run_OCP",
        ]

    def test_list_measurement_methods_can_filter_vendor(self):
        assert list_measurement_methods("pipette", vendor="opentrons") == []

    def test_list_measurement_methods_unknown_type_raises_clear_error(self):
        with pytest.raises(ValueError, match="Unknown instrument type"):
            list_measurement_methods("nonexistent")


class TestGetInstrumentClass:

    def test_returns_subclass_of_base_instrument(self):
        valid_pairs = [
            ("asmi", "vernier"),
            ("camera", "mount_only"),
            ("camera", "raspberry_pi"),
            ("filmetrics", "kla"),
            ("mounted_tool", "mount_only"),
            ("pipette", "opentrons"),
            ("potentiostat", "admiral"),
            ("potentiostat", "emstat"),
            ("uv_curing", "excelitas"),
            ("uvvis_ccs", "thorlabs"),
        ]
        for type_key, vendor in valid_pairs:
            cls = get_instrument_class(type_key, vendor)
            assert issubclass(cls, BaseInstrument)
            assert issubclass(cls, get_instrument_interface(type_key))

    def test_asmi_class(self):
        from cubos.instruments.asmi.vendors.vernier import VernierASMI
        assert get_instrument_class("asmi", "vernier") is VernierASMI

    def test_camera_class(self):
        from cubos.instruments.camera.vendors.raspberry_pi import RaspberryPiCamera
        assert get_instrument_class("camera", "raspberry_pi") is RaspberryPiCamera

    def test_mount_only_camera_class(self):
        from cubos.instruments.camera.vendors.mount_only import MountOnlyCamera
        assert get_instrument_class("camera", "mount_only") is MountOnlyCamera

    def test_filmetrics_class(self):
        from cubos.instruments.filmetrics.vendors.kla import KLAFilmetrics
        assert get_instrument_class("filmetrics", "kla") is KLAFilmetrics

    def test_mounted_tool_class(self):
        from cubos.instruments.mounted_tool.vendors.mount_only import MountOnlyTool
        assert get_instrument_class("mounted_tool", "mount_only") is MountOnlyTool

    def test_pipette_class(self):
        from cubos.instruments.pipette.vendors.opentrons import OpentronsPipette
        assert get_instrument_class("pipette", "opentrons") is OpentronsPipette

    def test_potentiostat_class(self):
        from cubos.instruments.potentiostat.vendors.admiral import AdmiralPotentiostat
        assert get_instrument_class("potentiostat", "admiral") is AdmiralPotentiostat

    def test_potentiostat_emstat_class(self):
        from cubos.instruments.potentiostat.vendors.emstat import EmstatPotentiostat
        assert get_instrument_class("potentiostat", "emstat") is EmstatPotentiostat

    def test_uv_curing_class(self):
        from cubos.instruments.uv_curing.vendors.excelitas import ExcelitasUVCuring
        assert get_instrument_class("uv_curing", "excelitas") is ExcelitasUVCuring

    def test_uvvis_ccs_class(self):
        from cubos.instruments.uvvis_ccs.vendors.thorlabs import ThorlabsUVVisCCS
        assert get_instrument_class("uvvis_ccs", "thorlabs") is ThorlabsUVVisCCS

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown instrument type"):
            get_instrument_class("nonexistent", "some_vendor")


class TestValidateInstrument:

    def test_valid_combinations_pass(self):
        valid_pairs = [
            ("asmi", "vernier"),
            ("camera", "mount_only"),
            ("camera", "raspberry_pi"),
            ("filmetrics", "kla"),
            ("mounted_tool", "mount_only"),
            ("pipette", "opentrons"),
            ("potentiostat", "admiral"),
            ("potentiostat", "emstat"),
            ("uv_curing", "excelitas"),
            ("uvvis_ccs", "thorlabs"),
        ]
        for type_key, vendor in valid_pairs:
            validate_instrument(type_key, vendor)

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown instrument type"):
            validate_instrument("nonexistent", "some_vendor")

    def test_wrong_vendor_raises(self):
        with pytest.raises(ValueError, match="not a supported vendor"):
            validate_instrument("uvvis_ccs", "wrong_vendor")

    def test_wrong_vendor_message_lists_allowed(self):
        with pytest.raises(ValueError, match="thorlabs"):
            validate_instrument("uvvis_ccs", "wrong_vendor")


class TestListMeasurementMethodParams:

    def test_dataclass_param_expands_to_its_fields(self):
        params = list_measurement_method_params("potentiostat", vendor="emstat")
        ocp = params["run_OCP"]
        assert ocp == [
            {
                "name": "params",
                "type": "OCPParams",
                "required": True,
                "default": None,
                "fields": [
                    {
                        "name": "duration_s",
                        "type": "float",
                        "required": True,
                        "default": None,
                        "fields": None,
                    },
                    {
                        "name": "sampling_interval_s",
                        "type": "float",
                        "required": False,
                        "default": 0.1,
                        "fields": None,
                    },
                ],
            }
        ]

    def test_engine_injected_params_are_excluded(self):
        params = list_measurement_method_params("asmi")
        indentation = params["indentation"]
        names = {spec["name"] for spec in indentation}
        assert names.isdisjoint(
            {"gantry", "well_z", "measurement_height", "indentation_limit_height"}
        )
        assert {"step_size", "force_limit"} <= names

    def test_filters_by_vendor(self):
        assert set(list_measurement_method_params("potentiostat", vendor="emstat")) == {
            "run_CA", "run_CP", "run_CV", "run_OCP",
        }

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown instrument type"):
            list_measurement_method_params("nonexistent")
