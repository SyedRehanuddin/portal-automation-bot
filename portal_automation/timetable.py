from __future__ import annotations

import logging
import os
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytz
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.ui import WebDriverWait

from .config import AppConfig
from .storage import read_json, write_json


LOGGER = logging.getLogger(__name__)

TIMETABLE_LOGIC_VERSION = "v2_fixed"
IST = pytz.timezone("Asia/Kolkata")
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
ALL_DAYS = DAYS + ["Saturday", "Sunday"]
TIMES = ["09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00", "19:00", "20:00"]
LUNCH_START = "12:00"
LUNCH_END = "13:00"
LUNCH_ENTRY = {"subject": "Lunch Break", "room": "-", "type": ""}
LEISURE_ENTRY = {"subject": "Leisure", "room": "-", "type": ""}


class TimetableError(RuntimeError):
    """Raised when timetable scraping or parsing fails."""


def get_timetable(config: AppConfig, force_refresh: bool = False) -> dict[str, dict[str, dict[str, str]]]:
    cache_path = _cache_path(config)
    cached = read_json(cache_path, {})
    if _debug_disable_cache():
        LOGGER.warning(
            "TIMETABLE_LOGIC_VERSION=%s cache disabled for debugging; module=%s",
            TIMETABLE_LOGIC_VERSION,
            __file__,
        )
    elif not force_refresh and _cache_valid(cached, config):
        return cached.get("data", {})

    data = scrape_timetable(config)
    write_json(cache_path, {"fetched_at": _now_ist().isoformat(timespec="seconds"), "data": data})
    return data


def get_cached_timetable(config: AppConfig) -> dict[str, dict[str, dict[str, str]]]:
    cached = read_json(_cache_path(config), {})
    data = cached.get("data") if isinstance(cached, dict) else None
    return data if isinstance(data, dict) else {}


def scrape_timetable(config: AppConfig) -> dict[str, dict[str, dict[str, str]]]:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1366,900")
    _configure_chrome_options(options)

    driver = webdriver.Chrome(options=options)
    driver.set_script_timeout(20)
    wait = WebDriverWait(driver, 30)
    try:
        timetable_config = config.raw.get("timetable", {})
        driver.get(timetable_config.get("url", "https://timetable.sruniv.com/batchReport"))

        _select_value(wait, "degree", timetable_config.get("degree", "BTECH"))
        wait.until(lambda d: len(Select(d.find_element(By.ID, "year")).options) > 1)
        _select_value(wait, "year", _normalize_year(str(timetable_config.get("year", "First"))))
        wait.until(lambda d: _select_has_option(d, "batch", timetable_config.get("batch", "")))
        _select_value(wait, "batch", timetable_config.get("batch", "25CAIBTCSB36"))

        ajax_data = _fetch_timetable_json(driver, timetable_config.get("batch", "25CAIBTCSB36"), _normalize_year(str(timetable_config.get("year", "First"))))
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#searchBatchReport button[type='submit']"))).click()
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#account21 tbody tr, #account11 tbody tr")))

        if ajax_data:
            return parse_ajax_data(ajax_data)
        return parse_table(driver)
    except (TimeoutException, WebDriverException) as exc:
        raise TimetableError("Timetable not available") from exc
    finally:
        driver.quit()


def parse_table(driver: webdriver.Chrome) -> dict[str, dict[str, dict[str, str]]]:
    data: dict[str, dict[str, dict[str, str]]] = {day: {} for day in DAYS}

    rows = driver.find_elements(By.CSS_SELECTOR, "#account21 tbody tr")
    if rows:
        for row in rows:
            cells = [cell.text.strip() for cell in row.find_elements(By.CSS_SELECTOR, "td")]
            if len(cells) < 9:
                continue
            day = cells[2]
            slot = _normalize_slot(cells[3])
            if day not in DAYS or slot is None:
                continue
            entry = _entry(cells[7], cells[4], cells[8])
            data[day][slot] = _merge_entries(data[day].get(slot), entry)
        return data

    rows = driver.find_elements(By.CSS_SELECTOR, "#account11 tbody tr")
    if rows:
        for row in rows:
            cells = [cell.text.strip() for cell in row.find_elements(By.CSS_SELECTOR, "td")]
            if len(cells) < 6:
                continue
            slot = _normalize_slot(cells[0])
            if slot is None:
                continue
            for index, day in enumerate(DAYS, start=1):
                if cells[index].strip():
                    data[day][slot] = _parse_grid_cell(cells[index])
        return data

    raise TimetableError("Timetable parsing failed")


def parse_ajax_data(response: dict[str, Any]) -> dict[str, dict[str, dict[str, str]]]:
    if not response.get("success"):
        raise TimetableError("Timetable not available")

    raw_data = response.get("data")
    if not isinstance(raw_data, dict):
        raise TimetableError("Timetable parsing failed")

    data: dict[str, dict[str, dict[str, str]]] = {day: {} for day in DAYS}
    for day in DAYS:
        day_slots = raw_data.get(day, {})
        if not isinstance(day_slots, dict):
            continue
        for slot, schedules in day_slots.items():
            normalized_slot = _normalize_slot(str(slot))
            if normalized_slot is None or not isinstance(schedules, list) or not schedules:
                continue
            merged: dict[str, str] | None = None
            for schedule in schedules:
                if not isinstance(schedule, dict):
                    continue
                entry = _entry(
                    str(schedule.get("subject", "")),
                    str(schedule.get("room_name", "")),
                    str(schedule.get("ltp", "")),
                )
                merged = _merge_entries(merged, entry)
            if merged is not None:
                data[day][normalized_slot] = merged
    return data


def get_current_class(timetable: dict[str, dict[str, dict[str, str]]], now: datetime | None = None) -> tuple[str, dict[str, Any]] | None:
    now = _coerce_ist(now)
    day = now.strftime("%A")
    if day not in DAYS:
        return None

    current_time = now.time()
    for block in _day_blocks(timetable, day, now):
        start = block["start"]
        end = block["end"]
        if start <= current_time < end:
            return day, block
    return None


def get_next_class(timetable: dict[str, dict[str, dict[str, str]]], now: datetime | None = None) -> tuple[str, dict[str, Any]] | None:
    now = _coerce_ist(now)
    day = now.strftime("%A")
    if day not in DAYS:
        return None

    current_time = now.time()
    for block in _day_blocks(timetable, day, now):
        start = block["start"]
        if start > current_time:
            return day, block
    return None


def format_now(timetable: dict[str, dict[str, dict[str, str]]], now: datetime | None = None) -> str:
    now = _coerce_ist(now)
    if now.strftime("%A") in {"Saturday", "Sunday"}:
        return "Holiday"
    current = get_current_class(timetable, now)
    if current is None:
        return "<b>Free now</b>"
    _, block = current
    return _format_current_block(block)


def format_next(timetable: dict[str, dict[str, dict[str, str]]], now: datetime | None = None) -> str:
    now = _coerce_ist(now)
    if now.strftime("%A") in {"Saturday", "Sunday"}:
        return "Holiday"
    next_class = get_next_class(timetable, now)
    if next_class is None:
        return "<b>No more classes today</b>"
    _, block = next_class
    return _format_next_block(block)


def format_today(timetable: dict[str, dict[str, dict[str, str]]], now: datetime | None = None) -> str:
    now = _coerce_ist(now)
    day = now.strftime("%A")
    if day in {"Saturday", "Sunday"}:
        return "Holiday"
    return format_day(timetable, day)


def format_day(timetable: dict[str, dict[str, dict[str, str]]], day: str) -> str:
    if day not in DAYS:
        return f"{day} Schedule:\nNo classes"

    blocks = _day_blocks_with_gaps(timetable, day)
    class_blocks = [block for block in blocks if block["kind"] == "class"]
    if not class_blocks:
        return f"{day} Schedule:\nNo classes"

    lines = [f"{day} Schedule:"]
    for block in blocks:
        lines.append(_format_day_block(block))
    return "\n".join(lines)


def format_week(timetable: dict[str, dict[str, dict[str, str]]]) -> str:
    parts = ["Weekly Timetable"]
    for day in DAYS:
        parts.append(format_day(timetable, day))
    return "\n\n".join(parts)


def format_current_room(timetable: dict[str, dict[str, dict[str, str]]], now: datetime | None = None) -> str:
    current = get_current_class(timetable, now)
    if current is None:
        return "<b>Free now</b>"
    _, block = current
    entry = block["entry"]
    return f"Room: {entry.get('room', '-')}"


def format_display_time(value: time) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def format_display_range(start: time, end: time) -> str:
    return f"{format_display_time(start)} – {format_display_time(end)}"


def format_block_start(block: dict[str, Any]) -> str:
    return format_display_time(block["start"])


def _select_value(wait: WebDriverWait, element_id: str, value: str) -> None:
    select = Select(wait.until(EC.presence_of_element_located((By.ID, element_id))))
    try:
        select.select_by_value(value)
    except WebDriverException:
        select.select_by_visible_text(value)


def _select_has_option(driver: webdriver.Chrome, element_id: str, value: str) -> bool:
    try:
        return any(option.get_attribute("value") == value or option.text.strip() == value for option in Select(driver.find_element(By.ID, element_id)).options)
    except WebDriverException:
        return False


def _fetch_timetable_json(driver: webdriver.Chrome, batch: str, year: str) -> dict[str, Any] | None:
    try:
        result = driver.execute_async_script(
            """
const batch = arguments[0];
const year = arguments[1];
const done = arguments[arguments.length - 1];
const tokenElement = document.querySelector('input[name=_token]');
if (!tokenElement) {
  done(null);
  return;
}
const body = new URLSearchParams({_token: tokenElement.value, batch, year});
const timeout = setTimeout(() => done(null), 15000);
fetch('/searchBatchReportPublic', {
  method: 'POST',
  headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
  body
}).then(response => response.json()).then(data => {
  clearTimeout(timeout);
  done(data);
}).catch(() => {
  clearTimeout(timeout);
  done(null);
});
""",
            batch,
            year,
        )
    except WebDriverException as exc:
        LOGGER.warning("Timetable AJAX fetch failed; falling back to rendered table: %s", exc.__class__.__name__)
        return None
    return result if isinstance(result, dict) else None


def _cache_path(config: AppConfig) -> Path:
    timetable_config = config.raw.get("timetable", {})
    path = Path(timetable_config.get("cache_file", "data/timetable.json"))
    if path.is_absolute():
        return path
    return config.root_dir / path


def _cache_valid(cached: dict[str, Any], config: AppConfig) -> bool:
    fetched_at = cached.get("fetched_at")
    if not fetched_at or not cached.get("data"):
        return False
    ttl_hours = float(config.raw.get("timetable", {}).get("cache_ttl_hours", 4))
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    if fetched.tzinfo is None:
        fetched = IST.localize(fetched)
    return _now_ist() - fetched.astimezone(IST) < timedelta(hours=ttl_hours)


def _entry(subject: str, room_name: str, ltp: str = "") -> dict[str, str]:
    return {"subject": _clean_subject(subject), "room": _room_number(room_name), "type": _class_type(ltp)}


def _parse_grid_cell(text: str) -> dict[str, str]:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), text.strip())
    room_match = re.search(r"\(([^)]*)\)", first_line)
    subject = first_line.split("(")[0].strip()
    room = _room_number(room_match.group(1) if room_match else "")
    type_match = re.search(r"\(([LTP])\)", first_line)
    return {"subject": _clean_subject(subject), "room": room, "type": _class_type(type_match.group(1) if type_match else "")}


def _clean_subject(subject: str) -> str:
    return re.sub(r"\s+", " ", subject.replace("&amp;", "&")).strip()


def _room_number(room_name: str) -> str:
    match = re.search(r"(\d+)", room_name)
    return match.group(1) if match else room_name.strip()


def _merge_entries(existing: dict[str, str] | None, new: dict[str, str]) -> dict[str, str]:
    if existing is None:
        return new
    subjects = [existing.get("subject", ""), new.get("subject", "")]
    rooms = [existing.get("room", ""), new.get("room", "")]
    types = [existing.get("type", ""), new.get("type", "")]
    return {
        "subject": " / ".join(value for value in subjects if value),
        "room": " / ".join(value for value in rooms if value),
        "type": " / ".join(value for value in types if value),
    }


def _configure_chrome_options(options: Options) -> None:
    chrome_bin = os.getenv("CHROME_BIN") or os.getenv("CHROME_BINARY_PATH")
    if chrome_bin:
        options.binary_location = chrome_bin

    if os.getenv("RENDER", "").strip().lower() == "true" or os.getenv("SELENIUM_CLOUD", "").strip().lower() in {"1", "true", "yes", "on"}:
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--remote-debugging-port=9222")


def _normalize_slot(value: str) -> str | None:
    match = re.search(r"(\d{1,2}):(\d{2})", value)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    return f"{hour:02d}:{minute:02d}"


def _normalize_year(value: str) -> str:
    mapping = {
        "1": "First",
        "first": "First",
        "2": "Second",
        "second": "Second",
        "3": "Third",
        "third": "Third",
        "4": "Fourth",
        "four": "Fourth",
        "fourth": "Fourth",
    }
    return mapping.get(value.strip().lower(), value)


def _slot_minutes(slot: str) -> int:
    parsed = _slot_start_time(slot)
    return parsed.hour * 60 + parsed.minute


def _slot_start_time(slot: str) -> time:
    return datetime.strptime(slot.strip(), "%H:%M").time()


def _slot_range(slot: str, day_time: datetime | None = None) -> tuple[time, time]:
    reference_day = _coerce_ist(day_time) if day_time is not None else _now_ist()
    start_datetime = datetime.combine(reference_day.date(), _slot_start_time(slot))
    end_datetime = start_datetime + timedelta(hours=1)
    return start_datetime.time(), end_datetime.time()


def _sorted_slots(rows: dict[str, dict[str, str]]) -> list[str]:
    return sorted(rows, key=_slot_start_time)


def _day_blocks(
    timetable: dict[str, dict[str, dict[str, str]]],
    day: str,
    day_time: datetime | None = None,
) -> list[dict[str, Any]]:
    rows = timetable.get(day, {})
    blocks = _merge_class_blocks(rows, day_time)
    lunch_block = _lunch_block(day_time)
    if not any(_blocks_overlap(block, lunch_block) for block in blocks):
        blocks.append(lunch_block)
    return sorted(blocks, key=lambda block: block["start"])


def _day_blocks_with_gaps(
    timetable: dict[str, dict[str, dict[str, str]]],
    day: str,
    day_time: datetime | None = None,
) -> list[dict[str, Any]]:
    blocks = _day_blocks(timetable, day, day_time)
    if not blocks:
        return []

    filled: list[dict[str, Any]] = [blocks[0]]
    for block in blocks[1:]:
        previous = filled[-1]
        if previous["end"] < block["start"]:
            filled.append(_leisure_block(previous["end"], block["start"]))
        filled.append(block)
    return filled


def _merge_class_blocks(rows: dict[str, dict[str, str]], day_time: datetime | None = None) -> list[dict[str, Any]]:
    merged_blocks: list[dict[str, Any]] = []
    for slot in _sorted_slots(rows):
        entry = rows[slot]
        start, end = _slot_range(slot, day_time)
        if merged_blocks and _can_merge_block(merged_blocks[-1], entry, start):
            merged_blocks[-1]["end"] = end
            merged_blocks[-1]["end_slot"] = _time_to_slot(end)
            continue
        merged_blocks.append(
            {
                "kind": "class",
                "entry": entry,
                "start": start,
                "end": end,
                "start_slot": slot,
                "end_slot": _time_to_slot(end),
            }
        )
    return merged_blocks


def _lunch_block(day_time: datetime | None = None) -> dict[str, Any]:
    start, end = _slot_range(LUNCH_START, day_time)
    return {
        "kind": "lunch",
        "entry": LUNCH_ENTRY,
        "start": start,
        "end": end,
        "start_slot": LUNCH_START,
        "end_slot": LUNCH_END,
    }


def _leisure_block(start: time, end: time) -> dict[str, Any]:
    return {
        "kind": "leisure",
        "entry": LEISURE_ENTRY,
        "start": start,
        "end": end,
        "start_slot": _time_to_slot(start),
        "end_slot": _time_to_slot(end),
    }


def _can_merge_block(block: dict[str, Any], entry: dict[str, str], next_start: time) -> bool:
    if block["kind"] != "class":
        return False
    return (
        block["end"] == next_start
        and block["entry"].get("subject") == entry.get("subject")
        and block["entry"].get("room") == entry.get("room")
        and block["entry"].get("type") == entry.get("type")
    )


def _blocks_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left["start"] < right["end"] and right["start"] < left["end"]


def _now_ist() -> datetime:
    return datetime.now(IST)


def _coerce_ist(value: datetime | None = None) -> datetime:
    if value is None:
        return _now_ist()
    if value.tzinfo is None:
        return IST.localize(value)
    return value.astimezone(IST)


def _debug_disable_cache() -> bool:
    value = os.getenv("TIMETABLE_DISABLE_CACHE", "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _class_type(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"l", "lecture"}:
        return "L"
    if normalized in {"t", "tutorial"}:
        return "Tutorial"
    if normalized in {"p", "lab", "practical"}:
        return "P"
    return value.strip()


def _display_slots(rows: dict[str, dict[str, str]]) -> list[str]:
    slots = sorted(set(rows) | {"12:00"})
    first = min(_slot_minutes(slot) for slot in slots)
    last = max(max(_slot_minutes(slot) for slot in slots), _slot_minutes("16:00"))
    return [f"{minutes // 60:02d}:00" for minutes in range(first, last + 1, 60)]


def _format_class(entry: dict[str, str], slot: str, include_end: bool) -> str:
    start_time = time.fromisoformat(slot)
    end_time = (datetime.combine(datetime.today(), start_time) + timedelta(hours=1)).time()
    time_line = format_display_range(start_time, end_time) if include_end else format_display_time(start_time)
    type_text = f" [{entry.get('type')}]" if entry.get("type") else ""
    return (
        f"{entry.get('subject', '-')}{type_text}\n"
        f"Room: {entry.get('room', '-')}\n"
        f"Time: {time_line}"
    )


def _format_block(block: dict[str, Any]) -> str:
    return _format_entry_with_range(block["entry"], block["start"], block["end"])


def _format_current_block(block: dict[str, Any]) -> str:
    entry = block["entry"]
    lines = [f"<b>📘 Current:</b> {_entry_label(entry)} ({format_display_range(block['start'], block['end'])})"]
    if _show_room(entry):
        lines.append(f"Room: {entry.get('room', '-')}")
    return "\n".join(lines)


def _format_next_block(block: dict[str, Any]) -> str:
    entry = block["entry"]
    lines = [f"<b>⏭ Next:</b> {_entry_label(entry)} at {format_display_time(block['start'])}"]
    if _show_room(entry):
        lines.append(f"Room: {entry.get('room', '-')}")
    return "\n".join(lines)


def _format_day_block(block: dict[str, Any]) -> str:
    entry = block["entry"]
    type_text = f" [{entry.get('type')}]" if entry.get("type") else ""
    room_text = f" (Room {entry.get('room', '-')})" if _show_room(entry) else ""
    return (
        f"{format_display_range(block['start'], block['end'])} - "
        f"{entry.get('subject', '-')}{type_text}{room_text}"
    )


def _format_entry_with_range(entry: dict[str, str], start: time, end: time) -> str:
    type_text = f" [{entry.get('type')}]" if entry.get("type") else ""
    lines = [
        f"{entry.get('subject', '-')}{type_text}",
    ]
    if _show_room(entry):
        lines.append(f"Room: {entry.get('room', '-')}")
    lines.append(f"Time: {format_display_range(start, end)}")
    return "\n".join(lines)


def _time_text(value: time) -> str:
    return format_display_time(value)


def _entry_label(entry: dict[str, str]) -> str:
    type_text = f" [{entry.get('type')}]" if entry.get("type") else ""
    return f"{entry.get('subject', '-')}{type_text}"


def _time_to_slot(value: time) -> str:
    return value.strftime("%H:%M")


def _show_room(entry: dict[str, str]) -> bool:
    subject = entry.get("subject", "").strip().lower()
    room = entry.get("room", "").strip()
    return subject not in {"lunch break", "leisure"} and room not in {"", "-"}
