import json
import os
from pathlib import Path
from typing import Any, List, Literal

from cubos.data import default_database_path
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs"
USER_SETTINGS_FILE = Path.home() / ".cubos" / "settings.json"


def _user_settings_file() -> Path:
    override = os.environ.get("CUBOS_SETTINGS_FILE")
    if override:
        return Path(override).expanduser()
    return USER_SETTINGS_FILE


def _env_config_dir_is_set() -> bool:
    return "CUBOS_CONFIG_DIR" in os.environ


def _load_user_settings() -> dict[str, Any]:
    if _env_config_dir_is_set():
        return {}
    path = _user_settings_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    config_dir = data.get("config_dir")
    if not isinstance(config_dir, str) or not config_dir:
        return {}
    return {"config_dir": Path(config_dir)}


def persist_user_settings(*, config_dir: Path) -> None:
    path = _user_settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"config_dir": str(config_dir.expanduser().resolve())}
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


class CubOSSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CUBOS_",
        extra="ignore",
        validate_assignment=True,
    )

    config_dir: Path = DEFAULT_CONFIG_DIR
    host: str = "127.0.0.1"
    port: int = 8742
    open_browser: bool = True
    data_db_path: Path = default_database_path()
    run_dir: Path = Path.home() / ".cubos" / "runs"
    api_token: SecretStr | None = None
    api_token_file: Path | None = None
    allowed_commands: List[str] = Field(default_factory=list)
    allowed_instruments: List[str] = Field(default_factory=list)
    expected_gantry_sha256: str | None = None
    expected_deck_sha256: str | None = None
    update_mode: Literal["tag", "branch"] = "tag"
    update_branch: str = "main"
    update_tag_pattern: str = r"^v\d{4}\.\d{2}\.\d{2}$"
    update_repo_dir: Path | None = None
    update_script: Path | None = None
    update_service: str = "cubos"
    # Extra Host/Origin values accepted by the Origin/Host-checking middleware,
    # on top of the configured host:port and localhost/127.0.0.1 equivalents.
    # Production should leave this empty; tests add "testserver" (the Host
    # header httpx's ASGI transport sends) via the API test fixtures.
    trusted_hosts: List[str] = Field(default_factory=list)

    def __init__(self, **data):
        if "config_dir" not in data:
            data = {**_load_user_settings(), **data}
        super().__init__(**data)
        self.ensure_config_dir()

    @property
    def configs_dir(self) -> Path:
        return self.ensure_config_dir()

    def ensure_config_dir(self) -> Path:
        path = self.config_dir.expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        self.config_dir = path
        return path

    def ensure_run_dir(self) -> Path:
        path = self.run_dir.expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        self.run_dir = path
        return path

    def resolved_api_token(self) -> SecretStr | None:
        if self.api_token is not None:
            return self.api_token
        if self.api_token_file is None:
            return None
        path = self.api_token_file.expanduser().resolve()
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError(f"API token file is empty: {path}")
        return SecretStr(token)


# Shared singleton — all routers must use this instance.
_settings = CubOSSettings()


def get_settings() -> CubOSSettings:
    return _settings
