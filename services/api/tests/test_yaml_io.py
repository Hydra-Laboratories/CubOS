"""Test YAML I/O utilities."""

import tempfile
from pathlib import Path

import pytest

from cubos_api.services.yaml_io import (
    YamlConfigError,
    classify_config,
    read_yaml,
    resolve_config_path,
    safe_filename,
    write_yaml,
)


def test_read_write_roundtrip():
    data = {"labware": {"plate1": {"type": "well_plate", "name": "test"}}}
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
        path = Path(f.name)
    write_yaml(path, data)
    result = read_yaml(path)
    assert result == data
    path.unlink()


def test_classify_deck():
    assert classify_config({"labware": {}}) == "deck"


def test_classify_gantry():
    assert classify_config({"working_volume": {}}) == "gantry"


def test_classify_gantry_with_embedded_instruments():
    assert classify_config({"working_volume": {}, "instruments": {}}) == "gantry"


def test_classify_protocol():
    assert classify_config({"protocol": []}) == "protocol"


def test_classify_unknown():
    assert classify_config({"other": {}}) is None


def test_read_empty_yaml():
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
        f.write("")
        path = Path(f.name)
    result = read_yaml(path)
    assert result == {}
    path.unlink()


def test_read_yaml_rejects_non_mapping(tmp_path):
    path = tmp_path / "scalar.yaml"
    path.write_text("protocol\n", encoding="utf-8")

    with pytest.raises(YamlConfigError, match="is not a YAML mapping"):
        read_yaml(path)


def test_write_yaml_preserves_comments_on_roundtrip(tmp_path):
    path = tmp_path / "commented.yaml"
    path.write_text(
        """\
# deck comment
labware:
  # plate comment
  plate:
    type: well_plate
""",
        encoding="utf-8",
    )

    data = read_yaml(path)
    data["labware"]["plate"]["name"] = "Commented Plate"
    write_yaml(path, data)

    text = path.read_text(encoding="utf-8")
    assert "# deck comment" in text
    assert "# plate comment" in text
    assert "name: Commented Plate" in text


def test_resolve_config_path_prefers_kind_subdirectory():
    with tempfile.TemporaryDirectory() as d:
        configs_dir = Path(d) / "configs"
        protocol_dir = configs_dir / "protocol"
        protocol_dir.mkdir(parents=True)
        assert resolve_config_path(configs_dir, "protocol", "move.yaml") == protocol_dir / "move.yaml"


def test_safe_filename_accepts_plain_names():
    assert safe_filename("deck.yaml") == "deck.yaml"
    assert safe_filename("my-config_1.yaml") == "my-config_1.yaml"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        ".",
        "..",
        "../etc/passwd",
        "..\\..\\windows\\evil.bat",
        "sub/dir/file.yaml",
        "sub\\dir\\file.yaml",
        "/etc/passwd",
        "C:\\Windows\\evil.bat",
    ],
)
def test_safe_filename_rejects_path_components(bad):
    with pytest.raises(ValueError):
        safe_filename(bad)


def test_resolve_config_path_rejects_traversal_filename():
    with tempfile.TemporaryDirectory() as d:
        configs_dir = Path(d) / "configs"
        configs_dir.mkdir(parents=True)
        with pytest.raises(ValueError):
            resolve_config_path(configs_dir, "deck", "..\\..\\evil.yaml")


def test_read_yaml_is_safe_under_concurrent_reads(tmp_path):
    import threading

    from cubos_api.services.yaml_io import read_yaml

    files = []
    for index in range(3):
        path = tmp_path / f"config_{index}.yaml"
        body = "\n".join(
            f"key_{index}_{n}:\n  nested: value_{index}_{n}\n  items: [{n}, {n + 1}, {n + 2}]"
            for n in range(200)
        )
        path.write_text(body + "\n", encoding="utf-8")
        files.append(path)
    expected = {path: dict(read_yaml(path)) for path in files}
    failures: list[str] = []

    def worker(path):
        for _ in range(20):
            try:
                if dict(read_yaml(path)) != expected[path]:
                    failures.append(f"mismatch reading {path.name}")
            except Exception as exc:
                failures.append(f"{type(exc).__name__} reading {path.name}")

    threads = [threading.Thread(target=worker, args=(path,)) for path in files * 2]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
