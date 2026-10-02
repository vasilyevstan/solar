from __future__ import annotations

import asyncio
import fcntl
import logging
import math
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeoutError
from playwright.async_api import BrowserContext, Page, Playwright, Request, async_playwright

from .auth import keychain_credentials
from .models import DateRange, GenerationReport, StatsError, make_report, parse_month_rows

LOGGER = logging.getLogger(__name__)
REPORT_PATH = "/rest/pvms/web/report/v1/station/station-kpi-list"
ROW_SELECTOR = ".dpdesign-table-body tr[data-row-key]"


def plant_from_url(url: str) -> str | None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not (parsed.hostname or "").endswith(".fusionsolar.huawei.com")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        return None
    match = re.match(r"^/view/station/(NE=\d+)(?:/|$)", unquote(parsed.fragment))
    return match[1] if match else None


@dataclass(frozen=True)
class StatsConfig:
    plant_url: str
    profile_dir: Path
    timeout_seconds: float = 300
    browser_mode: Literal["attach", "managed"] = "attach"
    headless: bool = True
    keychain_service: str | None = None
    data_dir: Path = field(default_factory=lambda: Path("state/solar-stats").resolve())

    def __post_init__(self) -> None:
        try:
            valid = plant_from_url(self.plant_url)
            parsed = urlsplit(self.plant_url)
        except ValueError:
            raise StatsError("invalid_config", "SOLAR_STATS_PLANT_URL is not a valid URL.") from None
        if not valid or parsed.query or "?" in parsed.fragment:
            raise StatsError("invalid_config", "Set SOLAR_STATS_PLANT_URL to the plant page without query parameters.")
        if not self.profile_dir.is_absolute():
            raise StatsError("invalid_config", "SOLAR_STATS_PROFILE_DIR must be an absolute dedicated-profile path.")
        if not self.data_dir.is_absolute():
            raise StatsError("invalid_config", "SOLAR_STATS_DATA_DIR must be an absolute directory path.")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise StatsError("invalid_config", "SOLAR_STATS_TIMEOUT_SECONDS must be finite and positive.")
        if self.browser_mode not in {"attach", "managed"} or type(self.headless) is not bool:
            raise StatsError("invalid_config", "Use browser mode attach/managed and headless true/false.")
        if self.keychain_service is not None and (
            not self.keychain_service.strip() or any(ord(char) < 32 for char in self.keychain_service)
        ):
            raise StatsError("invalid_config", "SOLAR_STATS_KEYCHAIN_SERVICE must be a nonempty Keychain item name.")

    @property
    def plant_id(self) -> str:
        plant_id = plant_from_url(self.plant_url)
        if plant_id is None:
            raise StatsError("invalid_config", "The configured URL has no plant identity.")
        return plant_id

    def report_url(self, session_url: str) -> str:
        if not self.is_plant_page(session_url):
            raise StatsError("authentication_required", "Open the configured plant in the dedicated browser.")
        parsed = urlsplit(session_url)
        routing = [(key, value) for key, value in parse_qsl(parsed.query) if key in {"app-id", "instance-id", "zone-id"}]
        return urlunsplit(parsed._replace(
            query=urlencode(routing), fragment=f"/view/station/{self.plant_id}/report"
        ))

    def is_plant_page(self, url: str) -> bool:
        parsed = urlsplit(url)
        configured = urlsplit(self.plant_url)
        return (
            parsed.hostname == configured.hostname
            and parsed.path == configured.path
            and plant_from_url(url) == self.plant_id
        )

    def is_login_url(self, url: str) -> bool:
        parsed = urlsplit(url)
        configured_host = urlsplit(self.plant_url).hostname or ""
        login_host = re.sub(r"^uni\d+", "", configured_host)
        return (
            parsed.scheme == "https"
            and parsed.hostname in {configured_host, login_host}
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and parsed.path == "/unisso/login.action"
        )

    @property
    def login_url(self) -> str:
        host = re.sub(r"^uni\d+", "", urlsplit(self.plant_url).hostname or "")
        return urlunsplit(("https", host, "/unisso/login.action", "", f"/view/station/{self.plant_id}/report"))

    @classmethod
    def from_environment(cls) -> StatsConfig:
        url = os.environ.get("SOLAR_STATS_PLANT_URL", "")
        profile = os.environ.get("SOLAR_STATS_PROFILE_DIR", "")
        if not url or not profile:
            raise StatsError("invalid_config", "Set SOLAR_STATS_PLANT_URL and SOLAR_STATS_PROFILE_DIR locally.")
        try:
            timeout = float(os.environ.get("SOLAR_STATS_TIMEOUT_SECONDS", "300"))
        except ValueError as error:
            raise StatsError("invalid_config", "SOLAR_STATS_TIMEOUT_SECONDS must be a number.") from error
        mode = os.environ.get("SOLAR_STATS_BROWSER_MODE", "attach")
        headless = os.environ.get("SOLAR_STATS_HEADLESS", "true")
        if mode not in {"attach", "managed"} or headless not in {"true", "false"}:
            raise StatsError("invalid_config", "Use browser mode attach/managed and headless true/false.")
        return cls(
            url, Path(profile).expanduser(), timeout, "attach" if mode == "attach" else "managed", headless == "true",
            os.environ.get("SOLAR_STATS_KEYCHAIN_SERVICE"),
            Path(os.environ.get("SOLAR_STATS_DATA_DIR", "state/solar-stats")).expanduser().resolve(),
        )


@dataclass(frozen=True)
class ReportPage:
    total: int
    number: int
    size: int
    dates: list[str]


def validate_report_page(payload: object) -> ReportPage:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise StatsError("source_unavailable", "FusionSolar did not return a successful report.")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise StatsError("portal_changed", "The report response has no data object.")
    total, number, size = (data.get(name) for name in ("total", "pageNo", "pageSize"))
    if any(type(value) is not int for value in (total, number, size)):
        raise StatsError("portal_changed", "Report pagination metadata is invalid.")
    if not isinstance(total, int) or not isinstance(number, int) or not isinstance(size, int):
        raise StatsError("portal_changed", "Report pagination metadata is invalid.")
    rows = data.get("list")
    if total < 0 or number < 1 or size < 1 or not isinstance(rows, list):
        raise StatsError("portal_changed", "Report pagination is incomplete.")
    dates = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("fmtCollectTimeStr"), str):
            raise StatsError("portal_changed", "A report row has no calendar label.")
        dates.append(row["fmtCollectTimeStr"])
    expected = min(size, max(0, total - (number - 1) * size))
    if len(dates) != expected:
        raise StatsError("incomplete_source", "The report page is truncated.")
    return ReportPage(total, number, size, dates)


@asynccontextmanager
async def profile_lock(profile: Path) -> AsyncIterator[None]:
    if not profile.is_dir():
        raise StatsError("authentication_required", "The dedicated browser profile is unavailable; open it and sign in.")
    descriptor = os.open(profile / ".solar-stats.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.1)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def debugging_port(profile: Path) -> int:
    try:
        line = (profile / "DevToolsActivePort").read_text().splitlines()[0]
        port = int(line)
    except (OSError, ValueError, IndexError) as error:
        raise StatsError(
            "authentication_required",
            "Open the dedicated, signed-in Chrome profile with loopback remote debugging enabled.",
        ) from error
    if not 1 <= port <= 65535:
        raise StatsError("invalid_config", "The dedicated browser published an invalid debugging port.")
    return port


class FusionSolarSource:
    def __init__(self, config: StatsConfig) -> None:
        self.config = config
        self._lock = asyncio.Lock()

    async def fetch(self, interval: DateRange) -> GenerationReport:
        try:
            if self.config.keychain_service and (os.environ.get("DEBUG") or os.environ.get("PWDEBUG")):
                raise StatsError("unsafe_debug_config", "Unset DEBUG and PWDEBUG before enabling credential-based login.")
            async with asyncio.timeout(self.config.timeout_seconds):
                if self.config.browser_mode == "managed":
                    self.config.profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if self.config.profile_dir.stat().st_mode & 0o077:
                        raise StatsError("invalid_config", "The managed profile must be private (directory mode 0700).")
                async with self._lock, profile_lock(self.config.profile_dir):
                    return await self._fetch(interval)
        except TimeoutError as error:
            raise StatsError("timeout", "The live query timed out; no cached or partial result was substituted.") from error
        except BrowserError:
            raise StatsError(
                "browser_session_unavailable",
                "The report browser is unavailable or its interface changed. Install Chrome or sign in to the dedicated profile.",
            ) from None
        except OSError as error:
            raise StatsError("local_access_failed", f"Cannot access the browser session ({type(error).__name__}).") from error

    def _check_plant(self, page: Page) -> None:
        if not self.config.is_plant_page(page.url):
            raise StatsError("authentication_required", "The signed-in page is not the configured plant.")

    def _check_login_origin(self, page: Page) -> None:
        if not self.config.is_login_url(page.url):
            raise StatsError("unexpected_login_origin", "Refusing to enter credentials outside the configured login origin.")

    @asynccontextmanager
    async def _browser_context(self, driver: Playwright) -> AsyncIterator[BrowserContext]:
        if self.config.browser_mode == "attach":
            port = debugging_port(self.config.profile_dir)
            browser = await driver.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15000)
            if not browser.contexts:
                raise StatsError("authentication_required", "No authenticated browser context is available.")
            yield browser.contexts[0]
        else:
            context = await driver.chromium.launch_persistent_context(
                str(self.config.profile_dir), channel="chrome", headless=self.config.headless,
                timeout=20000, args=["--restore-last-session"],
            )
            try:
                yield context
            finally:
                try:
                    async with asyncio.timeout(5):
                        await context.close()
                except (BrowserError, TimeoutError):
                    raise StatsError("browser_cleanup_failed", "The owned browser did not close cleanly.") from None

    async def _portal_state(self, page: Page, *, after_login: bool = False) -> str:
        result = await page.wait_for_function(
            """afterLogin => {
                const visible = element => !!element && !!(element.offsetWidth || element.offsetHeight);
                const report = document.querySelector('#timeDimension')?.closest('.dpdesign-select-selector');
                if (visible(report)) return 'report';
                const challenge = ['#twoFactorVerifyDiv', '#verifyCodeArea',
                    'iframe[src*="captcha"]', '[id*="captcha" i]'];
                if (challenge.some(selector => [...document.querySelectorAll(selector)].some(visible)))
                    return 'challenge';
                if (afterLogin) {
                    const error = document.querySelector('#errorMessage');
                    if (visible(error) && error.textContent.trim()) return 'rejected';
                } else if (visible(document.querySelector('#username'))) return 'login';
                return false;
            }""",
            arg=after_login, timeout=30000,
        )
        try:
            state = await result.json_value()
            if not isinstance(state, str) or state not in {"report", "login", "challenge", "rejected"}:
                raise StatsError("portal_changed", "The browser returned an unrecognized authentication state.")
            return state
        finally:
            await result.dispose()

    async def _login_destination(self, page: Page, popup: asyncio.Future[Page]) -> tuple[Page, str]:
        original = asyncio.create_task(self._portal_state(page, after_login=True))
        try:
            completed, _ = await asyncio.wait({original, popup}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
            if popup in completed:
                destination = popup.result()
                destination.set_default_timeout(15000)
                await destination.wait_for_load_state("domcontentloaded")
                return destination, await self._portal_state(destination, after_login=True)
            if original in completed:
                return page, original.result()
            raise StatsError("authentication_required", "Login timed out; no login retry was attempted.")
        finally:
            original.cancel()
            await asyncio.gather(original, return_exceptions=True)

    async def _ensure_report(self, page: Page) -> Page:
        try:
            state = await self._portal_state(page)
        except BrowserTimeoutError:
            if (
                not self.config.keychain_service
                or not self.config.is_plant_page(page.url)
                or (await page.locator("body").inner_text()).strip()
            ):
                raise
            LOGGER.warning("The restored portal tab is blank; reopening the normal regional sign-in page once.")
            await page.goto(self.config.login_url, wait_until="domcontentloaded")
            state = await self._portal_state(page)
        if state == "report":
            self._check_plant(page)
            return page
        if state == "challenge":
            raise StatsError("authentication_required", "Complete the FusionSolar MFA/CAPTCHA challenge manually.")
        if not self.config.keychain_service:
            raise StatsError("authentication_required", "Sign in to the dedicated browser or configure Keychain login.")
        self._check_login_origin(page)
        credentials = await keychain_credentials(self.config.keychain_service)
        popup: asyncio.Future[Page] = asyncio.get_running_loop().create_future()
        opened: list[Page] = []
        accepted: Page | None = None

        def on_popup(child: Page) -> None:
            opened.append(child)
            if not popup.done():
                popup.set_result(child)

        page.on("popup", on_popup)
        try:
            self._check_login_origin(page)
            await page.locator("#username").fill(credentials.username)
            self._check_login_origin(page)
            await page.locator("#value").fill(credentials.password)
            self._check_login_origin(page)
            await page.locator("#submitDataverify").click()
            destination, state = await self._login_destination(page, popup)
            if state != "report":
                raise StatsError(
                    "authentication_required",
                    "FusionSolar rejected login or requires an interactive challenge; no login retry was attempted.",
                )
            self._check_plant(destination)
            accepted = destination
            return destination
        except BrowserError:
            raise StatsError(
                "authentication_required",
                "Login did not complete. Check credentials and any MFA/CAPTCHA challenge; no login retry was attempted.",
            ) from None
        finally:
            del credentials
            page.remove_listener("popup", on_popup)
            popup.cancel()
            for child in opened:
                if child is not accepted:
                    try:
                        async with asyncio.timeout(5):
                            await child.close()
                    except (BrowserError, TimeoutError):
                        LOGGER.warning("Could not close an owned login popup.")

    async def _fetch(self, interval: DateRange) -> GenerationReport:
        async with async_playwright() as driver, self._browser_context(driver) as context:
            seed = next(
                (tab for tab in context.pages if self.config.is_plant_page(tab.url)), None
            )
            if seed is None and self.config.browser_mode == "attach" and not self.config.keychain_service:
                raise StatsError("authentication_required", "Open the configured plant in the signed-in dedicated browser.")
            if self.config.browser_mode == "managed":
                page = seed or (context.pages[0] if context.pages else await context.new_page())
            elif seed is not None:
                # Native new-tab navigation retains tab-scoped login state without reading or exporting it.
                async with seed.expect_popup() as pending:
                    await seed.evaluate('(url) => { window.open(url, "_blank"); }', self.config.report_url(seed.url))
                page = await pending.value
            else:
                page = await context.new_page()
            page.set_default_timeout(15000)
            try:
                if seed is None or self.config.browser_mode == "managed":
                    target = self.config.report_url(seed.url) if seed is not None else self.config.plant_url
                    await page.goto(target, wait_until="domcontentloaded")
                else:
                    await page.wait_for_load_state("domcontentloaded")
                original = page
                page = await self._ensure_report(page)
                if page is not original:
                    await original.close()
                await self._granularity(page, "By day")
                today = DateRange.parse(await page.get_by_placeholder("Select date", exact=True).input_value()).start
                interval.check_not_future(today)
                await self._granularity(page, "By month")
                await page.get_by_placeholder("Select month", exact=True).wait_for(state="visible")
                size_selector = page.locator(".dpdesign-select-selector").filter(
                    has=page.get_by_role("combobox", name="Page Size")
                )
                await size_selector.wait_for(state="visible")
                if "100 / page" not in await size_selector.inner_text():
                    await size_selector.click()
                    await self._request(page, lambda: page.get_by_text("100 / page", exact=True).click())
                months: dict[tuple[int, int], dict[date, Decimal | None]] = {}
                for year, month in interval.months():
                    months[(year, month)] = await self._month(page, year, month)
                self._check_plant(page)
                return make_report(interval, self.config.plant_id, months)
            finally:
                try:
                    if self.config.browser_mode == "attach":
                        async with asyncio.timeout(5):
                            await page.close()
                except (BrowserError, TimeoutError):
                    LOGGER.warning("Could not close the owned report tab; the shared browser was not terminated.")

    async def _granularity(self, page: Page, value: str) -> None:
        selector = page.locator(".dpdesign-select-selector").filter(has=page.locator("#timeDimension"))
        if (await selector.inner_text()).strip() != value:
            await selector.click()
            await page.get_by_text(value, exact=True).click()

    async def _request(self, page: Page, action: Callable[[], Awaitable[None]]) -> ReportPage:
        def is_report(request: Request) -> bool:
            parsed = urlsplit(request.url)
            return parsed.path == REPORT_PATH and (parsed.hostname or "").endswith(".fusionsolar.huawei.com")

        async with page.expect_request(is_report) as pending:
            await action()
        response = await (await pending.value).response()
        self._check_plant(page)
        if response is None:
            raise StatsError("source_unavailable", "The report request ended without a response.")
        if response.status in {401, 403}:
            raise StatsError("authentication_required", "FusionSolar requires sign-in or report permission.")
        if response.status == 429:
            raise StatsError("rate_limited", "FusionSolar rate-limited the query; retry later.")
        if response.status != 200:
            raise StatsError("source_unavailable", f"The report request returned HTTP {response.status}.")
        try:
            payload = await response.json()
        except ValueError as error:
            raise StatsError("portal_changed", "The report response was not JSON.") from error
        return validate_report_page(payload)

    async def _month(self, page: Page, year: int, month: int) -> dict[date, Decimal | None]:
        picker = page.get_by_placeholder("Select month", exact=True)
        previous = await picker.input_value()
        await picker.click()
        await page.locator(".dpdesign-picker-year-btn:visible").click()
        cell = page.locator(f'.dpdesign-picker-cell[title="{year}"]:visible')
        displayed_year = int(previous[:4])
        while not await cell.count():
            direction = "prev" if year < displayed_year else "next"
            button = page.locator(f".dpdesign-picker-header-super-{direction}-btn:visible").first
            if not await button.is_enabled():
                raise StatsError("unavailable_period", "The portal does not allow selecting the requested year.")
            await button.click()
            displayed_year += -10 if direction == "prev" else 10
        if "disabled" in (await cell.get_attribute("class") or ""):
            raise StatsError("unavailable_period", "The selected year is disabled in the portal.")
        await cell.click()
        month_cell = page.locator(f'.dpdesign-picker-cell[title="{year}-{month:02d}"]:visible')
        if "disabled" in (await month_cell.get_attribute("class") or ""):
            raise StatsError("unavailable_period", "The selected month is disabled in the portal.")
        await month_cell.click()
        current = await self._request(page, lambda: page.get_by_text("Search", exact=True).click())
        if current.number != 1 or current.total > 31:
            raise StatsError("incomplete_source", "The monthly report did not start with a valid first page.")
        total, size = current.total, current.size
        collected: dict[date, Decimal | None] = {}
        while True:
            if current.total != total or current.size != size:
                raise StatsError("incomplete_source", "Report pagination changed during the query.")
            rows = await self._table(page, current.dates)
            parsed = parse_month_rows(*rows, year, month)
            if collected.keys() & parsed.keys():
                raise StatsError("incomplete_source", "The report repeated a page.")
            collected.update(parsed)
            if len(collected) == total:
                return collected
            next_page = page.locator(".dpdesign-pagination-next")
            if await next_page.get_attribute("aria-disabled") == "true":
                raise StatsError("incomplete_source", "The report ended before all rows were read.")
            previous_number = current.number
            current = await self._request(page, next_page.click)
            if current.number != previous_number + 1:
                raise StatsError("incomplete_source", "The report skipped a page.")

    async def _table(self, page: Page, expected_dates: list[str]) -> tuple[list[str], list[list[str]]]:
        headers = await page.locator(".dpdesign-table-header th").all_text_contents()
        normalized = [" ".join(text.split()) for text in headers]
        if normalized.count("Statistical Period") != 1:
            raise StatsError("portal_changed", "The report date column is missing or ambiguous.")
        index = normalized.index("Statistical Period")
        await page.wait_for_function(
            """({selector, expected, index}) => {
                const dates = [...document.querySelectorAll(selector)].map(
                    row => row.querySelectorAll('td')[index]?.innerText.trim());
                return JSON.stringify(dates) === JSON.stringify(expected);
            }""",
            arg={"selector": ROW_SELECTOR, "expected": expected_dates, "index": index},
        )
        rows = await page.locator(ROW_SELECTOR).evaluate_all(
            "rows => rows.map(row => [...row.querySelectorAll('td')].map(cell => cell.innerText.trim()))"
        )
        if not isinstance(rows, list) or any(
            not isinstance(row, list) or any(not isinstance(cell, str) for cell in row) for row in rows
        ):
            raise StatsError("portal_changed", "The rendered report table changed shape.")
        self._check_plant(page)
        return headers, rows
