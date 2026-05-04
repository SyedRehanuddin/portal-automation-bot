from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from selenium.common.exceptions import WebDriverException

from .browser import PortalBrowser
from .config import AppConfig, load_config
from .diffing import build_change_messages
from .extractors import PortalExtractor
from .send_summary import build_summary
from .storage import read_json, write_json
from .timetable import (
    TimetableError,
    format_current_room,
    format_next,
    format_now,
    format_today,
    format_week,
    get_timetable,
)


LOGGER = logging.getLogger(__name__)
MAX_TELEGRAM_MESSAGE = 3500


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    if not _authorized(update, config):
        return

    await _reply(
        update,
        "Bot started.\n\n"
        "Ask me: attendance, marks, memo, or everything.\n"
        "Use /analyze to scan and show the menu.\n"
        "Use /check to silently alert only if something changed.\n"
        "Use /schedule to refresh timetable from website.",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    if not _authorized(update, config):
        return
    await _reply(update, _help_message())


async def attendance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_summary(update, context, "attendance")


async def marks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_summary(update, context, "marks")


async def memo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_summary(update, context, "memo")


async def total(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_summary(update, context, "total")


async def all_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_summary(update, context, "all")


async def timetable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _log_route(update, "timetable")
    await _reply_timetable(update, context, "week")


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _log_route(update, "today")
    await _reply_timetable(update, context, "today")


async def now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _log_route(update, "now")
    await _reply_timetable(update, context, "now")


async def next_class(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _log_route(update, "next")
    await _reply_timetable(update, context, "next")


async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _log_route(update, "schedule")
    config = _config(context)
    if not _authorized(update, config):
        return

    await _reply(update, "Checking timetable...")
    try:
        await _get_timetable_locked(context, config, force_refresh=True)
    except TimetableError:
        await _reply(update, "Timetable not available")
        return
    except Exception as exc:
        LOGGER.exception("Schedule refresh failed: %s", exc)
        await _reply(update, "Timetable not available")
        return

    await _reply(
        update,
        "Timetable updated.\n\n"
        "/timetable - full weekly timetable\n"
        "/today - today's schedule\n"
        "/now - current class\n"
        "/next - next class",
    )


async def natural_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    if not _authorized(update, config):
        return

    text = (update.effective_message.text if update.effective_message else "").lower()
    stripped = text.strip()
    if stripped in {"start", "bot start"}:
        await start(update, context)
        return
    if stripped in {"analyze", "analyse"}:
        await analyze(update, context)
        return
    if stripped in {"check", "check now", "refresh"}:
        await check_now(update, context)
        return
    if stripped in {"schedule", "refresh schedule", "update schedule"}:
        await schedule(update, context)
        return

    timetable_intent = _timetable_intent_from_text(text)
    if timetable_intent is not None:
        await _reply_timetable(update, context, timetable_intent)
        return

    section = _section_from_text(text)
    if section is None:
        await _reply(update, "Tell me what you need: attendance, marks, memo, or everything.")
        return

    await _reply_summary(update, context, section)


async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    if not _authorized(update, config):
        return

    try:
        messages, memo_pdf = await _run_check_locked(context, config, compare=True)
    except Exception as exc:
        LOGGER.exception("Manual check failed: %s", exc)
        await _reply(update, f"Portal check failed: {_escape_text(_short_error(exc))}")
        return
    if messages:
        await _send_messages(context, config.credentials.telegram_chat_id, messages)
        if memo_pdf:
            await _send_document(context, config.credentials.telegram_chat_id, memo_pdf, "New semester memo PDF")


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    if not _authorized(update, config):
        return

    await _reply(update, "Checking everything... ⏳")
    try:
        await _run_check_locked(context, config, compare=False)
    except Exception as exc:
        LOGGER.exception("Analyze failed: %s", exc)
        await _reply(update, f"Portal check failed: {_escape_text(_short_error(exc))}")
        return

    await _reply(update, "Checked everything ✅")
    await _reply(update, _menu_message())


async def monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    try:
        messages, memo_pdf = await _run_check_locked(context, config, compare=True)
    except Exception as exc:
        LOGGER.exception("Scheduled monitor failed: %s", exc)
        await _send_messages(
            context,
            config.credentials.telegram_chat_id,
            [f"<b>SRAAP monitor error</b>\n{_escape_text(_short_error(exc))}"],
        )
        return

    if messages:
        await _send_messages(context, config.credentials.telegram_chat_id, messages)
        if memo_pdf:
            await _send_document(context, config.credentials.telegram_chat_id, memo_pdf, "New semester memo PDF")


def run_portal_check(config: AppConfig, compare: bool = True) -> tuple[list[str], Path | None]:
    state_file = config.resolve_path("data_file")
    old_state = read_json(state_file, {})

    with PortalBrowser(config) as browser:
        extractor = PortalExtractor(browser)
        new_data = extractor.collect_all()

    new_state = {
        **new_data,
    }

    if not compare:
        write_json(state_file, new_state)
        return [], None

    if not old_state:
        write_json(state_file, new_state)
        return [], None

    if _memo_target_changed(old_state, new_state):
        write_json(state_file, new_state)
        return [], None

    messages = build_change_messages(old_state, new_state)
    write_json(state_file, new_state)
    return messages, _memo_pdf_path(new_state) if messages else None


async def _run_check_locked(
    context: ContextTypes.DEFAULT_TYPE,
    config: AppConfig,
    compare: bool = True,
) -> tuple[list[str], Path | None]:
    lock = context.application.bot_data.get("check_lock")
    if lock is None:
        lock = asyncio.Lock()
        context.application.bot_data["check_lock"] = lock

    async with lock:
        return await asyncio.to_thread(run_portal_check, config, compare)


async def _reply_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, section: str) -> None:
    config = _config(context)
    if not _authorized(update, config):
        return

    state = read_json(config.resolve_path("data_file"), {})
    if not state:
        await _reply(update, "No saved data yet. Checking portal now...")
        try:
            await _run_check_locked(context, config, compare=False)
        except Exception as exc:
            LOGGER.exception("Auto check for missing data failed: %s", exc)
            await _reply(update, f"Portal check failed: {_escape_text(_short_error(exc))}")
            return
        state = read_json(config.resolve_path("data_file"), {})
        if not state:
            await _reply(update, "I could not fetch portal data yet. Try /check again.")
            return

    await _reply(update, build_assistant_response(state, section))


async def _reply_timetable(update: Update, context: ContextTypes.DEFAULT_TYPE, intent: str) -> None:
    config = _config(context)
    if not _authorized(update, config):
        return

    try:
        timetable_data = await _get_timetable_locked(context, config, force_refresh=False)
    except TimetableError:
        await _reply(update, "Timetable not available")
        return
    except Exception as exc:
        LOGGER.exception("Timetable request failed: %s", exc)
        await _reply(update, "Timetable not available")
        return

    if intent == "week":
        await _reply(update, _route_debug(update, "timetable", intent) + "\n\n" + format_week(timetable_data))
    elif intent == "today":
        await _reply(update, _route_debug(update, "today", intent) + "\n\n" + format_today(timetable_data))
    elif intent == "now":
        await _reply(update, _route_debug(update, "now", intent) + "\n\n" + format_now(timetable_data))
    elif intent == "next":
        await _reply(update, _route_debug(update, "next", intent) + "\n\n" + format_next(timetable_data))
    elif intent == "room":
        await _reply(update, format_current_room(timetable_data))


async def _get_timetable_locked(
    context: ContextTypes.DEFAULT_TYPE,
    config: AppConfig,
    force_refresh: bool = False,
) -> dict[str, dict[str, dict[str, str]]]:
    lock = context.application.bot_data.get("timetable_lock")
    if lock is None:
        lock = asyncio.Lock()
        context.application.bot_data["timetable_lock"] = lock

    async with lock:
        return await asyncio.to_thread(get_timetable, config, force_refresh)


def build_assistant_response(state: dict[str, Any], section: str) -> str:
    if section == "total":
        attendance = state.get("attendance") or {}
        return f"Attendance: {_escape_text(str(attendance.get('overall_percent', '-')))}%"

    if section == "memo":
        memo = state.get("memo") or {}
        status = memo.get("status") or ("Released" if memo.get("available") else "Not released yet")
        return f"Memo: {_escape_text(str(status))}"

    return build_summary(state, section)


async def _reply(update: Update, text: str) -> None:
    if update.effective_message is None:
        return
    for chunk in _chunks(text):
        await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def _send_messages(context: ContextTypes.DEFAULT_TYPE, chat_id: str, messages: list[str]) -> None:
    for message in messages:
        for chunk in _chunks(message):
            await context.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )


async def _send_document(context: ContextTypes.DEFAULT_TYPE, chat_id: str, path: Path, caption: str) -> None:
    if not path.exists():
        return
    with path.open("rb") as document:
        await context.bot.send_document(chat_id=chat_id, document=document, caption=caption)


def _authorized(update: Update, config: AppConfig) -> bool:
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    if chat_id == str(config.credentials.telegram_chat_id):
        return True
    LOGGER.warning("Ignoring unauthorized Telegram chat id: %s", chat_id)
    return False


def _help_message() -> str:
    return "<b>SRAAP bot commands</b>\n" + _menu_message() + "\n/help - show this menu\n\n" + (
        "You can also type: start, analyze, check, attendance, marks, memo, all, everything."
    )


def _menu_message() -> str:
    return (
        "/analyze - scan portal and save latest data\n"
        "/check - silently check and alert only if data changed\n"
        "/attendance - attendance summary\n"
        "/marks - CIE / ETE marks\n"
        "/memo - semester memo status\n"
        "/total - total attendance only\n"
        "/all - all saved data\n"
        "/schedule - refresh timetable from website\n"
        "/timetable - full weekly timetable\n"
        "/today - today's schedule\n"
        "/now - current class\n"
        "/next - next class"
    )


def _section_from_text(text: str) -> str | None:
    if "all" in text or "everything" in text:
        return "all"
    if "attendance" in text or "attendence" in text or "total atten" in text:
        return "attendance"
    if "marks" in text or "cie" in text or "ete" in text:
        return "marks"
    if "memo" in text or "result" in text:
        return "memo"
    return None


def _timetable_intent_from_text(text: str) -> str | None:
    stripped = text.strip()
    if "next class" in text or stripped == "next":
        return "next"
    if stripped == "now" or "current class" in text:
        return "now"
    if "today schedule" in text or stripped == "today":
        return "today"
    if "timetable" in text or "time table" in text:
        return "week"
    if "room" in text or "where" in text:
        return "room"
    return None


def _config(context: ContextTypes.DEFAULT_TYPE) -> AppConfig:
    config = context.application.bot_data.get("config")
    if not isinstance(config, AppConfig):
        raise RuntimeError("Bot config was not initialized.")
    return config


def _memo_pdf_path(state: dict[str, Any]) -> Path | None:
    memo = state.get("memo") or {}
    downloaded_file = memo.get("downloaded_file")
    return Path(downloaded_file) if downloaded_file else None


def _memo_target_changed(old_state: dict[str, Any], new_state: dict[str, Any]) -> bool:
    old_memo = old_state.get("memo") or {}
    new_memo = new_state.get("memo") or {}
    return bool(new_memo.get("target")) and old_memo.get("target") != new_memo.get("target")


def _chunks(text: str) -> list[str]:
    return [text[index : index + MAX_TELEGRAM_MESSAGE] for index in range(0, len(text), MAX_TELEGRAM_MESSAGE)]


def _escape_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _short_error(exc: BaseException) -> str:
    text = str(exc).splitlines()[0].strip()
    if not text:
        text = exc.__class__.__name__
    if isinstance(exc, WebDriverException) and "invalid session id" in str(exc).lower():
        return "Selenium browser session expired. Run /check again."
    return text[:300]


def build_application(config: AppConfig) -> Application:
    application = Application.builder().token(config.credentials.telegram_bot_token).build()
    application.bot_data["config"] = config

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("attendance", attendance))
    application.add_handler(CommandHandler("marks", marks))
    application.add_handler(CommandHandler("memo", memo))
    application.add_handler(CommandHandler("total", total))
    application.add_handler(CommandHandler("all", all_data))
    application.add_handler(CommandHandler("check", check_now))
    application.add_handler(CommandHandler("analyze", analyze))
    application.add_handler(CommandHandler("schedule", schedule))
    application.add_handler(CommandHandler("timetable", timetable))
    application.add_handler(CommandHandler("today", today))
    application.add_handler(CommandHandler("now", now))
    application.add_handler(CommandHandler("next", next_class))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, natural_message))

    if _background_monitor_enabled(config):
        interval_minutes = float(config.monitoring.get("check_interval_minutes", 12))
        application.job_queue.run_repeating(
            monitor_job,
            interval=max(60, int(interval_minutes * 60)),
            first=10,
            name="sraap_monitor",
        )
    return application


def _background_monitor_enabled(config: AppConfig) -> bool:
    env_value = os.getenv("ENABLE_BACKGROUND_MONITOR", "").strip().lower()
    if env_value:
        return env_value in {"1", "true", "yes", "on"}
    return bool(config.monitoring.get("background_enabled", False))


def _log_route(update: Update, handler: str) -> None:
    message = update.effective_message
    text = message.text if message else ""
    LOGGER.warning(
        "COMMAND_ROUTE handler=%s text=%r chat_id=%s update_id=%s",
        handler,
        text,
        update.effective_chat.id if update.effective_chat else None,
        update.update_id,
    )


def _route_debug(update: Update, handler: str, intent: str) -> str:
    message = update.effective_message
    text = message.text if message else ""
    return (
        "ROUTE_DEBUG:\n"
        f"handler={handler}\n"
        f"intent={intent}\n"
        f"text={_escape_text(text)}\n"
        f"update_id={update.update_id}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SRAAP Telegram bot with Selenium monitoring.")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    config = load_config(args.config)
    app = build_application(config)
    LOGGER.info("Starting SRAAP Telegram bot.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
