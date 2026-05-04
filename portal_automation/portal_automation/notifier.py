from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import requests

from .config import Credentials


LOGGER = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, credentials: Credentials, timeout_seconds: int = 20) -> None:
        self.bot_token = credentials.telegram_bot_token
        self.chat_id = credentials.telegram_chat_id
        self.timeout_seconds = timeout_seconds

    def send(self, text: str) -> bool:
        if self._is_placeholder_config():
            LOGGER.warning("Telegram is not configured yet; skipping notification.")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            response = requests.post(url, json=payload, timeout=self.timeout_seconds)
            response.raise_for_status()
            return True
        except requests.RequestException as exc:
            LOGGER.error("Telegram notification failed: %s", _safe_error(exc))
            return False

    def send_many(self, messages: Iterable[str]) -> None:
        for message in messages:
            self.send(message)

    def send_document(self, path: Path, caption: str = "") -> bool:
        if self._is_placeholder_config():
            LOGGER.warning("Telegram is not configured yet; skipping document notification.")
            return False
        if not path.exists():
            LOGGER.error("Telegram document does not exist: %s", path)
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendDocument"
        payload = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"}
        try:
            with path.open("rb") as document:
                response = requests.post(
                    url,
                    data=payload,
                    files={"document": (path.name, document, "application/pdf")},
                    timeout=self.timeout_seconds,
                )
            response.raise_for_status()
            return True
        except requests.RequestException as exc:
            LOGGER.error("Telegram document notification failed: %s", _safe_error(exc))
            return False

    def _is_placeholder_config(self) -> bool:
        return (
            self.bot_token.startswith("your_")
            or self.chat_id.startswith("your_")
            or self.bot_token == "123456789:telegram_bot_token"
            or self.chat_id == "123456789"
        )


def _safe_error(exc: requests.RequestException) -> str:
    response = getattr(exc, "response", None)
    if response is not None:
        return f"HTTP {response.status_code}: {response.text[:200]}"
    return exc.__class__.__name__
