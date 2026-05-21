from __future__ import annotations

from datetime import datetime, timedelta

import pytz

from .config import AppConfig
from .storage import read_json, write_json


IST = pytz.timezone("Asia/Kolkata")
WAKE_HOUR = 8
WAKE_MINUTE = 50


def get_sleep_until(config: AppConfig) -> datetime | None:
    state = read_json(_state_path(config), {})
    value = state.get("sleep_until") if isinstance(state, dict) else None
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = IST.localize(parsed)
    return parsed.astimezone(IST)


def sleep_until_next_wakeup(config: AppConfig, now: datetime | None = None) -> datetime:
    current = _coerce_ist(now)
    wake_at = current.replace(hour=WAKE_HOUR, minute=WAKE_MINUTE, second=0, microsecond=0)
    if current >= wake_at:
        wake_at = wake_at + timedelta(days=1)
    write_json(_state_path(config), {"sleep_until": wake_at.isoformat(timespec="seconds")})
    return wake_at


def clear_sleep(config: AppConfig) -> None:
    write_json(_state_path(config), {})


def is_sleeping(config: AppConfig, now: datetime | None = None) -> tuple[bool, datetime | None]:
    current = _coerce_ist(now)
    wake_at = get_sleep_until(config)
    if wake_at is None:
        return False, None
    if current >= wake_at:
        clear_sleep(config)
        return False, None
    return True, wake_at


def format_wake_time(value: datetime) -> str:
    local = _coerce_ist(value)
    return local.strftime("%a, %d %b at %I:%M %p").replace(" 0", " ").lstrip("0")


def _state_path(config: AppConfig):
    return config.root_dir / "data" / "bot_sleep.json"


def _coerce_ist(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(IST)
    if value.tzinfo is None:
        return IST.localize(value)
    return value.astimezone(IST)
