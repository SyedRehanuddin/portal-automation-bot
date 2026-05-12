from __future__ import annotations

import logging
import json
import os
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait

from .config import AppConfig
from .storage import read_json, write_json


LOGGER = logging.getLogger(__name__)


class PortalBrowser:
    def __init__(self, config: AppConfig, captcha_handler: Callable[["PortalBrowser"], str] | None = None) -> None:
        self.config = config
        self.captcha_handler = captcha_handler
        self.driver: webdriver.Chrome | None = None
        self.cookies_file = config.resolve_path("cookies_file")
        self.downloads_dir = config.resolve_path("downloads_dir")

    def __enter__(self) -> "PortalBrowser":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> None:
        self.close()

    def start(self) -> None:
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        options = Options()
        options.add_argument(f"--window-size={self.config.browser.get('window_width', 1366)},{self.config.browser.get('window_height', 900)}")
        options.add_experimental_option(
            "prefs",
            {
                "download.default_directory": str(self.downloads_dir),
                "download.prompt_for_download": False,
                "plugins.always_open_pdf_externally": True,
            },
        )
        if self.config.browser.get("headless_after_login") is True and not _headless_enabled():
            LOGGER.warning("headless_after_login is configured, but CAPTCHA login requires a visible browser. Starting visible Chrome.")
        _configure_chrome_options(options)

        self.driver = webdriver.Chrome(options=options)
        self.driver.set_page_load_timeout(int(self.config.browser.get("page_load_timeout_seconds", 45)))

    def close(self) -> None:
        if self.driver is not None:
            self.driver.quit()
            self.driver = None

    @property
    def wait(self) -> WebDriverWait:
        return WebDriverWait(self.driver_or_raise, 20)

    @property
    def driver_or_raise(self) -> webdriver.Chrome:
        if self.driver is None:
            raise RuntimeError("Browser is not started")
        return self.driver

    def ensure_logged_in(self) -> None:
        if self._has_active_session():
            return
        if not self._has_valid_cookie_session():
            LOGGER.info("Saved portal cookies are missing or expired according to requests validation.")
        elif self._try_cookie_login():
            return
        else:
            LOGGER.info("Saved portal cookies passed requests validation but failed Selenium login.")
        if _headless_enabled():
            if self.captcha_handler is None:
                raise RuntimeError("Saved portal cookies are missing or expired. Run locally once with visible Chrome to refresh data/cookies.json, then redeploy or provide persistent storage.")
            self.telegram_captcha_login()
            return
        self.manual_login()

    def open_authenticated(self, url: str) -> None:
        self.ensure_logged_in()
        self.driver_or_raise.get(url)
        self.dismiss_popups()
        time.sleep(1)
        if self.is_session_expired():
            LOGGER.info("Session expired while opening %s; logging in again.", url)
            self.ensure_logged_in()
            self.driver_or_raise.get(url)
            self.dismiss_popups()

    def open_first_matching_link(self, keywords: list[str]) -> bool:
        self.ensure_logged_in()
        lowered_keywords = [keyword.lower() for keyword in keywords]

        for link in self.driver_or_raise.find_elements(By.CSS_SELECTOR, "a[href]"):
            text = f"{link.text} {link.get_attribute('href') or ''}".lower()
            if any(keyword in text for keyword in lowered_keywords):
                href = link.get_attribute("href")
                if href:
                    target_url = urljoin(self.driver_or_raise.current_url, href)
                    LOGGER.info("Opening discovered portal link: %s", target_url)
                    self.open_authenticated(target_url)
                    return True
        return False

    def is_session_expired(self) -> bool:
        current_url = (self.driver_or_raise.current_url or "").lower()
        login_url = self.config.portal["login_url"].lower()
        if "login" in current_url or current_url.startswith(login_url):
            return True

        login_selectors = self.config.selectors["login"]
        enrollment_selector = login_selectors.get("enrollment_input")
        password_selector = login_selectors.get("password_input")
        if enrollment_selector and self._has_element(enrollment_selector):
            return True
        if password_selector and self._has_element(password_selector):
            return True
        return False

    def _has_active_session(self) -> bool:
        try:
            current_url = self.driver_or_raise.current_url or ""
            if not current_url or current_url == "data:,":
                return False
            return not self.is_session_expired()
        except WebDriverException:
            return False

    def manual_login(self) -> None:
        driver = self.driver_or_raise
        login = self.config.selectors["login"]
        LOGGER.info("Opening login page for manual CAPTCHA login.")
        driver.get(self.config.portal["login_url"])
        self.dismiss_popups()

        self._type_if_present(login["enrollment_input"], self.config.credentials.enrollment_number)
        self._type_if_present(login["password_input"], self.config.credentials.password)

        captcha_selector = login.get("captcha_input")
        if captcha_selector:
            LOGGER.info("Waiting for manual CAPTCHA solving. Complete CAPTCHA in Chrome.")
            try:
                captcha = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, captcha_selector)))
                self.dismiss_popups()
                captcha.click()
            except TimeoutException:
                LOGGER.warning("CAPTCHA input was not found. Continuing; portal may use a different CAPTCHA layout.")

        timeout = int(self.config.browser.get("manual_captcha_timeout_seconds", 180))
        self._click_submit_when_ready(login.get("submit_button"), timeout)
        self._wait_until_logged_in(timeout)
        self.save_cookies()

    def telegram_captcha_login(self) -> None:
        if self.captcha_handler is None:
            raise RuntimeError("Telegram CAPTCHA login was requested without a CAPTCHA handler.")

        driver = self.driver_or_raise
        login = self.config.selectors["login"]
        captcha_selector = login.get("captcha_input")
        if not captcha_selector:
            raise RuntimeError("CAPTCHA selector is not configured.")

        timeout = int(self.config.browser.get("manual_captcha_timeout_seconds", 300))
        max_attempts = int(self.config.browser.get("captcha_max_attempts", 3))
        last_error: BaseException | None = None

        for attempt in range(1, max_attempts + 1):
            LOGGER.info("Opening login page for Telegram-assisted CAPTCHA login attempt %d.", attempt)
            driver.get(self.config.portal["login_url"])
            self.dismiss_popups()
            self._type_if_present(login["enrollment_input"], self.config.credentials.enrollment_number)
            self._type_if_present(login["password_input"], self.config.credentials.password)

            captcha = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, captcha_selector)))
            self.dismiss_popups()
            captcha.click()
            captcha_code = self.captcha_handler(self, attempt, max_attempts).strip()
            if not captcha_code:
                raise RuntimeError("Empty CAPTCHA was provided.")
            captcha.clear()
            captcha.send_keys(captcha_code)

            self._click_submit_when_ready(login.get("submit_button"), timeout)
            try:
                self._wait_until_logged_in(timeout)
                self.save_cookies()
                return
            except (RuntimeError, TimeoutError) as exc:
                last_error = exc
                LOGGER.warning("Telegram CAPTCHA login attempt %d failed: %s", attempt, exc)
                if attempt < max_attempts:
                    continue
                break

        raise RuntimeError(f"Telegram CAPTCHA login failed after {max_attempts} attempts: {last_error}")

    def save_login_screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.driver_or_raise.save_screenshot(str(path))
        return path

    def save_cookies(self) -> None:
        cookies = _dedupe_cookies(self.driver_or_raise.get_cookies())
        write_json(self.cookies_file, cookies)
        LOGGER.info("Saved %d cookies to %s", len(cookies), self.cookies_file)

    def dismiss_popups(self, max_passes: int = 3) -> None:
        driver = self.driver_or_raise
        for _ in range(max_passes):
            closed_any = False
            closed_any = self._close_visible_popup_buttons() or closed_any
            closed_any = self._close_popup_backdrops() or closed_any
            if not closed_any:
                break
            time.sleep(0.5)

    def _try_cookie_login(self) -> bool:
        cookies = _load_cookies(self.cookies_file)
        if not cookies:
            return False

        driver = self.driver_or_raise
        try:
            driver.get(self.config.portal["base_url"])
            for cookie in cookies:
                cookie = dict(cookie)
                cookie.pop("sameSite", None)
                try:
                    driver.add_cookie(cookie)
                except WebDriverException as exc:
                    retry_cookie = dict(cookie)
                    retry_cookie.pop("domain", None)
                    try:
                        driver.add_cookie(retry_cookie)
                    except WebDriverException:
                        LOGGER.debug("Skipping cookie %s: %s", cookie.get("name"), exc)
            driver.get(self._session_check_url())
            time.sleep(2)
            if not self.is_session_expired():
                LOGGER.info("Logged in using saved cookies.")
                return True
        except WebDriverException as exc:
            LOGGER.warning("Cookie login failed: %s", exc)

        return False

    def _has_valid_cookie_session(self) -> bool:
        return validate_cookie_session(self.config, self.cookies_file)

    def _wait_until_logged_in(self, timeout: int) -> None:
        indicator = self.config.selectors["login"].get("logged_in_indicator")
        error_selector = self.config.selectors["login"].get("login_error")
        end_at = time.time() + timeout

        while time.time() < end_at:
            self.dismiss_popups()
            if indicator and self._has_element(indicator):
                LOGGER.info("Login indicator found.")
                return
            if not self.is_session_expired():
                LOGGER.info("Browser no longer appears to be on login page.")
                return
            if error_selector and self._has_element(error_selector):
                error_text = self.driver_or_raise.find_element(By.CSS_SELECTOR, error_selector).text.strip()
                raise RuntimeError(f"Login failed: {error_text or 'portal displayed an error'}")
            time.sleep(2)

        raise TimeoutError("Login timed out. Check CAPTCHA, credentials, and login selectors.")

    def _click_submit_when_ready(self, submit_selector: str | None, timeout: int) -> None:
        if not submit_selector:
            LOGGER.info("No submit selector configured. Please submit the login form manually.")
            return

        captcha_selector = self.config.selectors["login"].get("captcha_input")
        end_at = time.time() + timeout
        while time.time() < end_at:
            if not self.is_session_expired():
                return

            self.dismiss_popups()

            if captcha_selector:
                try:
                    value = self.driver_or_raise.find_element(By.CSS_SELECTOR, captcha_selector).get_attribute("value") or ""
                    if not value.strip():
                        time.sleep(1)
                        continue
                except WebDriverException:
                    pass

            try:
                button = self.driver_or_raise.find_element(By.CSS_SELECTOR, submit_selector)
                if button.is_displayed() and button.is_enabled():
                    button.click()
                    return
            except WebDriverException:
                pass
            time.sleep(1)

        LOGGER.info("Submit button was not clicked automatically. You can submit the form manually.")

    def _type_if_present(self, selector: str, value: str) -> None:
        element = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
        self.dismiss_popups()
        element.clear()
        element.send_keys(value)

    def _has_element(self, selector: str) -> bool:
        try:
            return bool(self.driver_or_raise.find_elements(By.CSS_SELECTOR, selector))
        except WebDriverException:
            return False

    def _session_check_url(self) -> str:
        return _session_check_url(self.config)

    def _close_visible_popup_buttons(self) -> bool:
        driver = self.driver_or_raise
        selectors = [
            ".modal.show .close",
            ".modal.show [data-dismiss='modal']",
            ".modal.show .btn-close",
            ".modal.show button[aria-label='Close']",
            ".modal.show button.close",
            ".modal.show .fa-times",
            ".modal.show .bi-x",
            ".modal.show .bi-x-lg",
            ".modal.show [class*='close']",
        ]
        seen: set[str] = set()
        for selector in selectors:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    element_id = element.id
                except WebDriverException:
                    continue
                if element_id in seen:
                    continue
                seen.add(element_id)
                try:
                    if not element.is_displayed():
                        continue
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
                    driver.execute_script("arguments[0].click();", element)
                    LOGGER.info("Closed popup using selector %s", selector)
                    return True
                except WebDriverException:
                    continue

        for element in driver.find_elements(By.CSS_SELECTOR, ".modal.show button, .modal.show a, .modal.show span"):
            try:
                if not element.is_displayed():
                    continue
                label = " ".join(
                    [
                        element.text or "",
                        element.get_attribute("aria-label") or "",
                        element.get_attribute("title") or "",
                        element.get_attribute("class") or "",
                    ]
                ).strip().lower()
                if not any(token in label for token in ("close", "dismiss", "times", "cross", "x")):
                    continue
                driver.execute_script("arguments[0].click();", element)
                LOGGER.info("Closed popup using text/class match.")
                return True
            except WebDriverException:
                continue
        return False

    def _close_popup_backdrops(self) -> bool:
        driver = self.driver_or_raise
        for backdrop in driver.find_elements(By.CSS_SELECTOR, ".modal-backdrop.show, .modal.show"):
            try:
                if not backdrop.is_displayed():
                    continue
                ActionChains(driver).move_to_element_with_offset(backdrop, 5, 5).click().perform()
                LOGGER.info("Clicked popup backdrop.")
                return True
            except WebDriverException:
                continue
        return False


def _dedupe_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cookie in cookies:
        key = (cookie.get("name", ""), cookie.get("domain", ""), cookie.get("path", ""))
        unique[key] = cookie
    return list(unique.values())


def _load_cookies(path: Path) -> list[dict[str, Any]]:
    cookies = read_json(path, [])
    if isinstance(cookies, list) and cookies:
        return cookies

    raw_cookies = os.getenv("PORTAL_COOKIES_JSON", "").strip()
    if not raw_cookies:
        return []

    try:
        env_cookies = json.loads(raw_cookies)
    except json.JSONDecodeError:
        LOGGER.warning("PORTAL_COOKIES_JSON is not valid JSON.")
        return []

    return env_cookies if isinstance(env_cookies, list) else []


def _requests_cookie_jar(cookies: list[dict[str, Any]]) -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue
        jar.set(
            str(name),
            str(value),
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )
    return jar


def validate_cookie_session(config: AppConfig, cookies_file: Path | None = None) -> bool | None:
    cookies = _load_cookies(cookies_file or config.resolve_path("cookies_file"))
    if not cookies:
        return False

    check_url = _session_check_url(config)
    try:
        response = requests.get(
            check_url,
            cookies=_requests_cookie_jar(cookies),
            allow_redirects=True,
            timeout=int(config.browser.get("session_check_timeout_seconds", 15)),
        )
    except requests.RequestException as exc:
        LOGGER.info("Requests cookie validation failed: %s", exc)
        return None

    if response.status_code >= 500:
        LOGGER.info("Session check returned %s; falling back to Selenium validation.", response.status_code)
        return None

    if _looks_like_login_page(response.url, response.text, config):
        return False

    return response.ok


def _session_check_url(config: AppConfig) -> str:
    enabled_sections = config.monitoring.get("enabled_sections", ["attendance", "marks"])
    for section in enabled_sections:
        url = config.portal.get(f"{section}_url")
        if url:
            return url
    return config.portal["base_url"]


def _looks_like_login_page(url: str, html: str, config: AppConfig) -> bool:
    lowered_url = url.lower()
    login_url = config.portal["login_url"].lower()
    if "login" in lowered_url or lowered_url.startswith(login_url):
        return True

    lowered_html = html.lower()
    login_selectors = config.selectors["login"]
    markers = [
        "student_login",
        "captcha",
        "user_password",
        "user_id",
        "name=\"submit\"",
        "id=\"token\"",
    ]
    configured_markers = [
        str(login_selectors.get("enrollment_input", "")).lstrip("#.").lower(),
        str(login_selectors.get("password_input", "")).lstrip("#.").lower(),
        str(login_selectors.get("captcha_input", "")).lstrip("#.").lower(),
    ]
    return any(marker and marker in lowered_html for marker in [*markers, *configured_markers])


def _configure_chrome_options(options: Options) -> None:
    chrome_bin = os.getenv("CHROME_BIN") or os.getenv("CHROME_BINARY_PATH")
    if chrome_bin:
        options.binary_location = chrome_bin

    if _headless_enabled():
        options.add_argument("--headless=new")

    if _cloud_chrome_enabled():
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--remote-debugging-port=9222")


def _headless_enabled() -> bool:
    value = os.getenv("SELENIUM_HEADLESS", "").strip().lower()
    if value:
        return value in {"1", "true", "yes", "on"}
    return _cloud_chrome_enabled()


def _cloud_chrome_enabled() -> bool:
    return os.getenv("RENDER", "").strip().lower() == "true" or os.getenv("SELENIUM_CLOUD", "").strip().lower() in {"1", "true", "yes", "on"}
