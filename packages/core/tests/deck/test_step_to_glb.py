from __future__ import annotations

import sys
import types

import pytest

from cubos.deck.labware.definitions import step_to_glb


def test_resolve_io_single_file_accepts_file_or_directory_output(tmp_path):
    step_path = tmp_path / "part.step"
    step_path.write_text("ISO-10303-21;")

    assert step_to_glb.resolve_io(step_path, tmp_path / "custom.glb") == [
        (step_path, tmp_path / "custom.glb"),
    ]
    assert step_to_glb.resolve_io(step_path, tmp_path / "out") == [
        (step_path, tmp_path / "out" / "part.glb"),
    ]


def test_resolve_io_directory_sorts_supported_files(tmp_path):
    (tmp_path / "b.stl").write_text("solid b\nendsolid b\n")
    (tmp_path / "a.step").write_text("ISO-10303-21;")
    (tmp_path / "ignore.txt").write_text("nope")
    out_dir = tmp_path / "glb"

    assert step_to_glb.resolve_io(tmp_path, out_dir) == [
        (tmp_path / "a.step", out_dir / "a.glb"),
        (tmp_path / "b.stl", out_dir / "b.glb"),
    ]


def test_resolve_io_rejects_bad_inputs(tmp_path):
    txt_path = tmp_path / "part.txt"
    txt_path.write_text("nope")

    with pytest.raises(ValueError, match="not a STEP or STL file"):
        step_to_glb.resolve_io(txt_path, None)
    with pytest.raises(FileNotFoundError):
        step_to_glb.resolve_io(tmp_path / "missing.step", None)
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(ValueError, match="No STEP/STL files"):
        step_to_glb.resolve_io(empty_dir, None)


def test_converter_helpers_use_expected_library_apis(monkeypatch, tmp_path):
    saves = []
    exports = []

    class FakeAssembly:
        def __init__(self, shape, name):
            self.shape = shape
            self.name = name

        def save(self, path, **kwargs):
            saves.append((self.shape, self.name, path, kwargs))

    class FakeMesh:
        def export(self, path):
            exports.append(path)

    fake_cadquery = types.SimpleNamespace(
        importers=types.SimpleNamespace(importStep=lambda path: ("shape", path)),
        Assembly=FakeAssembly,
    )
    fake_trimesh = types.SimpleNamespace(
        load=lambda path, force: FakeMesh(),
    )
    monkeypatch.setitem(sys.modules, "cadquery", fake_cadquery)
    monkeypatch.setitem(sys.modules, "trimesh", fake_trimesh)

    step_to_glb._convert_step(tmp_path / "part.step", tmp_path / "part.glb", 0.1, 0.2)
    step_to_glb._convert_stl(tmp_path / "mesh.stl", tmp_path / "mesh.glb")

    assert saves == [
        (
            ("shape", str(tmp_path / "part.step")),
            "part",
            str(tmp_path / "part.glb"),
            {"exportType": "GLTF", "tolerance": 0.1, "angularTolerance": 0.2},
        ),
    ]
    assert exports == [str(tmp_path / "mesh.glb")]


def test_convert_dispatches_by_suffix_without_loading_cad_libraries(monkeypatch, tmp_path):
    calls = []

    def fake_step(src_path, glb_path, tolerance, angular_tolerance):
        calls.append(("step", src_path, glb_path, tolerance, angular_tolerance))

    def fake_stl(src_path, glb_path):
        calls.append(("stl", src_path, glb_path))

    monkeypatch.setattr(step_to_glb, "_convert_step", fake_step)
    monkeypatch.setattr(step_to_glb, "_convert_stl", fake_stl)

    step_to_glb.convert(tmp_path / "part.step", tmp_path / "out" / "part.glb", 0.1, 0.2)
    step_to_glb.convert(tmp_path / "mesh.stl", tmp_path / "out" / "mesh.glb", 0.3, 0.4)

    assert calls == [
        ("step", tmp_path / "part.step", tmp_path / "out" / "part.glb", 0.1, 0.2),
        ("stl", tmp_path / "mesh.stl", tmp_path / "out" / "mesh.glb"),
    ]
    assert (tmp_path / "out").is_dir()
    with pytest.raises(ValueError, match="Unsupported file type"):
        step_to_glb.convert(tmp_path / "part.txt", tmp_path / "out" / "part.glb", 0.1, 0.2)


def test_main_reports_resolution_errors(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "argv", ["step_to_glb.py", str(tmp_path / "missing.step")])

    assert step_to_glb.main() == 1

    captured = capsys.readouterr()
    assert "error:" in captured.err


def test_main_runs_resolved_jobs(monkeypatch, capsys, tmp_path):
    stl_path = tmp_path / "part.stl"
    stl_path.write_text("solid part\nendsolid part\n")
    calls = []

    def fake_convert(src_path, glb_path, tolerance, angular_tolerance):
        calls.append((src_path, glb_path, tolerance, angular_tolerance))

    monkeypatch.setattr(step_to_glb, "convert", fake_convert)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "step_to_glb.py",
            str(stl_path),
            "--tolerance",
            "0.05",
            "--angular-tolerance",
            "0.25",
        ],
    )

    assert step_to_glb.main() == 0

    assert calls == [(stl_path, tmp_path / "part.glb", 0.05, 0.25)]
    captured = capsys.readouterr()
    assert "Converted 1 file(s)" in captured.out
