import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cubos.gantry.errors import GantryLoaderError
from cubos.gantry.instrument_loader import (
    load_instrumented_gantry_from_config,
    load_instrumented_gantry_from_yaml,
    load_instrumented_gantry_from_yaml_safe,
)
from cubos.gantry.instrument_mount import InstrumentedGantry
from cubos.gantry.loader import load_gantry_from_yaml
from cubos.instruments.asmi.vendors.vernier import VernierASMI
from cubos.instruments.filmetrics.vendors.kla import KLAFilmetrics
from cubos.instruments.pipette.vendors.opentrons import OpentronsPipette
from cubos.instruments.camera.vendors.mount_only import MountOnlyCamera
from cubos.instruments.camera.vendors.raspberry_pi import RaspberryPiCamera
from cubos.instruments.mounted_tool.vendors.mount_only import MountOnlyTool
from cubos.instruments.uvvis_ccs.vendors.thorlabs import ThorlabsUVVisCCS
from cubos.instruments.yaml_schema import InstrumentYamlEntry


def _mock_controller():
    controller = MagicMock()
    controller.get_coordinates.return_value = {"x": 0.0, "y": 0.0, "z": 0.0}
    return controller


def _write_gantry_yaml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "gantry.yaml"
    path.write_text(textwrap.dedent(content))
    return path


def _gantry_yaml(
    instruments: str,
    *,
    grbl_settings: str = "",
    safe_z: str = "",
) -> str:
    lines = [
        "serial_port: /dev/ttyUSB0",
        "gantry_type: cub_xl",
        "cnc:",
        "  factory_z_travel_mm: 90.0",
    ]
    if safe_z:
        lines.append(f"  {safe_z}")
    lines.extend([
        "working_volume:",
        "  x_min: 0.0",
        "  x_max: 300.0",
        "  y_min: 0.0",
        "  y_max: 200.0",
        "  z_min: 0.0",
        "  z_max: 80.0",
    ])
    if grbl_settings:
        lines.extend(textwrap.dedent(grbl_settings).strip().splitlines())
    lines.append("instruments:")
    lines.extend(
        textwrap.indent(textwrap.dedent(instruments).strip(), "  ").splitlines()
    )
    return "\n".join(lines) + "\n"


class TestInstrumentYamlEntry:
    def test_allows_extra_fields(self):
        entry = InstrumentYamlEntry(
            type="uvvis_ccs",
            vendor="thorlabs",
            offset_x=1.0,
            serial_number="ABC123",
        )
        assert entry.type == "uvvis_ccs"
        assert entry.vendor == "thorlabs"
        assert entry.model_extra["serial_number"] == "ABC123"

    def test_defaults_for_optional_fields(self):
        entry = InstrumentYamlEntry(type="uvvis_ccs", vendor="thorlabs")
        assert entry.offset_x == 0.0
        assert entry.offset_y == 0.0
        assert entry.depth == 0.0
        assert not hasattr(entry, "measurement_height")

    def test_missing_vendor_raises(self):
        with pytest.raises(Exception):
            InstrumentYamlEntry(type="uvvis_ccs")


class TestLoadInstrumentedGantryFromConfig:
    def test_loads_instruments_embedded_in_gantry_yaml(self, tmp_path):
        gantry_path = _write_gantry_yaml(
            tmp_path,
            _gantry_yaml(
                """
                asmi:
                  type: asmi
                  vendor: vernier
                """,
                grbl_settings="""\
                grbl_settings:
                  status_report: 0
                  homing_enable: true
                """,
            ),
        )
        gantry_config = load_gantry_from_yaml(gantry_path)
        mounted = load_instrumented_gantry_from_config(
            gantry_config,
            _mock_controller(),
            mock_mode=True,
        )

        assert isinstance(mounted, InstrumentedGantry)
        assert isinstance(mounted.instruments["asmi"], VernierASMI)
        assert mounted.instruments["asmi"]._offline is True
        assert mounted.expected_grbl_settings == {"$10": 0.0, "$22": 1.0}
        assert mounted.safe_z == 80.0

    def test_uses_explicit_safe_z(self, tmp_path):
        gantry_path = _write_gantry_yaml(
            tmp_path,
            _gantry_yaml(
                """
                uvvis:
                  type: uvvis_ccs
                  vendor: thorlabs
                """,
                safe_z="safe_z: 60.0",
            ),
        )
        gantry_config = load_gantry_from_yaml(gantry_path)
        mounted = load_instrumented_gantry_from_config(
            gantry_config,
            _mock_controller(),
        )
        assert mounted.safe_z == 60.0

    def test_requires_embedded_instruments(self, tmp_path):
        gantry_path = _write_gantry_yaml(
            tmp_path,
            """\
            serial_port: /dev/ttyUSB0
            gantry_type: cub_xl
            cnc:
              factory_z_travel_mm: 90.0
            working_volume:
              x_min: 0.0
              x_max: 300.0
              y_min: 0.0
              y_max: 200.0
              z_min: 0.0
              z_max: 80.0
            """,
        )
        gantry_config = load_gantry_from_yaml(gantry_path)
        with pytest.raises(ValueError, match="instruments"):
            load_instrumented_gantry_from_config(gantry_config, _mock_controller())


class TestLoadInstrumentedGantryFromYaml:
    def test_loads_directly_from_gantry_yaml(self, tmp_path):
        gantry_path = _write_gantry_yaml(
            tmp_path,
            _gantry_yaml(
                """
                uvvis:
                  type: uvvis_ccs
                  vendor: thorlabs
                  offset_x: 15.0
                  offset_y: 0.0
                  depth: 5.0
                """
            ),
        )
        mounted = load_instrumented_gantry_from_yaml(gantry_path, _mock_controller())
        instr = mounted.instruments["uvvis"]
        assert isinstance(instr, ThorlabsUVVisCCS)
        assert instr.offset_x == 15.0
        assert instr.offset_y == 0.0
        assert instr.depth == 5.0

    def test_loads_multiple_instruments(self, tmp_path):
        gantry_path = _write_gantry_yaml(
            tmp_path,
            _gantry_yaml(
                """
                uvvis:
                  type: uvvis_ccs
                  vendor: thorlabs
                  offset_x: 15.0
                pipette:
                  type: pipette
                  vendor: opentrons
                  offset_x: 10.0
                  offset_y: 5.0
                  depth: 2.0
                """
            ),
        )
        mounted = load_instrumented_gantry_from_yaml(gantry_path, _mock_controller())
        assert len(mounted.instruments) == 2
        assert isinstance(mounted.instruments["uvvis"], ThorlabsUVVisCCS)
        assert isinstance(mounted.instruments["pipette"], OpentronsPipette)
        assert mounted.instruments["pipette"].offset_x == 10.0

    def test_loads_filmetrics(self, tmp_path):
        gantry_path = _write_gantry_yaml(
            tmp_path,
            _gantry_yaml(
                """
                film:
                  type: filmetrics
                  vendor: kla
                  offset_x: 20.0
                """
            ),
        )
        mounted = load_instrumented_gantry_from_yaml(gantry_path, _mock_controller())
        instr = mounted.instruments["film"]
        assert isinstance(instr, KLAFilmetrics)
        assert instr.offset_x == 20.0

    def test_loads_rpi_camera(self, tmp_path):
        gantry_path = _write_gantry_yaml(
            tmp_path,
            _gantry_yaml(
                """
                camera:
                  type: camera
                  vendor: raspberry_pi
                  offset_x: -12.0
                  offset_y: -4.0
                  depth: 3.0
                  offline: true
                """
            ),
        )
        mounted = load_instrumented_gantry_from_yaml(gantry_path, _mock_controller())
        instr = mounted.instruments["camera"]
        assert isinstance(instr, RaspberryPiCamera)
        assert instr.offset_x == -12.0
        assert instr.offset_y == -4.0
        assert instr.depth == 3.0

    def test_loads_mount_only_camera(self, tmp_path):
        gantry_path = _write_gantry_yaml(
            tmp_path,
            _gantry_yaml(
                """
                camera:
                  type: camera
                  vendor: mount_only
                  offset_x: -12.0
                  offset_y: -4.0
                  depth: 3.0
                  offline: true
                """
            ),
        )
        mounted = load_instrumented_gantry_from_yaml(gantry_path, _mock_controller())
        instr = mounted.instruments["camera"]
        assert isinstance(instr, MountOnlyCamera)
        assert instr.offset_x == -12.0
        assert instr.offset_y == -4.0
        assert instr.depth == 3.0

    def test_loads_mount_only_tool(self, tmp_path):
        gantry_path = _write_gantry_yaml(
            tmp_path,
            _gantry_yaml(
                """
                capper:
                  type: mounted_tool
                  vendor: mount_only
                  offset_x: 8.0
                  offset_y: 1.5
                  depth: 12.0
                  offline: true
                """
            ),
        )
        mounted = load_instrumented_gantry_from_yaml(gantry_path, _mock_controller())
        instr = mounted.instruments["capper"]
        assert isinstance(instr, MountOnlyTool)
        assert instr.offset_x == 8.0
        assert instr.offset_y == 1.5
        assert instr.depth == 12.0

    def test_invalid_vendor_raises_value_error(self, tmp_path):
        gantry_path = _write_gantry_yaml(
            tmp_path,
            _gantry_yaml(
                """
                uvvis:
                  type: uvvis_ccs
                  vendor: wrong_vendor
                """
            ),
        )
        with pytest.raises(ValueError, match="not a supported vendor"):
            load_instrumented_gantry_from_yaml(gantry_path, _mock_controller())

    def test_invalid_vendor_safe_loader_raises_gantry_loader_error(self, tmp_path):
        gantry_path = _write_gantry_yaml(
            tmp_path,
            _gantry_yaml(
                """
                uvvis:
                  type: uvvis_ccs
                  vendor: wrong_vendor
                """
            ),
        )
        with pytest.raises(GantryLoaderError, match="Instrument validation error"):
            load_instrumented_gantry_from_yaml_safe(
                gantry_path,
                _mock_controller(),
            )

    def test_all_valid_vendor_combos_load(self, tmp_path):
        pairs = [
            ("asmi", "vernier"),
            ("camera", "mount_only"),
            ("camera", "raspberry_pi"),
            ("filmetrics", "kla"),
            ("mounted_tool", "mount_only"),
            ("pipette", "opentrons"),
            ("uv_curing", "excelitas"),
            ("uvvis_ccs", "thorlabs"),
        ]
        for type_key, vendor in pairs:
            gantry_path = _write_gantry_yaml(
                tmp_path,
                _gantry_yaml(
                    f"""\
                    inst:
                      type: {type_key}
                      vendor: {vendor}
                    """
                ),
            )
            mounted = load_instrumented_gantry_from_yaml(
                gantry_path,
                _mock_controller(),
            )
            assert "inst" in mounted.instruments


class TestLoadInstrumentedGantryMockMode:
    def test_mock_mode_creates_offline_instrument(self, tmp_path):
        gantry_path = _write_gantry_yaml(
            tmp_path,
            _gantry_yaml(
                """
                pip:
                  type: pipette
                  vendor: opentrons
                  offset_x: -10.0
                """
            ),
        )
        mounted = load_instrumented_gantry_from_yaml(
            gantry_path,
            _mock_controller(),
            mock_mode=True,
        )
        assert isinstance(mounted.instruments["pip"], OpentronsPipette)
        assert mounted.instruments["pip"]._offline is True

    def test_mock_mode_false_keeps_online(self, tmp_path):
        gantry_path = _write_gantry_yaml(
            tmp_path,
            _gantry_yaml(
                """
                uvvis:
                  type: uvvis_ccs
                  vendor: thorlabs
                """
            ),
        )
        mounted = load_instrumented_gantry_from_yaml(
            gantry_path,
            _mock_controller(),
            mock_mode=False,
        )
        assert isinstance(mounted.instruments["uvvis"], ThorlabsUVVisCCS)
        assert mounted.instruments["uvvis"]._offline is False

    def test_mock_mode_swaps_all_instruments(self, tmp_path):
        gantry_path = _write_gantry_yaml(
            tmp_path,
            _gantry_yaml(
                """
                pip:
                  type: pipette
                  vendor: opentrons
                uvvis:
                  type: uvvis_ccs
                  vendor: thorlabs
                film:
                  type: filmetrics
                  vendor: kla
                """
            ),
        )
        mounted = load_instrumented_gantry_from_yaml(
            gantry_path,
            _mock_controller(),
            mock_mode=True,
        )
        assert isinstance(mounted.instruments["pip"], OpentronsPipette)
        assert mounted.instruments["pip"]._offline is True
        assert isinstance(mounted.instruments["uvvis"], ThorlabsUVVisCCS)
        assert mounted.instruments["uvvis"]._offline is True
        assert isinstance(mounted.instruments["film"], KLAFilmetrics)
        assert mounted.instruments["film"]._offline is True


class TestRetiredInstrumentFields:
    def _capper_yaml(self, extra: str = ""):
        return _gantry_yaml(
            f"""
            capper:
              type: capper
              vendor: pawduino
              engage_depth_mm: -15.0
              {extra}
            """
        )

    def test_park_position_is_dropped_with_a_warning(self, tmp_path, caplog):
        gantry_path = _write_gantry_yaml(
            tmp_path, self._capper_yaml("park_position: [-10.0, -10.0]"),
        )
        with caplog.at_level("WARNING", logger="cubos.gantry.instrument_loader"):
            mounted = load_instrumented_gantry_from_config(
                load_gantry_from_yaml(gantry_path), _mock_controller(), mock_mode=True,
            )
        capper = mounted.instruments["capper"]
        assert not hasattr(capper, "park_position")
        assert "park_position" in caplog.text
        assert "no longer used" in caplog.text

    def test_other_unknown_fields_still_fail(self, tmp_path):
        gantry_path = _write_gantry_yaml(
            tmp_path, self._capper_yaml("engage_depth: -15.0"),
        )
        with pytest.raises(ValueError, match="unsupported YAML field.*engage_depth"):
            load_instrumented_gantry_from_config(
                load_gantry_from_yaml(gantry_path), _mock_controller(), mock_mode=True,
            )

    def test_retired_field_is_type_scoped(self, tmp_path):
        gantry_path = _write_gantry_yaml(
            tmp_path,
            _gantry_yaml(
                """
                camera:
                  type: camera
                  vendor: mount_only
                  park_position: [1.0, 2.0]
                """
            ),
        )
        with pytest.raises(ValueError, match="unsupported YAML field.*park_position"):
            load_instrumented_gantry_from_config(
                load_gantry_from_yaml(gantry_path), _mock_controller(), mock_mode=True,
            )
