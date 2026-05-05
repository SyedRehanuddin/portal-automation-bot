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


def get_current_class(timetable: dict[str, dict[str, dict[str, str]]], now: datetime | None = None) -> tuple[str, str, dict[str, str]] | None:
    now = _coerce_ist(now)
    day = now.strftime("%A")
    if day not in DAYS:
        return None

    current_time = now.time()
    rows = timetable.get(day, {})
    for slot in _sorted_slots(rows):
        start, end = _slot_range(slot, now)
        if start <= current_time < end:
            return day, slot, timetable[day][slot]
    return None


def get_next_class(timetable: dict[str, dict[str, dict[str, str]]], now: datetime | None = None) -> tuple[str, str, dict[str, str]] | None:
    now = _coerce_ist(now)
    day = now.strftime("%A")
    if day not in DAYS:
        return None

    current_time = now.time()
    rows = timetable.get(day, {})
    for slot in _sorted_slots(rows):
        start = _slot_start_time(slot)
        if start > current_time:
            return day, slot, timetable[day][slot]
    return None


def format_now(timetable: dict[str, dict[str, dict[str, str]]], now: datetime | None = None) -> str:
    now = _coerce_ist(now)
    if now.strftime("%A") in {"Saturday", "Sunday"}:
        return "Holiday"
    current = get_current_class(timetable, now)
    if current is None:
        return "No class right now"
    _, slot, entry = current
    return _format_class(entry, slot, include_end=True)


def format_next(timetable: dict[str, dict[str, dict[str, str]]], now: datetime | None = None) -> str:
    now = _coerce_ist(now)
    if now.strftime("%A") in {"Saturday", "Sunday"}:
        return "Holiday"
    next_class = get_next_class(timetable, now)
    if next_class is None:
        return "No more classes today"
    _, slot, entry = next_class
    return "Next Class:\n" + _format_class(entry, slot, include_end=False)


def format_today(timetable: dict[str, dict[str, dict[str, str]]], now: datetime | None = None) -> str:
    now = _coerce_ist(now)
    day = now.strftime("%A")
    if day in {"Saturday", "Sunday"}:
        return "Holiday"
    return format_day(timetable, day)


def format_day(timetable: dict[str, dict[str, dict[str, str]]], day: str) -> str:
    rows = timetable.get(day, {})
    if not rows:
        return f"{day} Schedule:\nNo classes"

    lines = [f"{day} Schedule:"]
    for slot in _display_slots(rows):
        if slot == "12:00":
            lines.append("12:00 - Lunch")
            continue
        if slot not in rows:
            lines.append(f"{slot} - Leisure")
            continue
        entry = rows[slot]
        type_text = f" [{entry.get('type')}]" if entry.get("type") else ""
        lines.append(f"{slot} - {entry.get('subject', '-')}{type_text} (Room {entry.get('room', '-')})")
    return "\n".join(lines)


def format_week(timetable: dict[str, dict[str, dict[str, str]]]) -> str:
    parts = ["Weekly Timetable"]
    for day in DAYS:
        parts.append(format_day(timetable, day))
    return "\n\n".join(parts)


def format_current_room(timetable: dict[str, dict[str, dict[str, str]]], now: datetime | None = None) -> str:
    current = get_current_class(timetable, now)
    if current is None:
        return "No class right now"
    _, _, entry = current
    return f"Room: {entry.get('room', '-')}"


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


def _slot_range(slot: str, day_time: datetime) -> tuple[time, time]:
    start_datetime = datetime.combine(day_time.date(), _slot_start_time(slot))
    end_datetime = start_datetime + timedelta(hours=1)
    return start_datetime.time(), end_datetime.time()


def _sorted_slots(rows: dict[str, dict[str, str]]) -> list[str]:
    return sorted(rows, key=_slot_start_time)


def _now_ist() -> datetime:
    return datetime.now(IST)


def _coerce_ist(value: datetime | None = None) -> datetime:
    if value is None:
        return _now_ist()
    if value.tzinfo is None:
        return IST.localize(value)
    return value.astimezone(IST)


def _debug_disable_cache() -> bool:
    value = os.getenv("TIMETABLE_DISABLE_CACHE", "true").strip().lower()
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
    end_time = (datetime.combine(datetime.today(), time.fromisoformat(slot)) + timedelta(hours=1)).strftime("%H:%M")
    time_line = f"{slot} - {end_time}" if include_end else slot
    type_text = f" [{entry.get('type')}]" if entry.get("type") else ""
    return (
        f"{entry.get('subject', '-')}{type_text}\n"
        f"Room: {entry.get('room', '-')}\n"
        f"Time: {time_line}"
    )
