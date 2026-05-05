from __future__ import annotations

from typing import Any

from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.common.by import By

from .browser import PortalBrowser


class PortalExtractor:
    def __init__(self, browser: PortalBrowser) -> None:
        self.browser = browser

    def collect_all(self) -> dict[str, Any]:
        enabled_sections = set(self.browser.config.monitoring.get("enabled_sections", ["attendance", "marks"]))
        data: dict[str, Any] = {}

        if "attendance" in enabled_sections:
            data["attendance"] = self.extract_attendance()
        if "marks" in enabled_sections:
            data["marks"] = self.extract_table_page("marks")
        return data

    def extract_attendance(self) -> dict[str, Any]:
        config = self.browser.config
        self.browser.open_authenticated(config.portal["attendance_url"])

        tables = self.browser.driver_or_raise.find_elements(By.CSS_SELECTOR, config.selectors["attendance"].get("table", "table"))
        result: dict[str, Any] = {
            "overall_percent": self._extract_overall_attendance_percent(),
            "last_week": [],
            "course_wise": [],
        }

        for table in tables:
            table_text = table.text.strip()
            lowered = table_text.lower()

            if "s.no" in lowered and "date" in lowered and "attend" in lowered:
                row_limit = int(config.raw.get("attendance_options", {}).get("last_week_rows", 3))
                result["last_week"] = _parse_last_week_attendance(table_text)[:row_limit]
            elif "course name" in lowered and "present%" in lowered:
                result["course_wise"] = _parse_course_wise_attendance(table_text)

        if not result["last_week"] and not result["course_wise"]:
            raise RuntimeError("Attendance tables were not found on the dashboard. Check attendance selectors.")

        return result

    def _extract_overall_attendance_percent(self) -> str | None:
        driver = self.browser.driver_or_raise
        for element in driver.find_elements(By.CSS_SELECTOR, "div, section, article"):
            text = " ".join(element.text.split())
            if not text or "Attendance %" not in text:
                continue
            parts = text.split()
            for index, part in enumerate(parts):
                if part.lower() == "attendance" and index > 0:
                    candidate = parts[index - 1].strip()
                    if _looks_numeric(candidate):
                        return candidate
        return None

    def extract_table_page(self, section: str) -> list[dict[str, str]] | list[list[str]]:
        config = self.browser.config
        url = config.portal[f"{section}_url"]
        selectors = config.selectors[section]
        self.browser.open_authenticated(url)

        table_selector = selectors.get("table", "table")
        row_selector = selectors.get("row", "tbody tr")
        cell_selector = selectors.get("cell", "td")
        header_selector = selectors.get("header", "thead th")

        driver = self.browser.driver_or_raise
        table = self._find_table_or_discover_section(section, table_selector)
        headers = [element.text.strip() for element in table.find_elements(By.CSS_SELECTOR, header_selector)]
        headers = [header for header in headers if header]

        rows: list[list[str]] = []
        for row in table.find_elements(By.CSS_SELECTOR, row_selector):
            cells = [cell.text.strip() for cell in row.find_elements(By.CSS_SELECTOR, cell_selector)]
            cells = [cell for cell in cells if cell]
            if cells:
                rows.append(cells)

        if headers and all(len(row) == len(headers) for row in rows):
            return [dict(zip(headers, row)) for row in rows]
        return rows

    def _find_table_or_discover_section(self, section: str, table_selector: str) -> Any:
        driver = self.browser.driver_or_raise
        tables = driver.find_elements(By.CSS_SELECTOR, table_selector)
        if tables:
            return tables[0]

        if self._discover_section(section):
            tables = driver.find_elements(By.CSS_SELECTOR, table_selector)
            if tables:
                return tables[0]

        raise RuntimeError(
            f"No table found for {section}. Update {section}_url or selectors.{section}.table in config.json."
        )

    def _discover_section(self, section: str) -> bool:
        keywords = self.browser.config.raw.get("link_keywords", {}).get(section, [])
        if not keywords:
            return False
        return self.browser.open_first_matching_link(keywords)


def _row_cells(row: WebElement, cell_selector: str) -> list[str]:
    return [cell.text.strip() for cell in row.find_elements(By.CSS_SELECTOR, cell_selector) if cell.text.strip()]


def _table_headers(table: WebElement) -> list[str]:
    headers = [header.text.strip() for header in table.find_elements(By.CSS_SELECTOR, "thead th")]
    headers = [header for header in headers if header]
    if headers:
        return headers
    first_row = table.find_elements(By.CSS_SELECTOR, "tr")
    if not first_row:
        return []
    return [cell.text.strip() for cell in first_row[0].find_elements(By.CSS_SELECTOR, "th,td") if cell.text.strip()]


def _table_rows(table: WebElement, cell_selector: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.find_elements(By.CSS_SELECTOR, "tbody tr"):
        cells = _row_cells(row, cell_selector)
        if cells:
            rows.append(cells)
    if rows:
        return rows

    all_rows = table.find_elements(By.CSS_SELECTOR, "tr")
    for row in all_rows[1:]:
        cells = [cell.text.strip() for cell in row.find_elements(By.CSS_SELECTOR, "td") if cell.text.strip()]
        if cells:
            rows.append(cells)
    return rows


def _rows_as_dicts(headers: list[str], rows: list[list[str]]) -> list[dict[str, str]] | list[list[str]]:
    clean_headers = [header.replace("\n", " ").strip() for header in headers]
    if clean_headers and all(len(row) == len(clean_headers) for row in rows):
        return [dict(zip(clean_headers, row)) for row in rows]
    return rows


def _parse_last_week_attendance(table_text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in table_text.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0].isdigit() and "-" in parts[1]:
            rows.append(
                {
                    "S.No": parts[0],
                    "Date": parts[1],
                    "Held": parts[2],
                    "Attend": parts[3],
                }
            )
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
