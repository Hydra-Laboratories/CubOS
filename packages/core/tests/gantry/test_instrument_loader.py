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
from cubos.gantry.loader import load_gantry_from_yaml
from cubos.instruments.filmetrics.vendors.kla import KLAFilmetrics
from cubos.instruments.pipette.vendors.opentrons import OpentronsPipette
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


    def test_missing_vendor_raises(self):
        with pytest.raises(Exception):
            InstrumentYamlEntry(type="uvvis_ccs")


class TestLoadInstrumentedGantryFromConfig:


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
