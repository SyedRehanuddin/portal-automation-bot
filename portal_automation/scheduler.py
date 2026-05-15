from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any

import pytz
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.ext import Application

from .config import AppConfig
from .storage import read_json
from .timetable import (
    format_block_start,
    format_display_range,
    format_today,
    get_cached_timetable,
    get_current_class,
    get_next_class,
    get_timetable,
)


LOGGER = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")
TIMETABLE_REFRESH_TIME = "08:50"
FIRST_CLASS_REMINDER_TIME = "08:55"
CLASS_CONTEXT_TIMES = ["09:55", "10:55", "11:55", "12:55", "13:55", "14:55", "15:55"]
ATTENDANCE_UPDATE_TIMES = ["10:05", "11:05", "12:05", "13:05", "14:05", "15:05", "16:05", "17:05"]


def start_scheduler(application: Application, config: AppConfig) -> AsyncIOScheduler | None:
    if not _scheduler_enabled():
        LOGGER.info("Timetable background scheduler is disabled.")
        return None

    scheduler = AsyncIOScheduler(timezone=IST)
    refresh_hour, refresh_minute = _parse_time(TIMETABLE_REFRESH_TIME)
    scheduler.add_job(
        refresh_and_send_daily_schedule,
        CronTrigger(hour=refresh_hour, minute=refresh_minute, timezone=IST),
        args=[application, config],
        id="daily_timetable_refresh",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    first_hour, first_minute = _parse_time(FIRST_CLASS_REMINDER_TIME)
    scheduler.add_job(
        send_class_update,
        CronTrigger(hour=first_hour, minute=first_minute, timezone=IST),
        args=[application, config, FIRST_CLASS_REMINDER_TIME],
        id="first_class_reminder",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    for time_text in CLASS_CONTEXT_TIMES:
        hour, minute = _parse_time(time_text)
        scheduler.add_job(
            send_class_update,
            CronTrigger(hour=hour, minute=minute, timezone=IST),
            args=[application, config, time_text],
            id=f"class_update_{time_text.replace(':', '')}",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

    for time_text in ATTENDANCE_UPDATE_TIMES:
        hour, minute = _parse_time(time_text)
        scheduler.add_job(
            send_total_attendance_update,
            CronTrigger(hour=hour, minute=minute, timezone=IST),
            args=[application, config, time_text],
            id=f"attendance_update_{time_text.replace(':', '')}",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

    scheduler.start()
    LOGGER.info(
        "Started scheduler: timetable_refresh=%s first_class=%s class_context=%s attendance=%s",
        TIMETABLE_REFRESH_TIME,
        FIRST_CLASS_REMINDER_TIME,
        CLASS_CONTEXT_TIMES,
        ATTENDANCE_UPDATE_TIMES,
    )
    return scheduler


async def shutdown_scheduler(scheduler: AsyncIOScheduler | None) -> None:
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
        LOGGER.info("Stopped timetable scheduler.")


async def refresh_and_send_daily_schedule(application: Application, config: AppConfig) -> None:
    now = datetime.now(IST)
    if _is_weekend(now):
        LOGGER.info("Skipping daily timetable refresh on weekend: %s", now.strftime("%A"))
        return

    LOGGER.info(
        "Daily timetable refresh trigger=%s weekday=%s source=live",
        now.isoformat(timespec="seconds"),
        now.strftime("%A"),
    )

    try:
        timetable = await asyncio.to_thread(get_timetable, config, True)
    except Exception:
        LOGGER.exception("Daily timetable refresh failed.")
        await application.bot.send_message(
            chat_id=config.credentials.telegram_chat_id,
            text="Timetable refresh failed at 08:50. Try /schedule once.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not _has_day_cache(timetable, now):
        await application.bot.send_message(
            chat_id=config.credentials.telegram_chat_id,
            text=f"Updated timetable found no classes for {now.strftime('%A')}.",
            parse_mode=ParseMode.HTML,
        )
        return

    await application.bot.send_message(
        chat_id=config.credentials.telegram_chat_id,
        text=format_today(timetable, now),
        parse_mode=ParseMode.HTML,
    )


async def send_total_attendance_update(application: Application, config: AppConfig, trigger_time: str | None = None) -> None:
    now = datetime.now(IST)
    if _is_weekend(now):
        LOGGER.info(
            "Skipping attendance refresh trigger=%s on weekend: %s",
            trigger_time or now.strftime("%H:%M"),
            now.strftime("%A"),
        )
        return

    LOGGER.info(
        "Attendance refresh trigger=%s now=%s source=portal",
        trigger_time or now.strftime("%H:%M"),
        now.isoformat(timespec="seconds"),
    )

    try:
        from .telegram_bot import _run_check_locked

        await _run_check_locked(_ApplicationContext(application), config, compare=False)
    except Exception as exc:
        LOGGER.exception("Attendance refresh failed.")
        await application.bot.send_message(
            chat_id=config.credentials.telegram_chat_id,
            text=f"Attendance refresh failed: {_short_error(exc)}",
            parse_mode=ParseMode.HTML,
        )
        return

    state = read_json(config.resolve_path("data_file"), {})
    LOGGER.info(
        "Attendance reminder trigger=%s now=%s source=refreshed_cache has_state=%s",
        trigger_time or now.strftime("%H:%M"),
        now.isoformat(timespec="seconds"),
        bool(state),
    )
    if not state:
        await application.bot.send_message(
            chat_id=config.credentials.telegram_chat_id,
            text="No saved attendance data yet. Run /analyze once.",
            parse_mode=ParseMode.HTML,
        )
        return

    await application.bot.send_message(
        chat_id=config.credentials.telegram_chat_id,
        text=_format_total_attendance(state),
        parse_mode=ParseMode.HTML,
    )


async def send_class_update(application: Application, config: AppConfig, trigger_time: str | None = None) -> None:
    now = datetime.now(IST)
    if _is_weekend(now):
        LOGGER.info(
            "Skipping class reminder trigger=%s on weekend: %s",
            trigger_time or now.strftime("%H:%M"),
            now.strftime("%A"),
        )
        return

    timetable, source = await _load_timetable_for_reminder(config, now, trigger_time)
    if not timetable:
        return

    if not _has_day_cache(timetable, now):
        LOGGER.info(
            "Class reminder trigger=%s now=%s weekday=%s cached_days=%s source=%s status=no_classes_today",
            trigger_time or now.strftime("%H:%M"),
            now.isoformat(timespec="seconds"),
            now.strftime("%A"),
            sorted(timetable),
            source,
        )
        await application.bot.send_message(
            chat_id=config.credentials.telegram_chat_id,
            text="No classes today",
            parse_mode=ParseMode.HTML,
        )
        return

    current = get_current_class(timetable, now)
    next_class = get_next_class(timetable, now)
    current_slot = current[1]["start_slot"] if current else None
    next_slot = next_class[1]["start_slot"] if next_class else None

    LOGGER.info(
        "Class reminder trigger=%s now=%s current_slot=%s next_slot=%s source=%s",
        trigger_time or now.strftime("%H:%M"),
        now.isoformat(timespec="seconds"),
        current_slot,
        next_slot,
        source,
    )

    message = _format_class_update(current, next_class, trigger_time)
    await application.bot.send_message(
        chat_id=config.credentials.telegram_chat_id,
        text=message,
        parse_mode=ParseMode.HTML,
    )


def _format_class_update(
    current: tuple[str, dict[str, Any]] | None,
    next_class: tuple[str, dict[str, Any]] | None,
    trigger_time: str | None,
) -> str:
    if trigger_time == FIRST_CLASS_REMINDER_TIME and next_class is not None:
        _, block = next_class
        entry = block["entry"]
        lines = ["<b>🔔 First Class Starting Soon</b>", "", _entry_label(entry)]
        if _show_room(entry):
            lines.append(f"Room: {entry.get('room', '-')}")
        lines.append(f"Time: {_block_time_text(block)}")
        return "\n".join(lines)

    if current is not None:
        _, current_block = current
        current_entry = current_block["entry"]
        lines = [
            f"<b>📘 Current:</b> {_entry_label(current_entry)} ({_block_time_text(current_block)})",
        ]
        if _show_room(current_entry):
            lines.append(f"Room: {current_entry.get('room', '-')}")
        if next_class is not None:
            _, next_block = next_class
            next_entry = next_block["entry"]
            lines.append(f"<b>⏭ Next:</b> {_entry_label(next_entry)} at {format_block_start(next_block)}")
            if _show_room(next_entry):
                lines.append(f"Room: {next_entry.get('room', '-')}")
        else:
            lines.append("<b>No more classes today</b>")
        return "\n".join(lines)

    if next_class is not None:
        _, next_block = next_class
        next_entry = next_block["entry"]
        lines = [
            "<b>Free now</b>",
            f"<b>⏭ Next:</b> {_entry_label(next_entry)} at {format_block_start(next_block)}",
        ]
        if _show_room(next_entry):
            lines.append(f"Room: {next_entry.get('room', '-')}")
        return "\n".join(lines)

    return "<b>No more classes today</b>"


async def _load_timetable_for_reminder(
    config: AppConfig,
    now: datetime,
    trigger_time: str | None,
) -> tuple[dict[str, dict[str, dict[str, str]]], str]:
    timetable = get_cached_timetable(config)
    if timetable and _has_day_cache(timetable, now):
        return timetable, "cache"

    LOGGER.warning(
        "Class reminder trigger=%s now=%s weekday=%s cached_days=%s source=cache status=refreshing_missing_cache",
        trigger_time or now.strftime("%H:%M"),
        now.isoformat(timespec="seconds"),
        now.strftime("%A"),
        sorted(timetable),
    )

    try:
        refreshed = await asyncio.to_thread(get_timetable, config, True)
    except Exception:
        LOGGER.exception(
            "Class reminder trigger=%s now=%s weekday=%s source=live status=refresh_failed",
            trigger_time or now.strftime("%H:%M"),
            now.isoformat(timespec="seconds"),
            now.strftime("%A"),
        )
        return {}, "live_failed"
    return refreshed, "live_refresh"


def _block_time_text(block: dict[str, Any]) -> str:
    return format_display_range(block["start"], block["end"])


def _entry_label(entry: dict[str, Any]) -> str:
    type_text = f" [{entry.get('type')}]" if entry.get("type") else ""
    return f"{entry.get('subject', '-')}{type_text}"


def _show_room(entry: dict[str, Any]) -> bool:
    subject = str(entry.get("subject", "")).strip().lower()
    room = str(entry.get("room", "")).strip()
    return subject != "lunch break" and room not in {"", "-"}


def _has_day_cache(timetable: dict[str, dict[str, dict[str, str]]], now: datetime) -> bool:
    day = now.strftime("%A")
    return bool(timetable.get(day))


def _is_weekend(now: datetime) -> bool:
    return now.strftime("%A") in {"Saturday", "Sunday"}


def _missing_cache_message(now: datetime) -> str:
    return (
        f"Timetable cache not available for {now.strftime('%A')}.\n"
        "Run /schedule once to refresh timetable cache."
    )


def _parse_time(value: str) -> tuple[int, int]:
    hour, minute = value.split(":", maxsplit=1)
    return int(hour), int(minute)


def _scheduler_enabled() -> bool:
    value = os.getenv("ENABLE_TIMETABLE_SCHEDULER", "true").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _format_total_attendance(state: dict) -> str:
    attendance = state.get("attendance") or {}
    return f"Attendance: {attendance.get('overall_percent', '-')}%\n{_format_last_updated(state)}"


def _format_last_updated(state: dict) -> str:
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
    return f"Last updated: {formatted}"


def _short_error(exc: BaseException) -> str:
    text = str(exc).splitlines()[0].strip()
    return (text or exc.__class__.__name__)[:300]


class _ApplicationContext:
    def __init__(self, application: Application) -> None:
        self.application = application
        self.bot = application.bot
