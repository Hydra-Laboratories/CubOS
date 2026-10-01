"""Tests for protocol YAML loading and compilation."""

import tempfile
from pathlib import Path

import pytest

from cubos.protocol_engine.errors import ProtocolLoaderError
from cubos.protocol_engine.loader import load_protocol_from_yaml, load_protocol_from_yaml_safe


# ─── Valid protocol YAML fixtures ─────────────────────────────────────────────

VALID_SINGLE_MOVE = """
protocol:
  - move:
      instrument: pipette
      position: plate_1.A1
"""

VALID_TWO_MOVES = """
protocol:
  - move:
      instrument: pipette
      position: plate_1.A1
  - move:
      instrument: pipette
      position: plate_1.C9
"""

VALID_MEASURE_WITH_OMITTED_DEFAULTS = """
protocol:
  - measure:
      instrument: uvvis
      position: plate_1.A1
      measurement_height: 0.0
"""

VALID_SCAN_WITH_NEW_NAMES = """
protocol:
  - scan:
      plate: plate_1
      instrument: uvvis
      method: measure
      measurement_height: 0.0
      interwell_scan_height: 10.0
"""

VALID_TRANSFER_WITH_HEIGHTS = """
protocol:
  - transfer:
      source: vial_1
      destination: plate_1.A1
      volume_ul: 50.0
      source_height: 2.0
      destination_height: -1.0
"""

VALID_SERIAL_TRANSFER = """
protocol:
  - serial_transfer:
      source: vial_1
      plate: plate_1
      axis: A
      volumes: [5.0, 10.0]
"""


def _write_yaml(content: str) -> str:
    """Write YAML content to a temp file and return its path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write(content)
    f.close()
    return f.name


# ─── Valid loading ────────────────────────────────────────────────────────────


def test_scan_rejects_yaml_missing_measurement_height():
    """`measurement_height` is required on `scan`. Pin the registry-derived
    Pydantic schema contract — a future refactor adding a default value
    would silently re-enable footgun YAMLs without breaking anything else."""
    yaml = """
protocol:
  - scan:
      plate: plate_1
      instrument: uvvis
      method: measure
      interwell_scan_height: 10.0
"""
    path = _write_yaml(yaml)
    try:
        with pytest.raises(Exception, match="measurement_height"):
            load_protocol_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_scan_rejects_yaml_missing_interwell_scan_height():
    yaml = """
protocol:
  - scan:
      plate: plate_1
      instrument: uvvis
      method: measure
      measurement_height: 0.0
"""
    path = _write_yaml(yaml)
    try:
        with pytest.raises(Exception, match="interwell_scan_height"):
            load_protocol_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_measure_rejects_yaml_missing_measurement_height():
    yaml = """
protocol:
  - measure:
      instrument: uvvis
      position: plate_1.A1
"""
    path = _write_yaml(yaml)
    try:
        with pytest.raises(Exception, match="measurement_height"):
            load_protocol_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_scan_top_level_safe_approach_height_routes_rename_hint():
    """Top-level legacy scan fields are caught by Pydantic's generic
    `extra_forbidden` error. The loader's exception formatter intercepts
    and substitutes a rename hint pointing at the new field name."""
    yaml = """
protocol:
  - scan:
      plate: plate_1
      instrument: uvvis
      method: measure
      measurement_height: 0.0
      safe_approach_height: 10.0
"""
    path = _write_yaml(yaml)
    try:
        with pytest.raises(Exception, match="interwell_scan_height"):
            load_protocol_from_yaml_safe(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_scan_top_level_indentation_limit_routes_semantic_shift_hint():
    """`indentation_limit` was both renamed AND semantically flipped from a
    sign-agnostic magnitude to a signed labware-relative offset. Surface
    the meaning change so a user porting `indentation_limit: 5.0` doesn't
    accidentally write `indentation_limit_height: 5.0` (which would put
    the deepest plane *above* the well surface)."""
    yaml = """
protocol:
  - scan:
      plate: plate_1
      instrument: uvvis
      method: measure
      measurement_height: 0.0
      interwell_scan_height: 10.0
      indentation_limit: 5.0
"""
    path = _write_yaml(yaml)
    try:
        with pytest.raises(Exception, match="signed"):
            load_protocol_from_yaml_safe(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_scan_top_level_entry_travel_height_routes_rename_hint():
    yaml = """
protocol:
  - scan:
      plate: plate_1
      instrument: uvvis
      method: measure
      measurement_height: 0.0
      interwell_scan_height: 10.0
      entry_travel_height: 10.0
"""
    path = _write_yaml(yaml)
    try:
        with pytest.raises(Exception, match="safe_z"):
            load_protocol_from_yaml_safe(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_scan_top_level_interwell_travel_height_routes_rename_hint():
    yaml = """
protocol:
  - scan:
      plate: plate_1
      instrument: uvvis
      method: measure
      measurement_height: 0.0
      interwell_travel_height: 10.0
"""
    path = _write_yaml(yaml)
    try:
        with pytest.raises(Exception, match="interwell_scan_height"):
            load_protocol_from_yaml_safe(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_scan_top_level_z_limit_routes_rename_hint():
    yaml = """
protocol:
  - scan:
      plate: plate_1
      instrument: uvvis
      method: measure
      measurement_height: 0.0
      interwell_scan_height: 10.0
      z_limit: 5.0
"""
    path = _write_yaml(yaml)
    try:
        with pytest.raises(Exception, match="indentation_limit_height"):
            load_protocol_from_yaml_safe(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_yaml_rejects_two_element_named_position_cleanly():
    yaml = """
positions:
  bad: [1.0, 2.0]
protocol:
  - move:
      instrument: pipette
      position: bad
"""
    path = _write_yaml(yaml)
    try:
        with pytest.raises(Exception, match="exactly three finite XYZ"):
            load_protocol_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_yaml_rejects_nan_named_position_cleanly():
    yaml = """
positions:
  bad: [1.0, .nan, 3.0]
protocol:
  - move:
      instrument: pipette
      position: bad
"""
    path = _write_yaml(yaml)
    try:
        with pytest.raises(Exception, match="finite float"):
            load_protocol_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)


# ─── Safe loader error formatting ────────────────────────────────────────────


def test_safe_loader_unknown_command_has_clean_error():
    yaml = """
protocol:
  - unknown_cmd:
      a: b
"""
    path = _write_yaml(yaml)
    try:
        with pytest.raises(ProtocolLoaderError) as exc_info:
            load_protocol_from_yaml_safe(path)
        message = str(exc_info.value)
        assert "Protocol YAML error" in message
        assert "How to fix:" in message
    finally:
        Path(path).unlink(missing_ok=True)


def test_safe_loader_extra_args_has_clean_error():
    yaml = """
protocol:
  - move:
      instrument: pipette
      position: plate_1.A1
      extra_field: bad
"""
    path = _write_yaml(yaml)
    try:
        with pytest.raises(ProtocolLoaderError) as exc_info:
            load_protocol_from_yaml_safe(path)
        message = str(exc_info.value)
        assert "How to fix:" in message
    finally:
        Path(path).unlink(missing_ok=True)


def test_safe_loader_yaml_parse_error_has_clean_error():
    bad_yaml = "protocol:\n  - move:\n    instrument: [\n"
    path = _write_yaml(bad_yaml)
    try:
        with pytest.raises(ProtocolLoaderError) as exc_info:
            load_protocol_from_yaml_safe(path)
        message = str(exc_info.value)
        assert "parse error" in message.lower()
        assert "How to fix:" in message
    finally:
        Path(path).unlink(missing_ok=True)


def test_safe_loader_missing_file_has_clean_error():
    missing_path = "/tmp/this_protocol_does_not_exist_12345.yaml"
    with pytest.raises(ProtocolLoaderError) as exc_info:
        load_protocol_from_yaml_safe(missing_path)
    message = str(exc_info.value)
    assert "Protocol loader error" in message
    assert "How to fix:" in message


def test_empty_yaml_fails():
    path = _write_yaml("")
    try:
        with pytest.raises(Exception):
            load_protocol_from_yaml(path)
    finally:
        Path(path).unlink(missing_ok=True)
