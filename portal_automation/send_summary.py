from __future__ import annotations

import argparse
import html
from datetime import datetime
from pathlib import Path
from typing import Any

import pytz

from .config import load_config
from .notifier import TelegramNotifier
from .storage import read_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Send the latest saved SRAAP summary to Telegram.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument(
        "--section",
        default="all",
        choices=["all", "attendance", "last-week", "courses", "marks", "memo"],
        help="Which saved data to send.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    state = read_json(config.resolve_path("data_file"), {})
    if not state:
        print("No saved state found. Run the monitor once first.")
        return 1

    notifier = TelegramNotifier(config.credentials)
    message = build_summary(state, args.section)
    ok = True
    for chunk in _chunks(message, 3500):
        ok = notifier.send(chunk) and ok
    print("sent" if ok else "failed")
    return 0 if ok else 1


def build_summary(state: dict[str, Any], section: str = "all") -> str:
    parts = [f"<b>SRAAP {summary_title(section)}</b>"]
    last_updated = _format_last_updated(state)
    if last_updated:
        parts.append(last_updated)

    attendance = state.get("attendance") or {}
    if attendance and section == "total":
        return (
            f"<b>SRAAP total attendance</b>\n"
            f"{html.escape(str(attendance.get('overall_percent', '-')))}%\n"
            f"{last_updated}"
        )

    if attendance and section in {"all", "attendance"}:
        parts.append(f"<b>Total Attendance</b>\n{html.escape(str(attendance.get('overall_percent', '-')))}%")
        parts.append(_format_last_week(attendance.get("last_week") or []))
        parts.append(_format_course_attendance(attendance.get("course_wise") or []))

    if attendance and section == "last-week":
        parts.append(_format_last_week(attendance.get("last_week") or []))

    if attendance and section == "courses":
        parts.append(f"<b>Total Attendance</b>\n{html.escape(str(attendance.get('overall_percent', '-')))}%")
        parts.append(_format_course_attendance(attendance.get("course_wise") or []))

    marks = state.get("marks") or []
    if marks and section in {"all", "marks"}:
        parts.append(_format_marks(marks))

    memo = state.get("memo") or {}
    if memo and section in {"all", "memo"}:
        target = memo.get("target") or {}
        status = memo.get("status") or ("Available" if memo.get("available") else "Not available")
        parts.append(
            "<b>Semester Memo</b>\n"
            f"Target: {html.escape(str(target.get('description', 'Sem 2 April memo')))}\n"
            f"Status: {html.escape(str(status))}"
        )

    body = "\n\n".join(part for part in parts if part)
    if body.strip() == parts[0]:
        return f"{parts[0]}\nNo saved data found for this section."
    return body


def summary_title(section: str) -> str:
    titles = {
        "all": "latest saved check",
        "total": "total attendance",
        "attendance": "attendance summary",
        "last-week": "last 3 attendance days",
        "courses": "course attendance",
        "marks": "CIE / ETE marks",
        "memo": "semester memo status",
    }
    return titles.get(section, "latest saved check")


def _format_last_updated(state: dict[str, Any]) -> str:
    value = state.get("last_updated_at") or state.get("last_checked_at")
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = pytz.timezone("Asia/Kolkata").localize(parsed)
        parsed = parsed.astimezone(pytz.timezone("Asia/Kolkata"))
        formatted = parsed.strftime("%I:%M %p").lstrip("0")
    except ValueError:
        formatted = str(value)
    return f"<b>Last updated</b>: {html.escape(formatted)}"


def _format_last_week(rows: list[Any]) -> str:
    lines = ["<b>Last 3 Attendance Days</b>"]
    for row in rows[:3]:
        if not isinstance(row, dict):
            continue
        lines.append(
            f"S.No: {html.escape(str(row.get('S.No', '-')))} | "
            f"Date: {html.escape(str(row.get('Date', '-')))} | "
            f"Held: {html.escape(str(row.get('Held', '-')))} | "
            f"Attend: {html.escape(str(row.get('Attend', '-')))}"
        )
    return "\n".join(lines)


def _format_course_attendance(rows: list[Any]) -> str:
    lines = ["<b>Course Attendance</b>"]
    for row in rows:
        if not isinstance(row, dict):
            continue
        lines.append(
            f"{html.escape(str(row.get('Course Name', '-')))}\n"
            f"Present: {html.escape(str(row.get('PR', '-')))} | "
            f"Absent: {html.escape(str(row.get('AB', '-')))} | "
            f"Held: {html.escape(str(row.get('Held Cls', '-')))} | "
            f"{html.escape(str(row.get('Present%', '-')))}% | "
            f"Loss: {html.escape(str(row.get('Loss%', '-')))}%"
        )
    return "\n\n".join(lines)


def _format_marks(rows: list[Any]) -> str:
    lines = ["<b>CIE / ETE Marks</b>"]
    for row in rows:
        if isinstance(row, list) and len(row) >= 5:
            lines.append(
                f"{html.escape(str(row[0]))}. {html.escape(str(row[2]))}\n"
                f"CIE: {html.escape(str(row[3]))} | ETE: {html.escape(str(row[4]))}"
            )
        else:
            lines.append(html.escape(str(row)))
    return "\n\n".join(lines)


def _chunks(text: str, size: int) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)]


if __name__ == "__main__":
    raise SystemExit(main())
