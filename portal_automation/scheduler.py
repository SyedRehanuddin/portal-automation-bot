from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.ext import Application

from .config import AppConfig
from .timetable import format_today, get_cached_timetable, get_current_class, get_next_class


LOGGER = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")
CLASS_UPDATE_TIMES = ["08:55", "10:05", "11:05", "12:05", "13:05", "14:05", "15:05", "16:05", "16:35", "17:05"]


def start_scheduler(application: Application, config: AppConfig) -> AsyncIOScheduler | None:
    if not _scheduler_enabled():
        LOGGER.info("Timetable background scheduler is disabled.")
        return None

    scheduler = AsyncIOScheduler(timezone=IST)
    scheduler.add_job(
        send_daily_schedule,
        CronTrigger(hour=8, minute=50, timezone=IST),
        args=[application, config],
        id="daily_today_schedule",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    for time_text in CLASS_UPDATE_TIMES:
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

    scheduler.start()
    LOGGER.info("Started timetable scheduler with %d class reminder jobs.", len(CLASS_UPDATE_TIMES))
    return scheduler


async def shutdown_scheduler(scheduler: AsyncIOScheduler | None) -> None:
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
        LOGGER.info("Stopped timetable scheduler.")


async def send_daily_schedule(application: Application, config: AppConfig) -> None:
    now = datetime.now(IST)
    timetable = get_cached_timetable(config)
    LOGGER.info(
        "Daily schedule trigger=%s weekday=%s cached_days=%s source=cache",
        now.isoformat(timespec="seconds"),
        now.strftime("%A"),
        sorted(timetable),
    )
    if not _has_day_cache(timetable, now):
        await application.bot.send_message(
            chat_id=config.credentials.telegram_chat_id,
            text=_missing_cache_message(now),
        )
        return

    await application.bot.send_message(
        chat_id=config.credentials.telegram_chat_id,
        text=format_today(timetable, now),
    )


async def send_class_update(application: Application, config: AppConfig, trigger_time: str | None = None) -> None:
    now = datetime.now(IST)
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
