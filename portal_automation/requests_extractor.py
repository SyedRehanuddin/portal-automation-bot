from __future__ import annotations

import re
from typing import Any

import requests

from .browser import _load_cookies, _looks_like_login_page, _requests_cookie_jar
from .config import AppConfig


class RequestsPortalExtractor:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.session = requests.Session()
        cookies = _load_cookies(config.resolve_path("cookies_file"))
        if cookies:
            self.session.cookies.update(_requests_cookie_jar(cookies))

    def collect_all(self) -> dict[str, Any]:
        enabled_sections = set(self.config.monitoring.get("enabled_sections", ["attendance", "marks"]))
        data: dict[str, Any] = {}

        if "attendance" in enabled_sections:
            data["attendance"] = self.extract_attendance()
        if "marks" in enabled_sections:
            data["marks"] = self.extract_table_page("marks")
        return data

    def extract_attendance(self) -> dict[str, Any]:
        html = self._get_authenticated_html(self.config.portal["attendance_url"])
        tables = _extract_tables(html)
        page_text = _html_text(html)
        result: dict[str, Any] = {
            "overall_percent": _extract_overall_attendance_percent(page_text),
            "last_week": [],
            "course_wise": [],
        }

        for rows in tables:
            table_text = "\n".join(" ".join(row) for row in rows)
            lowered = table_text.lower()
            if "s.no" in lowered and "date" in lowered and "attend" in lowered:
                row_limit = int(self.config.raw.get("attendance_options", {}).get("last_week_rows", 3))
                result["last_week"] = _parse_last_week_attendance(table_text)[:row_limit]
            elif "course name" in lowered and "present%" in lowered:
                result["course_wise"] = _parse_course_wise_attendance(table_text)

        if not result["last_week"] and not result["course_wise"]:
            raise RuntimeError("Requests attendance extraction did not find attendance tables.")
        return result

    def extract_table_page(self, section: str) -> list[dict[str, str]] | list[list[str]]:
        html = self._get_authenticated_html(self.config.portal[f"{section}_url"])
        tables = _extract_tables(html)
        if not tables:
            raise RuntimeError(f"Requests {section} extraction did not find any tables.")

        rows = tables[0]
        if len(rows) < 2:
            return rows
        headers = [cell.replace("\n", " ").strip() for cell in rows[0] if cell.strip()]
        body = [[cell for cell in row if cell.strip()] for row in rows[1:]]
        body = [row for row in body if row]
        if headers and all(len(row) == len(headers) for row in body):
            return [dict(zip(headers, row)) for row in body]
        return body

    def _get_authenticated_html(self, url: str) -> str:
        response = self.session.get(
            url,
            allow_redirects=True,
            timeout=int(self.config.browser.get("requests_timeout_seconds", 20)),
        )
        response.raise_for_status()
        if _looks_like_login_page(response.url, response.text, self.config):
            raise RuntimeError("Requests session is not authenticated.")
        return response.text


def _extract_tables(html: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    for table_html in re.findall(r"<table\b.*?</table>", html, flags=re.IGNORECASE | re.DOTALL):
        rows: list[list[str]] = []
        for row_html in re.findall(r"<tr\b.*?</tr>", table_html, flags=re.IGNORECASE | re.DOTALL):
            cells = re.findall(r"<t[dh]\b.*?</t[dh]>", row_html, flags=re.IGNORECASE | re.DOTALL)
            row = [_html_text(cell) for cell in cells]
            row = [cell for cell in row if cell]
            if row:
                rows.append(row)
        if rows:
            tables.append(rows)
    return tables


def _html_text(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#039;", "'")
        .replace("&quot;", '"')
    )
    return re.sub(r"\s+", " ", text).strip()


def _extract_overall_attendance_percent(text: str) -> str | None:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*Attendance\s*%", text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _parse_last_week_attendance(table_text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in table_text.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0].isdigit() and "-" in parts[1]:
            rows.append({"S.No": parts[0], "Date": parts[1], "Held": parts[2], "Attend": parts[3]})
    return rows


def _parse_course_wise_attendance(table_text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in table_text.splitlines():
        parts = line.split()
        if len(parts) < 9 or not parts[0].isdigit():
            continue
        numeric_tail = parts[-6:]
        if not all(_looks_numeric(value) for value in numeric_tail):
            continue
        rows.append(
            {
                "S.No": parts[0],
                "Course Name": " ".join(parts[1:-6]),
                "LTP Cls": numeric_tail[0],
                "Held Cls": numeric_tail[1],
                "PR": numeric_tail[2],
                "AB": numeric_tail[3],
                "Present%": numeric_tail[4],
                "Loss%": numeric_tail[5],
            }
        )
    return rows


def _looks_numeric(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False
