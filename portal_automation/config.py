from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when the local configuration is missing or invalid."""


@dataclass(frozen=True)
class Credentials:
    enrollment_number: str
    password: str
    telegram_bot_token: str
    telegram_chat_id: str


@dataclass(frozen=True)
class AppConfig:
    root_dir: Path
    config_path: Path
    raw: dict[str, Any]
    credentials: Credentials

    @property
    def portal(self) -> dict[str, Any]:
        return self.raw["portal"]

    @property
    def selectors(self) -> dict[str, Any]:
        return self.raw["selectors"]

    @property
    def browser(self) -> dict[str, Any]:
        return self.raw["browser"]

    @property
    def monitoring(self) -> dict[str, Any]:
        return self.raw["monitoring"]

    def resolve_path(self, key: str) -> Path:
        value = self.monitoring[key]
        path = Path(value)
        if path.is_absolute():
            return path
        return self.root_dir / path


def load_config(config_path: str | Path) -> AppConfig:
    path = Path(config_path).resolve()
    root_dir = path.parent

    env_path = root_dir / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv()

    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        raw = json.load(file)

    _require_sections(raw, ["portal", "selectors", "browser", "monitoring"])
    _require_sections(raw["portal"], ["base_url", "login_url", "attendance_url", "marks_url"])
    _require_sections(raw["selectors"], ["login", "attendance", "marks"])

    credentials = Credentials(
        enrollment_number=_required_env("ENROLLMENT_NUMBER"),
        password=_required_env("PORTAL_PASSWORD"),
        telegram_bot_token=_required_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_required_env("TELEGRAM_CHAT_ID"),
    )

    return AppConfig(root_dir=root_dir, config_path=path, raw=raw, credentials=credentials)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _require_sections(data: dict[str, Any], names: list[str]) -> None:
    missing = [name for name in names if name not in data]
    if missing:
        raise ConfigError(f"Missing config keys: {', '.join(missing)}")
