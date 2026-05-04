from __future__ import annotations

import argparse
import logging
import time
from typing import Any

import requests

from .config import load_config
from .notifier import TelegramNotifier
from .send_summary import build_summary
from .storage import read_json, write_json


LOGGER = logging.getLogger(__name__)

COMMANDS = {
    "/start": "help",
    "/help": "help",
    "/total": "total",
    "/last3": "last-week",
    "/attendance": "attendance",
    "/courses": "courses",
    "/marks": "marks",
    "/memo": "memo",
    "/all": "all",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Reply to Telegram commands using saved SRAAP data.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--poll-seconds", type=float, default=3)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    config = load_config(args.config)
    notifier = TelegramNotifier(config.credentials)
    offset_file = config.root_dir / "data" / "telegram_offset.json"
    offset_state = read_json(offset_file, {})
    offset = int(offset_state.get("offset", 0))

    LOGGER.info("Telegram command listener started.")
    notifier.send("<b>SRAAP bot listener started</b>\nSend /help to see commands.")

    while True:
        try:
            updates = _get_updates(config.credentials.telegram_bot_token, offset)
            for update in updates:
                offset = max(offset, int(update["update_id"]) + 1)
                _handle_update(update, config, notifier)
            write_json(offset_file, {"offset": offset})
        except requests.RequestException as exc:
            LOGGER.error("Telegram polling failed: %s", exc.__class__.__name__)
        except Exception as exc:
            LOGGER.exception("Command listener error: %s", exc)
        time.sleep(args.poll_seconds)


def _get_updates(token: str, offset: int) -> list[dict[str, Any]]:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    response = requests.get(url, params={"offset": offset, "timeout": 20}, timeout=30)
    response.raise_for_status()
    return response.json().get("result", [])


def _handle_update(update: dict[str, Any], config: Any, notifier: TelegramNotifier) -> None:
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    if chat_id != str(config.credentials.telegram_chat_id):
        LOGGER.warning("Ignoring message from unauthorized chat id: %s", chat_id)
        return

    text = str(message.get("text", "")).strip()
    command = text.split()[0].lower() if text else ""
    section = COMMANDS.get(command)

    if section is None:
        notifier.send(_help_message("Unknown command."))
        return
    if section == "help":
        notifier.send(_help_message())
        return

    state = read_json(config.resolve_path("data_file"), {})
    if not state:
        notifier.send("No saved portal data found. Run the portal monitor once first.")
        return

    notifier.send(build_summary(state, section))


def _help_message(prefix: str = "") -> str:
    intro = f"{prefix}\n\n" if prefix else ""
    return (
        intro
        + "<b>SRAAP bot commands</b>\n"
        + "/total - total attendance only\n"
        + "/last3 - last 3 attendance days\n"
        + "/attendance - full attendance summary\n"
        + "/courses - course-wise attendance\n"
        + "/marks - CIE / ETE marks\n"
        + "/memo - semester memo status\n"
        + "/all - everything saved\n"
        + "/help - show this list"
    )


if __name__ == "__main__":
    raise SystemExit(main())
