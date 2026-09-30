"""Tests for the PANDA-BEAR -> CubOS importer.

Uses only synthetic SQLite fixtures built by ``panda_bear_fixture_db``
(mirrors the real production schema; no real snapshot data is ever
committed here or read by these tests).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cubos.data.fluid_state import load_initial_fluids
from cubos.deck.loader import load_deck_from_yaml
from cubos.protocol_engine.setup_validator import run_setup_validation
from cubos.tools.import_panda_bear import main
from cubos.tools.panda_bear_import import db_reader as db_reader_module
from cubos.tools.panda_bear_import.build import build_import
from cubos.tools.panda_bear_import.conversion import Point, ToolOffset, WorkingVolume, to_gantry
from cubos.tools.panda_bear_import.db_reader import SourceChangedError, read_snapshot
from cubos.tools.panda_bear_import.resolutions import (
    Resolutions,
    ResolutionsError,
    empty_resolutions,
    load_resolutions,
)
from cubos.tools.panda_bear_import.yaml_io import write_yaml_with_header

from .panda_bear_fixture_db import (
    build_fixture_db,
    build_happy_path_db,
    default_2x2_tips,
    default_2x2_wells,
    default_tiprack,
    default_vials,
    default_wellplate,
    default_wellplate_type,
    tip_row,
    vial_row,
)

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

ZERO_OFFSETS = {
    "pipette": ToolOffset(x=0.0, y=0.0, z=0.0),
    "electrode": ToolOffset(x=0.0, y=0.0, z=0.0),
}
WIDE_OPEN_VOLUME = WorkingVolume(x_min=-1e6, x_max=1e6, y_min=-1e6, y_max=1e6, z_min=-1e6, z_max=1e6)
REAL_WORKING_VOLUME = WorkingVolume(x_min=-401.0, x_max=0.0, y_min=-301.0, y_max=0.0, z_min=-85.0, z_max=0.0)
MIN_GANTRY_RAW = {
    "serial_port": "/dev/ttyUSB0",
    "gantry_type": "cub_xl",
    "origin_policy": "home_origin",
    "cnc": {"factory_z_travel_mm": 100.0, "y_axis_motion": "head", "safe_z": -5.0},
    "working_volume": {
        "x_min": -401.0, "x_max": 0.0,
        "y_min": -301.0, "y_max": 0.0,
        "z_min": -85.0, "z_max": 0.0,
    },
    # max_travel_* must match the working volume span, or the built-in Cub XL
    # rail-avoidance geometry falls back to its reference (540, 300, 100)
    # span and the translated rail box overlaps the entire reachable
    # home-frame envelope. Mirrors cub_xl_panda_home_origin.yaml.
    "grbl_settings": {"max_travel_x": 401.0, "max_travel_y": 301.0, "max_travel_z": 85.0},
    "instruments": {
        "camera": {
            "type": "camera", "vendor": "mount_only", "offline": True,
            "offset_x": 0.0, "offset_y": 0.0, "depth": 0.0,
        },
    },
}


def make_db(tmp_path: Path, name: str = "panda.db", **overrides) -> Path:
    defaults = dict(
        vials=default_vials(),
        wellplate_types=[default_wellplate_type()],
        wellplates=[default_wellplate()],
        wells=default_2x2_wells(),
        tipracks=[default_tiprack()],
        tips=default_2x2_tips(),
    )
    defaults.update(overrides)
    return build_fixture_db(tmp_path / name, **defaults)


def write_min_gantry_yaml(tmp_path: Path, name: str = "gantry.yaml") -> Path:
    import yaml

    path = tmp_path / name
    path.write_text(yaml.safe_dump(MIN_GANTRY_RAW, sort_keys=True), encoding="utf-8")
    return path


def write_resolutions_yaml(tmp_path: Path, data: dict, name: str = "resolutions.yaml") -> Path:
    import yaml

    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    return path


def conflicts_by_category(result, category: str) -> list:
    return [c for c in result.conflicts if c.category == category]


# ---------------------------------------------------------------------------
# Coordinate conversion
# ---------------------------------------------------------------------------


def test_pipette_offset_conversion_matches_documented_constants():
    """Regression check tying conversion.py to the documented tool offsets.

    Matches the production s1 vial (top=-115.0) converting to gantry
    (-133.0, -7.0, -15.0), independently verified against the real DB.
    """
    from cubos.tools.panda_bear_import.constants import DEFAULT_TOOL_OFFSETS

    point = to_gantry(Point(x=-17.1, y=-0.9, z=-115.0), DEFAULT_TOOL_OFFSETS["pipette"])
    assert (point.x, point.y, point.z) == (-133.0, -7.0, -15.0)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_deterministic_double_run(tmp_path):
    db_path = build_happy_path_db(tmp_path / "panda.db")
    gantry_path = write_min_gantry_yaml(tmp_path)

    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    rc1 = main([str(db_path), "--no-resolutions", "--out-dir", str(out1), "--gantry-source", str(gantry_path)])
    rc2 = main([str(db_path), "--no-resolutions", "--out-dir", str(out2), "--gantry-source", str(gantry_path)])
    assert rc1 == 0
    assert rc2 == 0

    files = [
        "deck/panda_imported_deck.yaml",
        "deck/panda_initial_fluids.yaml",
        "gantry/cub_xl_panda_home_origin_full.yaml",
        "protocol/panda/position_tour.yaml",
        "panda_import_report.json",
    ]
    for rel in files:
        content1 = (out1 / rel).read_bytes()
        content2 = (out2 / rel).read_bytes()
        assert content1 == content2, f"{rel} differed across two runs"


# ---------------------------------------------------------------------------
# Source hash guard
# ---------------------------------------------------------------------------


def test_source_hash_unchanged_reads_fine(tmp_path):
    db_path = build_happy_path_db(tmp_path / "panda.db")
    snapshot = read_snapshot(db_path)
    assert snapshot.sha256 == db_reader_module.sha256_file(db_path)


def test_source_changed_mid_read_is_refused(tmp_path, monkeypatch):
    db_path = build_happy_path_db(tmp_path / "panda.db")
    hashes = iter(["hash-before", "hash-after"])
    monkeypatch.setattr(db_reader_module, "sha256_file", lambda _path: next(hashes))

    with pytest.raises(SourceChangedError):
        read_snapshot(db_path)


def test_cli_refuses_and_writes_nothing_on_source_change(tmp_path, monkeypatch, capsys):
    db_path = build_happy_path_db(tmp_path / "panda.db")
    gantry_path = write_min_gantry_yaml(tmp_path)
    out_dir = tmp_path / "out"

    hashes = iter(["hash-before", "hash-after"])
    monkeypatch.setattr(db_reader_module, "sha256_file", lambda _path: next(hashes))

    rc = main([str(db_path), "--no-resolutions", "--out-dir", str(out_dir), "--gantry-source", str(gantry_path)])
    assert rc == 1
    assert not out_dir.exists()


# ---------------------------------------------------------------------------
# Conflict detection: one test per class
# ---------------------------------------------------------------------------


def test_detects_duplicate_vial_position(tmp_path):
    vials = [
        vial_row(id=1, position="s1", category=0, x=-50.0, y=-10.0, z=-70.0),
        vial_row(id=2, position="s1", category=0, x=-50.0, y=-11.0, z=-70.0),
        vial_row(id=3, position="w1", category=1, x=-50.0, y=-40.0, z=-70.0),
        vial_row(id=4, position="e1", category=2, x=-350.0, y=-270.0, z=-70.0),
    ]
    snapshot = read_snapshot(make_db(tmp_path, vials=vials))
    result = build_import(snapshot, empty_resolutions(), ZERO_OFFSETS, WIDE_OPEN_VOLUME, MIN_GANTRY_RAW)

    dups = conflicts_by_category(result, "vial_duplicate_position")
    assert len(dups) == 1
    assert dups[0].row_ids == (1, 2)
    assert dups[0].severity == "conflict"
    assert not dups[0].resolved
    assert dups[0] in result.blocking

    resolved = build_import(
        snapshot, Resolutions(exclude_vial_ids=frozenset({2})), ZERO_OFFSETS, WIDE_OPEN_VOLUME, MIN_GANTRY_RAW,
    )
    assert not resolved.blocking
    assert "s1" in resolved.deck["labware"]


def test_detects_out_of_envelope_position_as_nonblocking_warning(tmp_path):
    snapshot = read_snapshot(make_db(tmp_path))
    tight_volume = WorkingVolume(x_min=-40.0, x_max=0.0, y_min=-40.0, y_max=0.0, z_min=-40.0, z_max=0.0)

    result = build_import(snapshot, empty_resolutions(), ZERO_OFFSETS, tight_volume, MIN_GANTRY_RAW)

    warnings = conflicts_by_category(result, "position_out_of_envelope")
    assert warnings, "expected at least one out-of-envelope warning"
    assert all(c.severity == "warning" and c.resolved for c in warnings)
    assert not result.blocking, "out-of-envelope must never block the import"
    # Still written as-is, not clamped.
    assert result.deck["labware"]["s1"]["location"]["x"] == -50.0


def test_detects_missing_tip_length(tmp_path):
    tips = default_2x2_tips(tip_length=None)
    snapshot = read_snapshot(make_db(tmp_path, tips=tips))

    result = build_import(snapshot, empty_resolutions(), ZERO_OFFSETS, WIDE_OPEN_VOLUME, MIN_GANTRY_RAW)
    conflicts = conflicts_by_category(result, "missing_tip_length")
    assert len(conflicts) == 1
    assert not conflicts[0].resolved
    assert result.blocking

    resolved = build_import(
        snapshot, Resolutions(tip_length_mm=59.3), ZERO_OFFSETS, WIDE_OPEN_VOLUME, MIN_GANTRY_RAW,
    )
    assert not resolved.blocking
    assert resolved.deck["labware"]["tip_rack"]["tip_length"] == 59.3


def test_detects_wellplate_height_source_mismatch(tmp_path):
    wellplates = [default_wellplate(height=99.0)]  # well_hx default height is 5.0
    snapshot = read_snapshot(make_db(tmp_path, wellplates=wellplates))

    result = build_import(snapshot, empty_resolutions(), ZERO_OFFSETS, WIDE_OPEN_VOLUME, MIN_GANTRY_RAW)
    conflicts = conflicts_by_category(result, "wellplate_height_source_mismatch")
    assert len(conflicts) == 1
    assert not conflicts[0].resolved
    assert result.blocking

    resolved = build_import(
        snapshot, Resolutions(wellplate_height_overrides={10: 5.0}), ZERO_OFFSETS, WIDE_OPEN_VOLUME, MIN_GANTRY_RAW,
    )
    assert not resolved.blocking
    assert "ito_pama_plate" in resolved.deck["labware"]


def test_detects_tiprack_shape_mismatch(tmp_path):
    tipracks = [default_tiprack(rows="ABCD", cols=15)]
    snapshot = read_snapshot(make_db(tmp_path, tipracks=tipracks))

    result = build_import(snapshot, empty_resolutions(), ZERO_OFFSETS, WIDE_OPEN_VOLUME, MIN_GANTRY_RAW)
    conflicts = conflicts_by_category(result, "tiprack_shape_mismatch")
    assert len(conflicts) == 1
    assert not conflicts[0].resolved
    assert result.blocking

    resolved = build_import(
        snapshot,
        Resolutions(tiprack_shape_overrides={1: (2, 2)}),
        ZERO_OFFSETS,
        WIDE_OPEN_VOLUME,
        MIN_GANTRY_RAW,
    )
    assert not resolved.blocking
    assert resolved.deck["labware"]["tip_rack"]["rows"] == 2
    assert resolved.deck["labware"]["tip_rack"]["columns"] == 2


def test_detects_tiprack_with_zero_tips(tmp_path):
    tipracks = [default_tiprack(id=1), default_tiprack(id=2)]
    tips = default_2x2_tips(rack_id=1)  # rack 2 gets none
    snapshot = read_snapshot(make_db(tmp_path, tipracks=tipracks, tips=tips))

    result = build_import(snapshot, empty_resolutions(), ZERO_OFFSETS, WIDE_OPEN_VOLUME, MIN_GANTRY_RAW)
    conflicts = conflicts_by_category(result, "tiprack_no_tips")
    assert len(conflicts) == 1
    assert conflicts[0].row_ids == (2,)
    assert not conflicts[0].resolved
    assert result.blocking

    resolved = build_import(
        snapshot,
        Resolutions(exclude_tiprack_ids=frozenset({2})),
        ZERO_OFFSETS,
        WIDE_OPEN_VOLUME,
        MIN_GANTRY_RAW,
    )
    assert not resolved.blocking


# ---------------------------------------------------------------------------
# Resolutions silence exactly their own conflict
# ---------------------------------------------------------------------------


def test_resolutions_silence_only_their_own_conflict(tmp_path):
    vials = [
        vial_row(id=1, position="s1", category=0, x=-50.0, y=-10.0, z=-70.0),
        vial_row(id=2, position="s1", category=0, x=-50.0, y=-11.0, z=-70.0),
        vial_row(id=3, position="w1", category=1, x=-50.0, y=-40.0, z=-70.0),
        vial_row(id=4, position="e1", category=2, x=-350.0, y=-270.0, z=-70.0),
    ]
    tips = default_2x2_tips(tip_length=None)
    snapshot = read_snapshot(make_db(tmp_path, vials=vials, tips=tips))

    resolutions = Resolutions(exclude_vial_ids=frozenset({2}))  # resolves only the vial dup
    result = build_import(snapshot, resolutions, ZERO_OFFSETS, WIDE_OPEN_VOLUME, MIN_GANTRY_RAW)

    by_category = {c.category: c for c in result.conflicts}
    assert by_category["vial_duplicate_position"].resolved
    assert not by_category["missing_tip_length"].resolved
    assert result.blocking == (by_category["missing_tip_length"],)


# ---------------------------------------------------------------------------
# Unresolved conflict -> non-zero exit, no partial writes
# ---------------------------------------------------------------------------


def test_cli_blocks_and_writes_nothing_on_unresolved_conflict(tmp_path, capsys):
    vials = [
        vial_row(id=1, position="s1", category=0, x=-50.0, y=-10.0, z=-70.0),
        vial_row(id=2, position="s1", category=0, x=-50.0, y=-11.0, z=-70.0),
        vial_row(id=3, position="w1", category=1, x=-50.0, y=-40.0, z=-70.0),
        vial_row(id=4, position="e1", category=2, x=-350.0, y=-270.0, z=-70.0),
    ]
    db_path = make_db(tmp_path, vials=vials)
    gantry_path = write_min_gantry_yaml(tmp_path)
    out_dir = tmp_path / "out"

    rc = main([str(db_path), "--no-resolutions", "--out-dir", str(out_dir), "--gantry-source", str(gantry_path)])

    assert rc == 1
    assert not out_dir.exists()
    captured = capsys.readouterr()
    assert "BLOCKED" in captured.err
    assert "vial_duplicate_position" in captured.err


def test_cli_succeeds_and_writes_all_outputs_when_resolved(tmp_path):
    vials = [
        vial_row(id=1, position="s1", category=0, x=-50.0, y=-10.0, z=-70.0),
        vial_row(id=2, position="s1", category=0, x=-50.0, y=-11.0, z=-70.0),
        vial_row(id=3, position="w1", category=1, x=-50.0, y=-40.0, z=-70.0),
        vial_row(id=4, position="e1", category=2, x=-350.0, y=-270.0, z=-70.0),
    ]
    db_path = make_db(tmp_path, vials=vials)
    gantry_path = write_min_gantry_yaml(tmp_path)
    resolutions_path = write_resolutions_yaml(tmp_path, {"exclude_vial_ids": [2]})
    out_dir = tmp_path / "out"

    rc = main([
        str(db_path), "--resolutions", str(resolutions_path),
        "--out-dir", str(out_dir), "--gantry-source", str(gantry_path),
    ])

    assert rc == 0
    for rel in (
        "deck/panda_imported_deck.yaml",
        "deck/panda_initial_fluids.yaml",
        "gantry/cub_xl_panda_home_origin_full.yaml",
        "protocol/panda/position_tour.yaml",
        "panda_import_report.json",
    ):
        assert (out_dir / rel).exists()


# ---------------------------------------------------------------------------
# Generated outputs load through the real CubOS loaders
# ---------------------------------------------------------------------------


def test_generated_deck_loads_via_deck_loader(tmp_path):
    snapshot = read_snapshot(build_happy_path_db(tmp_path / "panda.db"))
    result = build_import(snapshot, empty_resolutions(), ZERO_OFFSETS, REAL_WORKING_VOLUME, MIN_GANTRY_RAW)

    deck_path = tmp_path / "deck.yaml"
    write_yaml_with_header(deck_path, ["test fixture"], result.deck)

    deck = load_deck_from_yaml(deck_path)
    assert set(deck) == {"s1", "w1", "e1", "ito_pama_plate", "tip_rack", "tip_disposal"}


def test_generated_fluids_load_via_load_initial_fluids(tmp_path):
    snapshot = read_snapshot(build_happy_path_db(tmp_path / "panda.db"))
    result = build_import(snapshot, empty_resolutions(), ZERO_OFFSETS, REAL_WORKING_VOLUME, MIN_GANTRY_RAW)

    fluids_path = tmp_path / "fluids.yaml"
    write_yaml_with_header(fluids_path, ["test fixture"], result.fluids)

    fluids = load_initial_fluids(fluids_path)
    assert fluids["s1"] == {"volume_ul": 1000.0, "composition": {"water": 1000.0}}
    assert fluids["w1"] == {"volume_ul": 500.0, "composition": {"waste": 500.0}}
    assert fluids["e1"] == {"volume_ul": 2000.0, "composition": {"ebath": 2000.0}}


# ---------------------------------------------------------------------------
# Feature 05: role/solution emission from category + name
# ---------------------------------------------------------------------------


def test_vial_role_emitted_from_category(tmp_path):
    snapshot = read_snapshot(build_happy_path_db(tmp_path / "panda.db"))
    result = build_import(snapshot, empty_resolutions(), ZERO_OFFSETS, REAL_WORKING_VOLUME, MIN_GANTRY_RAW)

    labware = result.deck["labware"]
    assert labware["s1"]["role"] == "stock"  # category 0
    assert labware["w1"]["role"] == "waste"  # category 1
    assert labware["e1"]["role"] == "process"  # category 2 (electrode/bath)


def test_vial_solution_emitted_from_name(tmp_path):
    snapshot = read_snapshot(build_happy_path_db(tmp_path / "panda.db"))
    result = build_import(snapshot, empty_resolutions(), ZERO_OFFSETS, REAL_WORKING_VOLUME, MIN_GANTRY_RAW)

    labware = result.deck["labware"]
    assert labware["s1"]["solution"] == "water"
    assert labware["w1"]["solution"] == "waste"
    assert labware["e1"]["solution"] == "ebath"


def test_vial_solution_omitted_when_name_is_blank(tmp_path):
    vials = [
        vial_row(id=1, position="s1", category=0, x=-50.0, y=-10.0, z=-70.0, name="  "),
    ]
    snapshot = read_snapshot(make_db(tmp_path, vials=vials))
    result = build_import(snapshot, empty_resolutions(), ZERO_OFFSETS, WIDE_OPEN_VOLUME, MIN_GANTRY_RAW)

    labware = result.deck["labware"]["s1"]
    assert labware["role"] == "stock"
    assert "solution" not in labware


def test_vial_role_omitted_for_unrecognized_category(tmp_path):
    vials = [
        vial_row(id=1, position="s1", category=99, x=-50.0, y=-10.0, z=-70.0, name="mystery"),
    ]
    snapshot = read_snapshot(make_db(tmp_path, vials=vials))
    result = build_import(snapshot, empty_resolutions(), ZERO_OFFSETS, WIDE_OPEN_VOLUME, MIN_GANTRY_RAW)

    labware = result.deck["labware"]["s1"]
    assert "role" not in labware
    assert labware["solution"] == "mystery"


def test_generated_deck_role_and_solution_load_through_real_deck_loader(tmp_path):
    snapshot = read_snapshot(build_happy_path_db(tmp_path / "panda.db"))
    result = build_import(snapshot, empty_resolutions(), ZERO_OFFSETS, REAL_WORKING_VOLUME, MIN_GANTRY_RAW)

    deck_path = tmp_path / "deck.yaml"
    write_yaml_with_header(deck_path, ["test fixture"], result.deck)
    deck = load_deck_from_yaml(deck_path)

    assert deck["s1"].role == "stock"
    assert deck["s1"].solution == "water"
    assert deck["w1"].role == "waste"
    assert deck["e1"].role == "process"


def test_generated_deck_determinism_covers_role_and_solution_fields(tmp_path):
    """Regression guard: role/solution participate in the byte-identical
    determinism ``test_deterministic_double_run`` already checks at the
    file level -- this pins the actual values so a determinism regression
    that only shuffled these two new fields would still be caught."""
    db_path = build_happy_path_db(tmp_path / "panda.db")
    gantry_path = write_min_gantry_yaml(tmp_path)

    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    for out_dir in (out1, out2):
        rc = main([
            str(db_path), "--no-resolutions", "--out-dir", str(out_dir),
            "--gantry-source", str(gantry_path),
        ])
        assert rc == 0

    for out_dir in (out1, out2):
        deck = load_deck_from_yaml(out_dir / "deck" / "panda_imported_deck.yaml")
        assert deck["s1"].role == "stock"
        assert deck["s1"].solution == "water"


def test_generated_configs_pass_run_setup_validation_end_to_end(tmp_path):
    snapshot = read_snapshot(build_happy_path_db(tmp_path / "panda.db"))
    result = build_import(snapshot, empty_resolutions(), ZERO_OFFSETS, REAL_WORKING_VOLUME, MIN_GANTRY_RAW)

    deck_path = tmp_path / "deck.yaml"
    gantry_path = tmp_path / "gantry_full.yaml"
    tour_path = tmp_path / "tour.yaml"
    write_yaml_with_header(deck_path, ["test fixture"], result.deck)
    write_yaml_with_header(gantry_path, ["test fixture"], result.gantry_full)
    write_yaml_with_header(tour_path, ["test fixture"], result.tour)

    validation = run_setup_validation(gantry_path, deck_path, tour_path)
    assert validation.passed, validation.output


# ---------------------------------------------------------------------------
# Pitch / calibration derivation
# ---------------------------------------------------------------------------


def test_wellplate_and_tiprack_pitch_derivation(tmp_path):
    snapshot = read_snapshot(build_happy_path_db(tmp_path / "panda.db"))
    result = build_import(snapshot, empty_resolutions(), ZERO_OFFSETS, WIDE_OPEN_VOLUME, MIN_GANTRY_RAW)

    plate = result.deck["labware"]["ito_pama_plate"]
    assert plate["x_offset"] == 10.0
    assert plate["y_offset"] == 10.0
    assert plate["calibration"]["a1"] == {"x": -60.0, "y": -50.0, "z": -24.0}
    assert plate["calibration"]["a2"] == {"x": -60.0, "y": -60.0}

    rack = result.deck["labware"]["tip_rack"]
    assert rack["x_offset"] == 10.0
    assert rack["y_offset"] == 10.0
    assert rack["calibration"]["a1"] == {"x": -50.0, "y": -30.0, "z": -20.0}


def test_pitch_residual_reported_when_grid_is_irregular(tmp_path):
    from .panda_bear_fixture_db import well_row

    wells = default_2x2_wells()
    # Nudge B2 off-grid by 0.5mm -- above the 0.2mm pitch-residual tolerance.
    wells[-1] = well_row(plate_id=10, well_id="B2", x=-70.5, y=-60.0, z=-30.0)
    snapshot = read_snapshot(make_db(tmp_path, wells=wells))

    result = build_import(snapshot, empty_resolutions(), ZERO_OFFSETS, WIDE_OPEN_VOLUME, MIN_GANTRY_RAW)
    residual_warnings = conflicts_by_category(result, "well_pitch_residual")
    assert residual_warnings
    assert all(c.severity == "warning" and c.resolved for c in residual_warnings)
    assert not result.blocking


# ---------------------------------------------------------------------------
# Z semantics: computed mouth (location.z / calibration.a1.z) == DB top
# ---------------------------------------------------------------------------


def test_vial_location_z_equals_converted_db_top(tmp_path):
    snapshot = read_snapshot(build_happy_path_db(tmp_path / "panda.db"))
    offset = ToolOffset(x=3.0, y=-2.0, z=11.0)
    result = build_import(
        snapshot, empty_resolutions(), {"pipette": offset, "electrode": offset}, WIDE_OPEN_VOLUME, MIN_GANTRY_RAW,
    )

    s1 = next(v for v in snapshot.vials if v.position == "s1")
    expected_z = round(s1.top + offset.z, 3)
    assert result.deck["labware"]["s1"]["location"]["z"] == expected_z


def test_wellplate_calibration_a1_z_equals_converted_db_top(tmp_path):
    snapshot = read_snapshot(build_happy_path_db(tmp_path / "panda.db"))
    offset = ToolOffset(x=3.0, y=-2.0, z=11.0)
    result = build_import(
        snapshot, empty_resolutions(), {"pipette": offset, "electrode": offset}, WIDE_OPEN_VOLUME, MIN_GANTRY_RAW,
    )

    a1_well = next(w for w in snapshot.wells if w.well_id == "A1")
    expected_z = round(a1_well.top + offset.z, 3)
    assert result.deck["labware"]["ito_pama_plate"]["calibration"]["a1"]["z"] == expected_z


# ---------------------------------------------------------------------------
# Feature 06: capper/decapper instrument upgrade
# ---------------------------------------------------------------------------


def _gantry_raw_with_capper_mount(**overrides) -> dict:
    import copy

    raw = copy.deepcopy(MIN_GANTRY_RAW)
    entry = {
        "type": "mounted_tool",
        "vendor": "mount_only",
        "offline": True,
        "offset_x": 62.9,
        "offset_y": 5.1,
        "depth": 61.5,
    }
    entry.update(overrides)
    raw["instruments"]["vial_capper_decapper"] = entry
    return raw


def test_capper_mount_only_placeholder_upgraded_to_real_instrument(tmp_path):
    snapshot = read_snapshot(build_happy_path_db(tmp_path / "panda.db"))
    gantry_raw = _gantry_raw_with_capper_mount()
    result = build_import(
        snapshot, empty_resolutions(), ZERO_OFFSETS, WIDE_OPEN_VOLUME, gantry_raw,
    )

    entry = result.gantry_full["instruments"]["vial_capper_decapper"]
    assert entry["type"] == "capper"
    assert entry["vendor"] == "pawduino"
    # Calibrated mount geometry is preserved verbatim.
    assert entry["offset_x"] == 62.9
    assert entry["offset_y"] == 5.1
    assert entry["depth"] == 61.5
    assert entry["offline"] is True
    # Motion-sequence config is present (parameterizes decap/cap -- see
    # cubos.protocol_engine.commands.capper).
    assert isinstance(entry["engage_depth_mm"], float)
    assert "park_position" not in entry
    assert isinstance(entry["capture_retries"], int)
    assert isinstance(entry["capture_settle_s"], float)


def test_capper_instrument_absent_when_not_declared_in_source_gantry(tmp_path):
    """Instrument optional: no vial_capper_decapper mount -> nothing emitted."""
    snapshot = read_snapshot(build_happy_path_db(tmp_path / "panda.db"))
    result = build_import(
        snapshot, empty_resolutions(), ZERO_OFFSETS, WIDE_OPEN_VOLUME, MIN_GANTRY_RAW,
    )
    assert "vial_capper_decapper" not in result.gantry_full["instruments"]


def test_capper_entry_not_clobbered_when_already_upgraded(tmp_path):
    """A hand-authored/already-upgraded entry is left exactly as-is."""
    snapshot = read_snapshot(build_happy_path_db(tmp_path / "panda.db"))
    gantry_raw = _gantry_raw_with_capper_mount(
        type="capper", vendor="pawduino", engage_depth_mm=-99.0,
    )
    result = build_import(
        snapshot, empty_resolutions(), ZERO_OFFSETS, WIDE_OPEN_VOLUME, gantry_raw,
    )
    entry = result.gantry_full["instruments"]["vial_capper_decapper"]
    assert entry["vendor"] == "pawduino"
    assert entry["engage_depth_mm"] == -99.0


def test_capper_registry_accepts_the_generated_pawduino_entry(tmp_path):
    """The emitted capper entry is a valid type/vendor pair in the registry."""
    from cubos.instruments import registry

    snapshot = read_snapshot(build_happy_path_db(tmp_path / "panda.db"))
    gantry_raw = _gantry_raw_with_capper_mount()
    result = build_import(
        snapshot, empty_resolutions(), ZERO_OFFSETS, WIDE_OPEN_VOLUME, gantry_raw,
    )
    entry = result.gantry_full["instruments"]["vial_capper_decapper"]
    registry.validate_instrument(entry["type"], entry["vendor"])


def test_committed_home_origin_full_yaml_matches_current_upgrade_logic():
    """Regression: the checked-in generated config must match what the
    importer would produce today from the real source gantry YAML -- a
    drifted committed artifact would otherwise go unnoticed.
    """
    import yaml as yaml_module

    from cubos.tools.panda_bear_import.build import _build_gantry_full

    configs_dir = Path(__file__).resolve().parents[2] / "configs" / "gantry"
    raw = yaml_module.safe_load(
        (configs_dir / "cub_xl_panda_home_origin.yaml").read_text(encoding="utf-8")
    )
    committed = yaml_module.safe_load(
        (configs_dir / "cub_xl_panda_home_origin_full.yaml").read_text(encoding="utf-8")
    )
    pipette_offset = ToolOffset(x=-115.9, y=-6.1, z=100.0)
    produced = _build_gantry_full(raw, pipette_offset)

    assert (
        produced["instruments"]["vial_capper_decapper"]
        == committed["instruments"]["vial_capper_decapper"]
    )


# ---------------------------------------------------------------------------
# Resolutions file parsing
# ---------------------------------------------------------------------------


def test_committed_default_resolutions_file_loads():
    path = Path(__file__).resolve().parents[2] / "configs" / "deck" / "panda_import_resolutions.yaml"
    resolutions = load_resolutions(path)
    assert resolutions.exclude_vial_ids == frozenset({10, 12, 13, 14})
    assert resolutions.tiprack_shape_overrides == {1: (2, 12)}
    assert resolutions.exclude_tiprack_ids == frozenset({2})
    assert resolutions.tip_length_mm == 59.3
    assert resolutions.wellplate_height_overrides == {120: 9.0}


def test_resolutions_rejects_unknown_keys(tmp_path):
    path = write_resolutions_yaml(tmp_path, {"not_a_real_key": True})
    with pytest.raises(ResolutionsError):
        load_resolutions(path)


def test_resolutions_rejects_non_integer_ids(tmp_path):
    path = write_resolutions_yaml(tmp_path, {"exclude_vial_ids": ["not-an-int"]})
    with pytest.raises(ResolutionsError):
        load_resolutions(path)
