from __future__ import annotations

import asyncio
import fcntl
import logging
import math
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from playwright.async_api import Error as BrowserError
from playwright.async_api import Page, Response, async_playwright

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
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise StatsError("invalid_config", "SOLAR_STATS_TIMEOUT_SECONDS must be finite and positive.")

    @property
    def plant_id(self) -> str:
        plant_id = plant_from_url(self.plant_url)
        if plant_id is None:
            raise StatsError("invalid_config", "The configured URL has no plant identity.")
        return plant_id

    def report_url(self, session_url: str) -> str:
        if plant_from_url(session_url) != self.plant_id:
            raise StatsError("authentication_required", "Open the configured plant in the dedicated browser.")
        parsed = urlsplit(session_url)
        routing = [(key, value) for key, value in parse_qsl(parsed.query) if key in {"app-id", "instance-id", "zone-id"}]
        return urlunsplit(parsed._replace(
            query=urlencode(routing), fragment=f"/view/station/{self.plant_id}/report"
        ))

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
        return cls(url, Path(profile).expanduser(), timeout)


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
            async with asyncio.timeout(self.config.timeout_seconds):
                async with self._lock, profile_lock(self.config.profile_dir):
                    return await self._fetch(interval)
        except TimeoutError as error:
            raise StatsError("timeout", "The live query timed out; no cached or partial result was substituted.") from error
        except BrowserError as error:
            raise StatsError(
                "browser_session_unavailable",
                "The report browser is unavailable or its interface changed. Reopen/sign in to the dedicated profile.",
            ) from error
        except OSError as error:
            raise StatsError("local_access_failed", f"Cannot access the browser session ({type(error).__name__}).") from error

    def _check_plant(self, page: Page) -> None:
        if plant_from_url(page.url) != self.config.plant_id:
            raise StatsError("authentication_required", "The signed-in page is not the configured plant.")

    async def _fetch(self, interval: DateRange) -> GenerationReport:
        port = debugging_port(self.config.profile_dir)
        async with async_playwright() as driver:
            browser = await driver.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15000)
            if not browser.contexts:
                raise StatsError("authentication_required", "No authenticated browser context is available.")
            seed = next(
                (tab for tab in browser.contexts[0].pages if plant_from_url(tab.url) == self.config.plant_id), None
            )
            if seed is None:
                raise StatsError("authentication_required", "Open the configured plant in the signed-in dedicated browser.")
            # Native new-tab navigation retains tab-scoped login state without reading or exporting it.
            async with seed.expect_popup() as pending:
                await seed.evaluate('(url) => { window.open(url, "_blank"); }', self.config.report_url(seed.url))
            page = await pending.value
            page.set_default_timeout(15000)
            try:
                await page.wait_for_load_state("domcontentloaded")
                selector = page.locator(".dpdesign-select-selector").filter(has=page.locator("#timeDimension"))
                try:
                    await selector.wait_for(state="visible")
                except BrowserError as error:
                    raise StatsError(
                        "authentication_required",
                        "The Plant Report is not available. Sign in normally; MFA and CAPTCHA cannot be bypassed.",
                    ) from error
                self._check_plant(page)
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
        def is_report(response: Response) -> bool:
            parsed = urlsplit(response.url)
            return parsed.path == REPORT_PATH and (parsed.hostname or "").endswith(".fusionsolar.huawei.com")

        async with page.expect_response(is_report) as pending:
            await action()
        response = await pending.value
        self._check_plant(page)
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
