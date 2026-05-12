from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any

import pytz
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from selenium.common.exceptions import WebDriverException

from .browser import PortalBrowser, validate_cookie_session
from .config import AppConfig, load_config
from .diffing import build_change_messages
from .extractors import PortalExtractor
from .requests_extractor import RequestsPortalExtractor
from .send_summary import build_summary
from .storage import read_json, write_json
from .timetable import (
    TimetableError,
    format_current_room,
    format_next,
    format_now,
    format_today,
    format_week,
    get_cached_timetable,
    get_timetable,
)


LOGGER = logging.getLogger(__name__)
MAX_TELEGRAM_MESSAGE = 3500
IST = pytz.timezone("Asia/Kolkata")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    if not _authorized(update, config):
        return

    await _reply(
        update,
        _dashboard_message(),
        reply_markup=_dashboard_keyboard(),
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


async def total(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_summary(update, context, "total")


async def all_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_summary(update, context, "all")


async def captcha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    if not _authorized(update, config):
        return

    code = " ".join(context.args).strip()
    if not code:
        await _reply(update, "Send it like: /captcha ABCD")
        return

    request = context.application.bot_data.get("captcha_request")
    if not isinstance(request, dict):
        await _reply(update, "No CAPTCHA is waiting right now. Run /check or /analyze first.")
        return

    request["answer"] = code
    queue = request.get("queue")
    if isinstance(queue, Queue):
        try:
            queue.put_nowait(code)
        except Exception:
            LOGGER.info("CAPTCHA answer was stored but could not be queued immediately.")

    attempt = request.get("attempt")
    max_attempts = request.get("max_attempts")
    await _reply(update, f"CAPTCHA received for attempt {attempt}/{max_attempts}. Continuing portal login...")


async def timetable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_timetable(update, context, "week")


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_timetable(update, context, "today")


async def now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_timetable(update, context, "now")


async def next_class(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_timetable(update, context, "next")


async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        reply_markup=_timetable_keyboard(),
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
        await _reply(update, "Tell me what you need: attendance, marks, or everything.")
        return

    await _reply_summary(update, context, section)


async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    if not _authorized(update, config):
        return

    await _reply(update, "Checking for changes...")
    try:
        messages, _ = await _run_check_locked(context, config, compare=True)
    except Exception as exc:
        LOGGER.exception("Manual check failed: %s", exc)
        await _reply(update, f"Portal check failed: {_escape_text(_short_error(exc))}")
        return
    if messages:
        await _send_messages(context, config.credentials.telegram_chat_id, messages)
        return

    await _reply(update, "No changes found.")


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
    await _reply(update, _menu_message(), reply_markup=_dashboard_keyboard())


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    if not _authorized(update, config):
        return

    query = update.callback_query
    if query is None:
        return
    await query.answer()

    action = (query.data or "").removeprefix("ui:")
    if action == "menu":
        await _reply(update, _dashboard_message(), reply_markup=_dashboard_keyboard())
    elif action == "attendance":
        await _reply_summary(update, context, "attendance")
    elif action == "total":
        await _reply_summary(update, context, "total")
    elif action == "marks":
        await _reply_summary(update, context, "marks")
    elif action == "all":
        await _reply_summary(update, context, "all")
    elif action == "today":
        await _reply_timetable(update, context, "today")
    elif action == "now":
        await _reply_timetable(update, context, "now")
    elif action == "next":
        await _reply_timetable(update, context, "next")
    elif action == "week":
        await _reply_timetable(update, context, "week")
    elif action == "check":
        await check_now(update, context)
    elif action == "analyze":
        await analyze(update, context)
    elif action == "schedule":
        await schedule(update, context)
    else:
        await _reply(update, "Unknown button. Use /start to open the dashboard.")


async def monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    try:
        messages, _ = await _run_check_locked(context, config, compare=True)
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


def run_portal_check(
    config: AppConfig,
    compare: bool = True,
    captcha_handler: Any = None,
) -> tuple[list[str], Path | None]:
    state_file = config.resolve_path("data_file")
    old_state = read_json(state_file, {})

    new_data = _collect_portal_data(config, captcha_handler)

    new_state = {
        "last_updated_at": datetime.now(IST).isoformat(timespec="seconds"),
        **new_data,
    }

    if not compare:
        write_json(state_file, new_state)
        return [], None

    if not old_state:
        write_json(state_file, new_state)
        return [], None

    messages = build_change_messages(old_state, new_state)
    write_json(state_file, new_state)
    return messages, None


def _collect_portal_data(config: AppConfig, captcha_handler: Any = None) -> dict[str, Any]:
    if not config.raw.get("requests_extraction", {}).get("enabled", True):
        LOGGER.info("Requests portal extraction is disabled; using Selenium.")
        with PortalBrowser(config, captcha_handler=captcha_handler) as browser:
            extractor = PortalExtractor(browser)
            return extractor.collect_all()

    validation = validate_cookie_session(config)
    if validation is True:
        try:
            LOGGER.info("Collecting portal data with requests.")
            return RequestsPortalExtractor(config).collect_all()
        except Exception as exc:
            LOGGER.info("Requests portal extraction failed; falling back to Selenium: %s", exc)
    elif validation is False:
        LOGGER.info("Requests validation says portal cookies are expired; using Selenium/CAPTCHA flow.")
    else:
        LOGGER.info("Requests validation was inconclusive; using Selenium fallback.")

    with PortalBrowser(config, captcha_handler=captcha_handler) as browser:
        extractor = PortalExtractor(browser)
        return extractor.collect_all()


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
        captcha_handler = _build_captcha_handler(context, config)
        return await asyncio.to_thread(run_portal_check, config, compare, captcha_handler)


async def _reply_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, section: str) -> None:
    config = _config(context)
    if not _authorized(update, config):
        return

    state = read_json(config.resolve_path("data_file"), {})
    if not state:
        await _reply(update, "No saved data yet. Run /analyze to fetch the latest portal data first.")
        return

    await _reply(update, build_assistant_response(state, section), reply_markup=_portal_keyboard())


async def _reply_timetable(update: Update, context: ContextTypes.DEFAULT_TYPE, intent: str) -> None:
    config = _config(context)
    if not _authorized(update, config):
        return

    try:
        timetable_data = get_cached_timetable(config)
        if not timetable_data:
            await _reply(update, "Timetable not available right now.")
            return
    except TimetableError:
        await _reply(update, "Timetable not available")
        return
    except Exception as exc:
        LOGGER.exception("Timetable request failed: %s", exc)
        await _reply(update, "Timetable not available")
        return

    if intent == "week":
        await _reply(update, format_week(timetable_data), reply_markup=_timetable_keyboard())
    elif intent == "today":
        await _reply(update, format_today(timetable_data), reply_markup=_timetable_keyboard())
    elif intent == "now":
        await _reply(update, format_now(timetable_data), reply_markup=_timetable_keyboard())
    elif intent == "next":
        await _reply(update, format_next(timetable_data), reply_markup=_timetable_keyboard())
    elif intent == "room":
        await _reply(update, format_current_room(timetable_data), reply_markup=_timetable_keyboard())


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
        return (
            f"Attendance: {_escape_text(str(attendance.get('overall_percent', '-')))}%\n"
            f"{_format_last_updated(state)}"
        )

    return build_summary(state, section)


def _format_last_updated(state: dict[str, Any]) -> str:
    value = state.get("last_updated_at") or state.get("last_checked_at")
    if not value:
        return "Last updated: -"
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = IST.localize(parsed)
        parsed = parsed.astimezone(IST)
        formatted = parsed.strftime("%I:%M %p").lstrip("0")
    except ValueError:
        formatted = str(value)
    return f"Last updated: {_escape_text(formatted)}"


def _build_captcha_handler(context: ContextTypes.DEFAULT_TYPE, config: AppConfig) -> Any:
    loop = asyncio.get_running_loop()
    chat_id = config.credentials.telegram_chat_id

    def handle_captcha(browser: PortalBrowser, attempt: int = 1, max_attempts: int = 1) -> str:
        screenshot_path = browser.save_login_screenshot(config.root_dir / "data" / "captcha_login.png")
        queue: Queue[str] = Queue(maxsize=1)
        request: dict[str, Any] = {
            "queue": queue,
            "answer": None,
            "attempt": attempt,
            "max_attempts": max_attempts,
        }

        async def publish_request() -> None:
            context.application.bot_data["captcha_request"] = request
            with screenshot_path.open("rb") as image:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=image,
                    caption=(
                        f"Portal login CAPTCHA required ({attempt}/{max_attempts}). "
                        "Reply with /captcha CODE within 5 minutes."
                    ),
                )

        asyncio.run_coroutine_threadsafe(publish_request(), loop).result(timeout=30)

        timeout = int(config.browser.get("manual_captcha_timeout_seconds", 300))
        end_at = loop.time() + timeout
        try:
            while loop.time() < end_at:
                answer = request.get("answer")
                if isinstance(answer, str) and answer.strip():
                    asyncio.run_coroutine_threadsafe(_clear_captcha_request(context), loop).result(timeout=10)
                    return answer
                try:
                    answer = queue.get(timeout=1)
                    asyncio.run_coroutine_threadsafe(_clear_captcha_request(context), loop).result(timeout=10)
                    return answer
                except Empty:
                    continue
        except Empty as exc:
            asyncio.run_coroutine_threadsafe(_clear_captcha_request(context), loop).result(timeout=10)
            raise TimeoutError("CAPTCHA timed out. Run /check or /analyze again.") from exc
        asyncio.run_coroutine_threadsafe(_clear_captcha_request(context), loop).result(timeout=10)
        raise TimeoutError("CAPTCHA timed out. Run /check or /analyze again.")

    return handle_captcha


async def _clear_captcha_request(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.application.bot_data.pop("captcha_request", None)


async def _reply(update: Update, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if update.effective_message is None:
        return
    for chunk in _chunks(text):
        await update.effective_message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
        reply_markup = None


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
        "You can also type: start, analyze, check, attendance, marks, all, everything."
    )


def _dashboard_message() -> str:
    return (
        "<b>Portler Dashboard</b>\n"
        "Tap a button or type a command.\n\n"
        "Portal data buttons use saved data unless you tap Analyze or Check.\n"
        "Timetable buttons use the latest saved timetable."
    )


def _dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Attendance", callback_data="ui:attendance"),
                InlineKeyboardButton("Total", callback_data="ui:total"),
            ],
            [
                InlineKeyboardButton("Marks", callback_data="ui:marks"),
                InlineKeyboardButton("All Data", callback_data="ui:all"),
            ],
            [
                InlineKeyboardButton("Today", callback_data="ui:today"),
                InlineKeyboardButton("Now", callback_data="ui:now"),
                InlineKeyboardButton("Next", callback_data="ui:next"),
            ],
            [
                InlineKeyboardButton("Full Week", callback_data="ui:week"),
            ],
            [
                InlineKeyboardButton("Analyze", callback_data="ui:analyze"),
                InlineKeyboardButton("Check", callback_data="ui:check"),
            ],
            [
                InlineKeyboardButton("Refresh Timetable", callback_data="ui:schedule"),
            ],
        ]
    )


def _portal_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Attendance", callback_data="ui:attendance"),
                InlineKeyboardButton("Total", callback_data="ui:total"),
            ],
            [
                InlineKeyboardButton("Marks", callback_data="ui:marks"),
                InlineKeyboardButton("All Data", callback_data="ui:all"),
            ],
            [
                InlineKeyboardButton("Analyze", callback_data="ui:analyze"),
                InlineKeyboardButton("Check", callback_data="ui:check"),
            ],
            [
                InlineKeyboardButton("Menu", callback_data="ui:menu"),
            ],
        ]
    )


def _timetable_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Today", callback_data="ui:today"),
                InlineKeyboardButton("Now", callback_data="ui:now"),
                InlineKeyboardButton("Next", callback_data="ui:next"),
            ],
            [
                InlineKeyboardButton("Full Week", callback_data="ui:week"),
                InlineKeyboardButton("Menu", callback_data="ui:menu"),
            ],
        ]
    )


def _menu_message() -> str:
    return (
        "/analyze - scan portal and save latest data\n"
        "/check - silently check and alert only if data changed\n"
        "/attendance - attendance summary\n"
        "/marks - CIE / ETE marks\n"
        "/total - total attendance only\n"
        "/all - all saved data\n"
        "/schedule - refresh timetable from website\n"
        "/timetable - full weekly timetable\n"
        "/today - today's schedule\n"
        "/now - current class\n"
        "/next - next class\n"
        "/captcha CODE - answer portal CAPTCHA when asked"
    )


def _section_from_text(text: str) -> str | None:
    if "all" in text or "everything" in text:
        return "all"
    if "attendance" in text or "attendence" in text or "total atten" in text:
        return "attendance"
    if "marks" in text or "cie" in text or "ete" in text:
        return "marks"
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
    application = Application.builder().token(config.credentials.telegram_bot_token).concurrent_updates(True).build()
    application.bot_data["config"] = config

    application.add_handler(CallbackQueryHandler(button_handler, pattern=r"^ui:"))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("attendance", attendance))
    application.add_handler(CommandHandler("marks", marks))
    application.add_handler(CommandHandler("total", total))
    application.add_handler(CommandHandler("all", all_data))
    application.add_handler(CommandHandler("captcha", captcha))
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


async def setup_bot_commands(application: Application) -> None:
    try:
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Open dashboard"),
                BotCommand("analyze", "Refresh portal data"),
                BotCommand("check", "Check and report changes"),
                BotCommand("attendance", "Attendance summary"),
                BotCommand("total", "Total attendance"),
                BotCommand("marks", "CIE / ETE marks"),
                BotCommand("all", "All saved data"),
                BotCommand("schedule", "Refresh timetable"),
                BotCommand("today", "Today's schedule"),
                BotCommand("now", "Current class"),
                BotCommand("next", "Next class"),
                BotCommand("timetable", "Full weekly timetable"),
            ]
        )
    except Exception:
        LOGGER.exception("Failed to update Telegram command menu.")


def _background_monitor_enabled(config: AppConfig) -> bool:
    env_value = os.getenv("ENABLE_BACKGROUND_MONITOR", "").strip().lower()
    if env_value:
        return env_value in {"1", "true", "yes", "on"}
    return bool(config.monitoring.get("background_enabled", False))


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
