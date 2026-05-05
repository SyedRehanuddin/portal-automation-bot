from __future__ import annotations

import argparse
import logging
import random
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from selenium.common.exceptions import WebDriverException
import pytz

from .browser import PortalBrowser
from .config import ConfigError, load_config
from .diffing import build_change_messages
from .extractors import PortalExtractor
from .notifier import TelegramNotifier
from .storage import read_json, write_json


LOGGER = logging.getLogger(__name__)
STOP_REQUESTED = False


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor university portal attendance, marks, and memo updates.")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--once", action="store_true", help="Run one check and exit")
    parser.add_argument("--notify-first-run", action="store_true", help="Send Telegram summary even when no previous state exists")
    args = parser.parse_args()

    setup_logging()
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        LOGGER.error("%s", exc)
        return 2

    notifier = TelegramNotifier(config.credentials)
    state_file = config.resolve_path("data_file")
    interval_minutes = float(config.monitoring.get("check_interval_minutes", 12))
    interval_seconds = max(60, int(interval_minutes * 60))

    with PortalBrowser(config) as browser:
        extractor = PortalExtractor(browser)
        while not STOP_REQUESTED:
            run_check(extractor, notifier, state_file, notify_first_run=args.notify_first_run)
            if args.once:
                break
            sleep_with_jitter(interval_seconds)

    return 0


def run_check(extractor: PortalExtractor, notifier: TelegramNotifier, state_file: Any, notify_first_run: bool = False) -> None:
    LOGGER.info("Starting portal check at %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    old_state = read_json(state_file, {})

    try:
        new_data = extractor.collect_all()
    except (TimeoutError, RuntimeError, WebDriverException) as exc:
        LOGGER.exception("Portal check failed: %s", exc)
        notifier.send(f"<b>Portal automation error</b>\n{_short_error(exc)}")
        return
    except Exception as exc:
        LOGGER.exception("Unexpected portal check failure: %s", exc)
        notifier.send(f"<b>Portal automation error</b>\nUnexpected error: {_short_error(exc)}")
        return

    new_state = {
        "last_checked_at": datetime.now().isoformat(timespec="seconds"),
        "last_updated_at": datetime.now(pytz.timezone("Asia/Kolkata")).isoformat(timespec="seconds"),
        **new_data,
    }

    if not old_state:
        write_json(state_file, new_state)
        LOGGER.info("Initial state saved to %s", state_file)
        if notify_first_run:
            notifier.send("<b>Portal automation started</b>\nInitial data saved. Future messages will only show changes.")
        return

    messages = build_change_messages(old_state, new_state)
    if _memo_target_changed(old_state, new_state):
        write_json(state_file, new_state)
        LOGGER.info("Memo target changed; saved new baseline without notification.")
        return

    write_json(state_file, new_state)

    if messages:
        LOGGER.info("Detected %d change(s). Sending Telegram notifications.", len(messages))
        notifier.send_many(messages)
        _send_memo_pdf_if_available(notifier, new_state)
    else:
        LOGGER.info("No changes detected.")


def sleep_with_jitter(interval_seconds: int) -> None:
    jitter = random.randint(-60, 60)
    sleep_for = max(60, interval_seconds + jitter)
    LOGGER.info("Sleeping for %d seconds.", sleep_for)
    end_at = time.time() + sleep_for
    while not STOP_REQUESTED and time.time() < end_at:
        time.sleep(min(5, end_at - time.time()))


def _memo_target_changed(old_state: dict[str, Any], new_state: dict[str, Any]) -> bool:
    old_memo = old_state.get("memo") or {}
    new_memo = new_state.get("memo") or {}
    return bool(new_memo.get("target")) and old_memo.get("target") != new_memo.get("target")


def _short_error(exc: BaseException) -> str:
    text = str(exc).splitlines()[0].strip()
    return text or exc.__class__.__name__


def _send_memo_pdf_if_available(notifier: TelegramNotifier, state: dict[str, Any]) -> None:
    memo = state.get("memo") or {}
    downloaded_file = memo.get("downloaded_file")
    if not downloaded_file:
        return
    caption = "<b>New semester memo PDF</b>"
    notifier.send_document(Path(downloaded_file), caption=caption)


def request_stop(signum: int, frame: Any) -> None:
    del signum, frame
    global STOP_REQUESTED
    STOP_REQUESTED = True
    LOGGER.info("Stop requested. Exiting after current step.")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


if __name__ == "__main__":
    raise SystemExit(main())
