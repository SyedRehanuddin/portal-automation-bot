from __future__ import annotations

import base64
import hashlib
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.common.by import By

from .browser import PortalBrowser


LOGGER = logging.getLogger(__name__)


class PortalExtractor:
    def __init__(self, browser: PortalBrowser) -> None:
        self.browser = browser

    def collect_all(self) -> dict[str, Any]:
        enabled_sections = set(self.browser.config.monitoring.get("enabled_sections", ["attendance", "marks", "memo"]))
        data: dict[str, Any] = {}

        if "attendance" in enabled_sections:
            data["attendance"] = self.extract_attendance()
        if "marks" in enabled_sections:
            data["marks"] = self.extract_table_page("marks")
        if "memo" in enabled_sections:
            data["memo"] = self.extract_memo()

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

    def extract_memo(self) -> dict[str, Any]:
        config = self.browser.config
        selectors = config.selectors["memo"]
        self.browser.open_authenticated(config.portal["memo_url"])
        driver = self.browser.driver_or_raise

        target = config.raw.get("memo_target", {})
        target_row = self._find_target_memo_row()
        if target and target_row is None:
            rows = self._memo_rows_preview()
            return {
                "target": target,
                "available": False,
                "status": "Target memo row not available yet",
                "matching_row": None,
                "rows_seen": rows,
            }

        if target_row is not None:
            clicked = self._click_print_memo_in_row(target_row)
            if not clicked:
                return {
                    "target": target,
                    "available": True,
                    "status": "Target memo row found, but Print Memo control was not found in that row",
                    "matching_row": _row_cells(target_row, selectors.get("cell", "td")),
                }
            downloaded_file = self._save_current_memo_as_pdf(target)
        else:
            self._click_print_memo_if_present()
            downloaded_file = None

        pdf_selector = selectors.get("pdf_links", "a[href$='.pdf'], a[href*='.pdf']")
        links: list[dict[str, str]] = []

        for link in driver.find_elements(By.CSS_SELECTOR, pdf_selector):
            href = link.get_attribute("href")
            if href:
                links.append(
                    {
                        "text": link.text.strip() or "PDF",
                        "url": urljoin(driver.current_url, href),
                    }
                )

        page_text = ""
        availability_selector = selectors.get("availability_text", "body")
        try:
            page_text = driver.find_element(By.CSS_SELECTOR, availability_selector).text.strip()
        except WebDriverException as exc:
            LOGGER.warning("Could not read memo availability text: %s", exc)

        return {
            "target": target,
            "available": bool(links) or _looks_available(page_text),
            "matching_row": _row_cells(target_row, selectors.get("cell", "td")) if target_row is not None else None,
            "downloaded_file": str(downloaded_file) if downloaded_file else None,
            "pdf_links": links,
            "page_fingerprint": _fingerprint(page_text),
            "summary": page_text[:1000],
        }

    def _find_target_memo_row(self) -> WebElement | None:
        target = self.browser.config.raw.get("memo_target", {})
        if not target:
            return None

        selectors = self.browser.config.selectors["memo"]
        rows = self.browser.driver_or_raise.find_elements(By.CSS_SELECTOR, selectors.get("row", "tbody tr"))
        for row in rows:
            cells = _row_cells(row, selectors.get("cell", "td"))
            if _memo_row_matches_target(cells, target):
                return row
        return None

    def _memo_rows_preview(self, max_rows: int = 10) -> list[list[str]]:
        selectors = self.browser.config.selectors["memo"]
        rows = self.browser.driver_or_raise.find_elements(By.CSS_SELECTOR, selectors.get("row", "tbody tr"))
        return [_row_cells(row, selectors.get("cell", "td")) for row in rows[:max_rows]]

    def _click_print_memo_in_row(self, row: WebElement) -> bool:
        selectors = self.browser.config.selectors["memo"]
        print_selector = selectors.get("print_memo", "a, button, input[type='button'], input[type='submit']")
        driver = self.browser.driver_or_raise
        old_windows = set(driver.window_handles)
        for element in row.find_elements(By.CSS_SELECTOR, print_selector):
            if _looks_like_print_memo_control(element):
                try:
                    element.click()
                    time.sleep(2)
                    self._switch_to_newest_window(old_windows)
                    LOGGER.info("Clicked target row Print Memo control.")
                    return True
                except WebDriverException as exc:
                    LOGGER.warning("Could not click target row Print Memo control: %s", exc)
                    return False
        return False

    def _click_print_memo_if_present(self) -> None:
        selectors = self.browser.config.selectors["memo"]
        print_selector = selectors.get("print_memo")
        if not print_selector:
            return

        driver = self.browser.driver_or_raise
        original_window = driver.current_window_handle
        original_windows = set(driver.window_handles)

        for element in driver.find_elements(By.CSS_SELECTOR, print_selector):
            if not _looks_like_print_memo_control(element):
                continue

            try:
                element.click()
                time.sleep(2)
                self._switch_to_newest_window(original_windows)
                LOGGER.info("Clicked Print Memo control.")
                return
            except WebDriverException as exc:
                LOGGER.warning("Could not click Print Memo control: %s", exc)
                if original_window in driver.window_handles:
                    driver.switch_to.window(original_window)
                return

    def _switch_to_newest_window(self, old_windows: set[str] | None = None) -> None:
        driver = self.browser.driver_or_raise
        if old_windows is None:
            old_windows = set()
        new_windows = [handle for handle in driver.window_handles if handle not in old_windows]
        if new_windows:
            driver.switch_to.window(new_windows[-1])

    def _save_current_memo_as_pdf(self, target: dict[str, Any]) -> Path:
        downloads_dir = self.browser.downloads_dir
        downloads_dir.mkdir(parents=True, exist_ok=True)
        description = str(target.get("description") or "semester_memo")
        safe_name = "_".join(description.lower().replace("/", " ").split())
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = downloads_dir / f"{safe_name}_{timestamp}.pdf"

        driver = self.browser.driver_or_raise
        try:
            pdf = driver.execute_cdp_cmd(
                "Page.printToPDF",
                {
                    "printBackground": True,
                    "landscape": False,
                    "paperWidth": 8.27,
                    "paperHeight": 11.69,
                },
            )
            output_path.write_bytes(base64.b64decode(pdf["data"]))
            LOGGER.info("Saved target memo PDF to %s", output_path)
            return output_path
        except WebDriverException as exc:
            raise RuntimeError(f"Target memo found, but PDF save failed: {exc}") from exc

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


def _looks_available(text: str) -> bool:
    lowered = text.lower()
    unavailable_phrases = [
        "not available",
        "no memo",
        "not released",
        "coming soon",
        "no record found",
    ]
    available_phrases = [
        "memo",
        "marksheet",
        "semester result",
        "download",
        "pdf",
    ]
    if any(phrase in lowered for phrase in unavailable_phrases):
        return False
    return any(phrase in lowered for phrase in available_phrases)


def _fingerprint(text: str) -> str:
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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


def _memo_row_matches_target(cells: list[str], target: dict[str, Any]) -> bool:
    normalized_cells = [_normalize(cell) for cell in cells]
    row_text = " ".join(normalized_cells)

    year = str(target.get("year", "")).strip().lower()
    semester = str(target.get("semester", "")).strip().lower()
    keywords = [str(keyword).strip().lower() for keyword in target.get("exam_session_keywords", [])]

    if year and year not in normalized_cells:
        return False
    if semester and semester not in normalized_cells:
        return False
    if keywords and not any(keyword in row_text for keyword in keywords):
        return False
    return True


def _looks_like_print_memo_control(element: WebElement) -> bool:
    label = " ".join(
        [
            element.text or "",
            element.get_attribute("value") or "",
            element.get_attribute("title") or "",
            element.get_attribute("aria-label") or "",
            element.get_attribute("href") or "",
        ]
    ).lower()
    return "print" in label and "memo" in label


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


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
