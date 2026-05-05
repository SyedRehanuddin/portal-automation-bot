from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


SUPABASE_TABLE = os.getenv("SUPABASE_TABLE", "bot_state")


def read_json(path: Path, default: Any) -> Any:
    remote = _read_remote(path)
    if remote is not None:
        return remote

    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except json.JSONDecodeError:
        backup = path.with_suffix(path.suffix + ".corrupt")
        path.replace(backup)
        return default


def write_json(path: Path, data: Any) -> None:
    if _write_remote(path, data):
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False, sort_keys=True)
    temp_path.replace(path)


def _storage_key(path: Path) -> str:
    return str(path).replace("\\", "/")


def _supabase_headers() -> dict[str, str] | None:
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip() or os.getenv("SUPABASE_ANON_KEY", "").strip()
    if not url or not key:
        return None
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _supabase_endpoint() -> str | None:
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    if not url:
        return None
    return f"{url}/rest/v1/{SUPABASE_TABLE}"


def _remote_enabled() -> bool:
    return os.getenv("STORAGE_BACKEND", "").strip().lower() == "supabase"


def _read_remote(path: Path) -> Any | None:
    if not _remote_enabled():
        return None
    endpoint = _supabase_endpoint()
    headers = _supabase_headers()
    if endpoint is None or headers is None:
        return None

    response = requests.get(
        endpoint,
        headers=headers,
        params={"key": f"eq.{_storage_key(path)}", "select": "value"},
        timeout=15,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    rows = response.json()
    if not rows:
        return None
    return rows[0].get("value")


def _write_remote(path: Path, data: Any) -> bool:
    if not _remote_enabled():
        return False
    endpoint = _supabase_endpoint()
    headers = _supabase_headers()
    if endpoint is None or headers is None:
        return False

    response = requests.post(
        endpoint,
        headers={**headers, "Prefer": "resolution=merge-duplicates"},
        json={"key": _storage_key(path), "value": data, "updated_at": datetime.now(timezone.utc).isoformat()},
        timeout=15,
    )
    response.raise_for_status()
    return True
