"""Read/write YAML config files safely."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Dict, List, Optional

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

log = logging.getLogger(__name__)


def _yaml_codec() -> YAML:
    # ruamel's round-trip YAML object keeps parser state on the instance, so a
    # module-level singleton corrupts documents when request threads read
    # different files at the same time.
    codec = YAML(typ="rt")
    codec.default_flow_style = False
    codec.preserve_quotes = True
    return codec


class YamlConfigError(ValueError):
    """Raised when a config file exists but cannot be parsed as YAML."""


def read_yaml(path: Path) -> Dict[str, Any]:
    try:
        with path.open() as f:
            data = _yaml_codec().load(f)
    except YAMLError as exc:
        raise YamlConfigError(f"Invalid YAML in {path.name}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, MutableMapping):
        raise YamlConfigError(f"{path.name} is not a YAML mapping")
    return data


def write_yaml(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            _yaml_codec().dump(data, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def classify_config(data: Any) -> Optional[str]:
    """Classify a YAML config by its top-level keys."""
    if not isinstance(data, MutableMapping):
        return None
    if "labware" in data:
        return "deck"
    if "working_volume" in data:
        return "gantry"
    if "protocol" in data:
        return "protocol"
    return None


def safe_filename(filename: str) -> str:
    """Validate that ``filename`` is a bare filename with no path components.

    Raises ``ValueError`` if ``filename`` is empty, ``.``/``..``, or contains
    any path separator (including a backslash, which pathlib on POSIX treats
    as a plain character but which Windows treats as a directory separator —
    URL-encoded backslashes otherwise slip through route matching and let a
    ``configs_dir / filename`` join escape the configs directory).
    """
    if filename in ("", ".", ".."):
        raise ValueError(f"Invalid filename: {filename!r}")
    if "/" in filename or "\\" in filename:
        raise ValueError(f"Invalid filename: {filename!r}")
    if Path(filename).name != filename:
        raise ValueError(f"Invalid filename: {filename!r}")
    return filename


def resolve_config_path(configs_dir: Path, kind: str, filename: str) -> Path:
    """Return the full path for a config file, using the subdirectory if it exists."""
    filename = safe_filename(filename)
    sub = configs_dir / kind
    if sub.is_dir():
        return sub / filename
    return configs_dir / filename


def delete_config(configs_dir: Path, kind: str, filename: str) -> Path:
    """Delete a config file and return its path. Raises FileNotFoundError if absent."""
    path = resolve_config_path(configs_dir, kind, filename)
    if not path.is_file():
        raise FileNotFoundError(filename)
    path.unlink()
    return path


def list_configs(configs_dir: Path, kind: str) -> List[str]:
    """List YAML filenames for the given kind.

    Checks ``configs_dir/<kind>/`` first (CubOS's standard layout),
    then falls back to a flat scan of ``configs_dir/`` with content-based
    classification.
    """
    sub = configs_dir / kind
    if sub.is_dir():
        return sorted(p.name for p in sub.glob("*.yaml"))

    # Fallback: flat directory with content-based classification.
    results = []
    if not configs_dir.is_dir():
        return results
    for p in sorted(configs_dir.glob("*.yaml")):
        try:
            data = read_yaml(p)
            if classify_config(data) == kind:
                results.append(p.name)
        except Exception as exc:
            log.warning("Skipping unreadable config %s: %s", p, exc)
            continue
    return results
