from __future__ import annotations

import logging
import os
import asyncio
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.ext import Application

from .config import AppConfig
from .storage import read_json
from .timetable import format_today, get_cached_timetable, get_current_class, get_next_class, get_timetable


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
        )
        return

    if not _has_day_cache(timetable, now):
        await application.bot.send_message(
            chat_id=config.credentials.telegram_chat_id,
            text=f"Updated timetable found no classes for {now.strftime('%A')}.",
        )
        return

    await application.bot.send_message(
        chat_id=config.credentials.telegram_chat_id,
        text=format_today(timetable, now),
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
        )
        return

    await application.bot.send_message(
        chat_id=config.credentials.telegram_chat_id,
        text=_format_total_attendance(state),
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

    timetable = get_cached_timetable(config)
    if not _has_day_cache(timetable, now):
        LOGGER.warning(
            "Class reminder trigger=%s now=%s weekday=%s cached_days=%s source=cache status=missing_day_cache",
            trigger_time or now.strftime("%H:%M"),
            now.isoformat(timespec="seconds"),
            now.strftime("%A"),
            sorted(timetable),
        )
        await application.bot.send_message(
            chat_id=config.credentials.telegram_chat_id,
            text=_missing_cache_message(now),
        )
        return

    current = get_current_class(timetable, now)
    next_class = get_next_class(timetable, now)
    current_slot = current[1] if current else None
    next_slot = next_class[1] if next_class else None

    LOGGER.info(
        "Class reminder trigger=%s now=%s current_slot=%s next_slot=%s source=cache",
        trigger_time or now.strftime("%H:%M"),
        now.isoformat(timespec="seconds"),
        current_slot,
        next_slot,
    )

    message = _format_class_update(current, next_class, trigger_time)
    await application.bot.send_message(chat_id=config.credentials.telegram_chat_id, text=message)


def _format_class_update(
    current: tuple[str, str, dict[str, str]] | None,
    next_class: tuple[str, str, dict[str, str]] | None,
    trigger_time: str | None,
) -> str:
    if trigger_time == "08:55" and next_class is not None:
        _, slot, entry = next_class
        return (
            "🔔 First Class Starting Soon\n\n"
            f"{entry.get('subject', '-')}\n"
            f"Room: {entry.get('room', '-')}\n"
            f"Time: {_slot_range_text(slot)}"
        )

    if current is not None:
        _, current_slot, current_entry = current
        lines = [
            f"📘 Current: {current_entry.get('subject', '-')} ({_slot_range_text(current_slot)})",
            f"Room: {current_entry.get('room', '-')}",
        ]
        if next_class is not None:
            _, next_slot, next_entry = next_class
            lines.extend(
                [
                    f"⏭ Next: {next_entry.get('subject', '-')} at {next_slot}",
                    f"Room: {next_entry.get('room', '-')}",
                ]
            )
        else:
            lines.append("✅ No more classes today")
        return "\n".join(lines)

    if next_class is not None:
        _, next_slot, next_entry = next_class
        return (
            "😎 Free now\n"
            f"⏭ Next: {next_entry.get('subject', '-')} at {next_slot}\n"
            f"Room: {next_entry.get('room', '-')}"
        )

    return "✅ No more classes today"


def _slot_range_text(slot: str) -> str:
    start = datetime.strptime(slot, "%H:%M")
    end = start + timedelta(hours=1)
    return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"


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
    return (
        f"Attendance: {attendance.get('overall_percent', '-')}%\n"
        f"{_format_last_updated(state)}"
    )


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
