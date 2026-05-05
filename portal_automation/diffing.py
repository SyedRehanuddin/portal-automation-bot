from __future__ import annotations

import html
from typing import Any


def build_change_messages(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    messages: list[str] = []

    if old.get("attendance") != new.get("attendance"):
        messages.append(_format_attendance_change(old.get("attendance") or {}, new.get("attendance") or {}))

    if old.get("marks") != new.get("marks"):
        messages.append(_format_section_change("Marks updated", old.get("marks"), new.get("marks")))

    return messages


def _format_section_change(title: str, old_value: Any, new_value: Any) -> str:
    return (
        f"<b>{html.escape(title)}</b>\n"
        f"\n<b>Before:</b>\n{html.escape(_compact(old_value))}"
        f"\n\n<b>Now:</b>\n{html.escape(_compact(new_value))}"
    )


def _compact(value: Any, max_chars: int = 2500) -> str:
    if value is None:
        return "No previous data"
    text = _to_text(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 40].rstrip() + "\n...message shortened..."


def _to_text(value: Any) -> str:
    if isinstance(value, list):
        if not value:
            return "[]"
        return "\n".join(_row_to_text(item) for item in value[:25])
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "\n".join(f"{key}: {_to_text(item)}" for key, item in value.items())
    return str(value)


def _row_to_text(item: Any) -> str:
    if isinstance(item, dict):
        return " | ".join(f"{key}: {value}" for key, value in item.items())
    if isinstance(item, list):
        return " | ".join(str(value) for value in item)
    return str(item)


def _format_attendance_change(old_value: dict[str, Any], new_value: dict[str, Any]) -> str:
    lines = ["<b>Attendance changed</b>"]

    old_total = old_value.get("overall_percent")
    new_total = new_value.get("overall_percent")
    if old_total != new_total:
        before = old_total if old_total is not None else "not saved"
        lines.append(f"\n<b>Total Attendance</b>\n{html.escape(str(before))}% -> {html.escape(str(new_total))}%")
    elif new_total is not None:
        lines.append(f"\n<b>Total Attendance</b>\n{html.escape(str(new_total))}%")

    last_week = new_value.get("last_week") or []
    if last_week:
        lines.append("\n<b>Last 3 Attendance Days</b>")
        for row in last_week[:3]:
            if isinstance(row, dict):
                lines.append(
                    f"S.No: {html.escape(row.get('S.No', '-'))} | "
                    f"Date: {html.escape(row.get('Date', '-'))} | "
                    f"Held: {html.escape(row.get('Held', '-'))} | "
                    f"Attend: {html.escape(row.get('Attend', '-'))}"
                )

    course_changes = _course_attendance_changes(old_value.get("course_wise") or [], new_value.get("course_wise") or [])
    if course_changes:
        lines.append("\n<b>Course Updates</b>")
        lines.extend(course_changes)
    else:
        courses = new_value.get("course_wise") or []
        if courses:
            lines.append("\n<b>Course Attendance</b>")
            for row in courses:
                if isinstance(row, dict):
                    lines.append(_format_course_row(row))

    return "\n".join(lines)


def _course_attendance_changes(old_courses: list[Any], new_courses: list[Any]) -> list[str]:
    old_by_name = {
        str(row.get("Course Name", "")): row
        for row in old_courses
        if isinstance(row, dict) and row.get("Course Name")
    }
    lines: list[str] = []

    for row in new_courses:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Course Name", ""))
        old = old_by_name.get(name)
        if old is None:
            lines.append(_format_course_row(row, prefix="New: "))
            continue

        changed_bits = []
        for key in ["Held Cls", "PR", "AB", "Present%", "Loss%"]:
            if old.get(key) != row.get(key):
                changed_bits.append(f"{key}: {old.get(key, '-')} -> {row.get(key, '-')}")
        if changed_bits:
            lines.append(f"{html.escape(name)}\n" + html.escape(" | ".join(changed_bits)))

    return lines


def _format_course_row(row: dict[str, Any], prefix: str = "") -> str:
    name = str(row.get("Course Name", "-"))
    present = str(row.get("PR", "-"))
    absent = str(row.get("AB", "-"))
    percent = str(row.get("Present%", "-"))
    held = str(row.get("Held Cls", "-"))
    loss = str(row.get("Loss%", "-"))
    return (
        f"{html.escape(prefix + name)}\n"
        f"Present: {html.escape(present)} | Absent: {html.escape(absent)} | Held: {html.escape(held)} | "
        f"{html.escape(percent)}% | Loss: {html.escape(loss)}%"
    )
