from __future__ import annotations

import asyncio
import calendar
import csv
import hashlib
import io
import json
import subprocess
import sys
from contextlib import suppress
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_client_server_memory_streams
from playwright.async_api import Error as BrowserError

from solar_stats import server as server_module
from solar_stats.browser import FusionSolarSource, StatsConfig, debugging_port, profile_lock, validate_report_page
from solar_stats.models import DateRange, GenerationReport, StatsError, make_report, parse_kwh, parse_month_rows
from solar_stats.output import csv_matrix, render, save_report
from solar_stats.server import create_server

PLANT_URL = "https://example.fusionsolar.huawei.com/cloud.html#/view/station/NE=123456/overview"
HEADERS = ["Statistical Period", "Inverter Yield (kWh)", "PV Yield (kWh)"]


def synthetic_report(interval: DateRange, *, missing: bool = False) -> GenerationReport:
    months = {}
    for year, month in interval.months():
        months[(year, month)] = {
            date(year, month, day): None if missing and day == 2 else Decimal("1.125")
            for day in range(1, calendar.monthrange(year, month)[1] + 1)
        }
    return make_report(interval, "NE=123456", months)


class FakeSource:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def fetch(self, interval: DateRange) -> GenerationReport:
        self.calls += 1
        if self.fail:
            raise StatsError("authentication_required", "Sign in to the dedicated browser.")
        return synthetic_report(interval, missing=True)


@pytest.mark.parametrize("text,expected", [("0.000", Decimal(0)), ("1,234.567", Decimal("1234.567")), ("--", None), ("", None)])
def test_kwh_parsing(text, expected) -> None:
    assert parse_kwh(text) == expected


@pytest.mark.parametrize("text", ["-1", "NaN", "Infinity", "1e3", "1,23", "1.000 kW", "0,5"])
def test_invalid_yield_is_not_zero(text) -> None:
    with pytest.raises(StatsError, match="invalid_source_data"):
        parse_kwh(text)


def test_bind_to_pv_column_and_preserve_zero_versus_missing() -> None:
    rows = [["2025-01-01", "999", "0.000"], ["2025-01-02", "999", "--"]]
    parsed = parse_month_rows(HEADERS, rows, 2025, 1)
    report = make_report(DateRange.parse("2025-01-01", "2025-01-03"), "NE=123456", {(2025, 1): parsed})
    assert report.generation_kwh == 0
    assert [day.source_missing for day in report.daily] == [False, True, True]
    assert report.missing_dates == ["2025-01-02", "2025-01-03"]
    assert not report.source_complete
    assert report.aggregation == "sum_with_missing_as_zero"


@pytest.mark.parametrize(
    "headers,rows",
    [
        (["Statistical Period", "PV Yield (MWh)"], [["2025-01-01", "1"]]),
        (["Statistical Period", "PV Yield (kWh)", "PV Yield (kWh)"], [["2025-01-01", "1", "2"]]),
        (HEADERS, [["2025-01-01"]]),
        (HEADERS, [["2025-01-01", "1", "1"], ["2025-01-01", "1", "1"]]),
        (HEADERS, [["2025-02-01", "1", "1"]]),
        (HEADERS, [["2025-01-01 00:00:00 DST", "1", "1"]]),
    ],
)
def test_wrong_metric_period_duplicates_or_rows_fail(headers, rows) -> None:
    with pytest.raises(StatsError):
        parse_month_rows(headers, rows, 2025, 1)


@pytest.mark.parametrize(
    "start,end",
    [("2025-02-29", None), ("2025-1-1", None), ("2025-W01-1", None), ("2025-01-02", "2025-01-01"), ("2025-01-01", "")],
)
def test_invalid_intervals(start, end) -> None:
    with pytest.raises(StatsError, match="invalid_request"):
        DateRange.parse(start, end)


def test_inclusive_multiyear_and_portal_calendar() -> None:
    interval = DateRange.parse("2024-12-31", "2025-01-02")
    assert len(interval.dates()) == 3
    assert interval.months() == [(2024, 12), (2025, 1)]
    interval.check_not_future(date(2025, 1, 2))
    with pytest.raises(StatsError, match="future"):
        interval.check_not_future(date(2025, 1, 1))
    assert DateRange.parse("2024-02-29").dates() == [date(2024, 2, 29)]


def test_failed_months_are_not_silently_filled() -> None:
    with pytest.raises(StatsError, match="incomplete_source"):
        make_report(DateRange.parse("2025-01-01", "2025-02-01"), "NE=123456", {(2025, 1): {}})
    with pytest.raises(StatsError, match="invalid_source_data"):
        make_report(DateRange.parse("2025-01-01"), "NE=123456", {(2025, 1): {date(2025, 1, 1): Decimal("-1")}})


@pytest.mark.parametrize("year,count,blanks", [(2023, 365, 7), (2024, 366, 6), (2025, 365, 7)])
def test_full_year_csv_shape_and_separate_exports(tmp_path, year, count, blanks) -> None:
    report = synthetic_report(DateRange.parse(f"{year}-01-01", f"{year}-12-31"), missing=True)
    path = tmp_path / f"generation-{year}.csv"
    save_report(report, "csv", path)
    rows = list(csv.reader(path.open()))
    assert rows[0] == ["year", "month", *(str(day) for day in range(1, 32))]
    assert len(rows) == 13
    assert rows[1][1] == "January" and rows[-1][1] == "December"
    assert all(row[0] == str(year) and len(row) == 33 for row in rows[1:])
    cells = [cell for row in rows[1:] for cell in row[2:]]
    assert sum(cell != "" for cell in cells) == count
    assert cells.count("") == blanks
    assert sum(Decimal(cell) for cell in cells if cell) == Decimal(str(report.generation_kwh))
    assert rows[2][30] == ("1.125" if year == 2024 else "")
    sidecar = json.loads(path.with_suffix(".metadata.json").read_text())
    assert sidecar["missing_dates"] == report.missing_dates
    assert sidecar["csv_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert sidecar["zero_filled_count"] == 12
    assert path.stat().st_mode & 0o777 == 0o600


def test_partial_multiyear_matrix_leaves_unrequested_days_blank() -> None:
    report = synthetic_report(DateRange.parse("2024-12-31", "2025-01-02"))
    rows = list(csv.reader(io.StringIO(csv_matrix(report))))
    assert len(rows) == 3
    assert rows[1][:2] == ["2024", "December"]
    assert all(cell == "" for cell in rows[1][2:-1])
    assert rows[1][-1] == "1.125"
    assert rows[2][:4] == ["2025", "January", "1.125", "1.125"]
    assert all(cell == "" for cell in rows[2][4:])
    assert json.loads(render(report, "json"))["generation_kwh"] == 3.375


def test_export_failure_does_not_replace_previous_data(tmp_path, monkeypatch) -> None:
    report = synthetic_report(DateRange.parse("2025-01-01"))
    path = tmp_path / "data.csv"
    path.write_text("previous dataset\n")
    monkeypatch.setattr("solar_stats.output.os.fsync", lambda _: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(StatsError, match="export_failed"):
        save_report(report, "csv", path)
    assert path.read_text() == "previous dataset\n"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "url",
    ["http://example.fusionsolar.huawei.com/#/view/station/NE=123/overview",
     "https://example.com/#/view/station/NE=123/overview",
     "https://user:secret@example.fusionsolar.huawei.com/#/view/station/NE=123/overview",
     PLANT_URL + "?token=secret", "https://[bad", "https://example.fusionsolar.huawei.com/login"],
)
def test_reject_unsafe_or_unbound_portal_urls(tmp_path, url) -> None:
    with pytest.raises(StatsError, match="invalid_config"):
        StatsConfig(url, tmp_path)


def test_config_and_loopback_port(tmp_path) -> None:
    config = StatsConfig(PLANT_URL, tmp_path)
    assert config.plant_id == "NE=123456"
    assert config.report_url(PLANT_URL).endswith("/NE=123456/report")
    assert config.timeout_seconds == 300
    with pytest.raises(StatsError):
        StatsConfig(PLANT_URL, Path("relative"))
    for value in ["0", "65536", "not-a-port"]:
        (tmp_path / "DevToolsActivePort").write_text(value)
        with pytest.raises(StatsError):
            debugging_port(tmp_path)
    (tmp_path / "DevToolsActivePort").write_text("12345\n/private-browser-endpoint\n")
    assert debugging_port(tmp_path) == 12345


def test_session_route_preserves_only_application_routing(tmp_path) -> None:
    config = StatsConfig(PLANT_URL, tmp_path)
    route = PLANT_URL.replace("cloud.html#", "cloud.html?app-id=plant&zone-id=region&token=do-not-copy#")
    assert config.report_url(route) == PLANT_URL.replace(
        "cloud.html#", "cloud.html?app-id=plant&zone-id=region#"
    ).replace("/overview", "/report")
    with pytest.raises(StatsError, match="authentication_required"):
        config.report_url(PLANT_URL.replace("123456", "654321"))


def test_query_opens_native_child_tab_and_closes_only_its_own_page(tmp_path, monkeypatch) -> None:
    async def check():
        source = FusionSolarSource(StatsConfig(PLANT_URL, tmp_path))
        seed, page, context, driver = (MagicMock() for _ in range(4))
        seed.url = page.url = PLANT_URL
        context.pages = [seed]
        popup = asyncio.get_running_loop().create_future()
        popup.set_result(page)
        seed.expect_popup.return_value.__aenter__.return_value.value = popup
        seed.evaluate = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        page.locator.return_value.filter.return_value.wait_for = AsyncMock()
        page.close = AsyncMock()
        driver.chromium.connect_over_cdp = AsyncMock(return_value=MagicMock(contexts=[context]))
        manager = MagicMock()
        manager.__aenter__.return_value = driver
        monkeypatch.setattr("solar_stats.browser.async_playwright", lambda: manager)
        monkeypatch.setattr("solar_stats.browser.debugging_port", lambda _: 12345)
        monkeypatch.setattr(source, "_ensure_report", AsyncMock())
        monkeypatch.setattr(source, "_granularity", AsyncMock(side_effect=StatsError("probe_complete", "Tab is ready.")))
        with pytest.raises(StatsError, match="probe_complete"):
            await source._fetch(DateRange.parse("2025-01-01"))
        seed.evaluate.assert_awaited_once_with(
            '(url) => { window.open(url, "_blank"); }', source.config.report_url(PLANT_URL)
        )
        context.new_page.assert_not_called()
        seed.close.assert_not_called()
        page.close.assert_awaited_once()
    asyncio.run(check())


def test_report_pagination_contract() -> None:
    payload = {"success": True, "data": {
        "total": 31, "pageNo": 4, "pageSize": 10,
        "list": [{"fmtCollectTimeStr": "2025-01-31"}],
    }}
    page = validate_report_page(payload)
    assert (page.total, page.number, page.size, page.dates) == (31, 4, 10, ["2025-01-31"])
    for broken in [
        {"success": False}, {"success": True, "data": {}},
        {"success": True, "data": {**payload["data"], "list": []}},
        {"success": True, "data": {**payload["data"], "pageNo": True}},
    ]:
        with pytest.raises(StatsError):
            validate_report_page(broken)


def test_profile_lock_cancellation_and_recovery(tmp_path) -> None:
    async def check():
        async def acquire():
            async with profile_lock(tmp_path):
                return True
        async with profile_lock(tmp_path):
            pending = asyncio.create_task(acquire())
            await asyncio.sleep(0.02)
            assert not pending.done()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert await acquire()
    asyncio.run(check())


def test_source_deadlines_and_safe_browser_errors(tmp_path, monkeypatch) -> None:
    async def check():
        source = FusionSolarSource(StatsConfig(PLANT_URL, tmp_path, 0.02))
        async def hang(_):
            await asyncio.sleep(10)
        monkeypatch.setattr(source, "_fetch", hang)
        with pytest.raises(StatsError, match="timeout"):
            await source.fetch(DateRange.parse("2025-01-01"))
        monkeypatch.setattr(source, "_fetch", AsyncMock(side_effect=BrowserError("sensitive internal details")))
        with pytest.raises(StatsError) as error:
            await source.fetch(DateRange.parse("2025-01-01"))
        assert "sensitive internal details" not in str(error.value)
    asyncio.run(check())


def test_mcp_results_csv_errors_and_no_cache() -> None:
    async def check():
        source = FakeSource()
        server = create_server(source)
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            pending = asyncio.create_task(server._lowlevel_server.run(
                *server_streams, server._lowlevel_server.create_initialization_options()
            ))
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    assert [tool.name for tool in tools] == ["get_generation"]
                    assert tools[0].annotations.read_only_hint
                    assert not tools[0].annotations.destructive_hint
                    assert "daily" in tools[0].output_schema["properties"]
                    arguments = {"start_date": "2025-01-01", "end_date": "2025-01-02"}
                    first = await session.call_tool("get_generation", arguments)
                    assert not first.is_error
                    assert first.structured_content["generation_kwh"] == 1.125
                    assert first.structured_content["missing_dates"] == ["2025-01-02"]
                    second = await session.call_tool("get_generation", {**arguments, "format": "csv"})
                    assert not second.is_error
                    assert second.content[0].text.startswith("year,month,1,2,3,")
                    assert source.calls == 2
                    invalid = await session.call_tool("get_generation", {**arguments, "format": "html"})
                    assert invalid.is_error and source.calls == 2
                    source.fail = True
                    failed = await session.call_tool("get_generation", arguments)
                    assert failed.is_error and failed.structured_content is None
                    assert "authentication_required" in str(failed.content)
            finally:
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
    asyncio.run(check())


def test_cli_stdout_and_file_output(tmp_path, monkeypatch, capsys) -> None:
    fake = FakeSource()
    monkeypatch.setenv("SOLAR_STATS_PLANT_URL", PLANT_URL)
    monkeypatch.setenv("SOLAR_STATS_PROFILE_DIR", str(tmp_path))
    monkeypatch.setattr(server_module, "FusionSolarSource", lambda _: fake)
    arguments = ["query", "--start-date", "2025-01-01", "--end-date", "2025-01-02"]
    server_module.main(arguments)
    assert json.loads(capsys.readouterr().out)["generation_kwh"] == 1.125
    server_module.main([*arguments, "--format", "csv"])
    assert capsys.readouterr().out.startswith("year,month,1,2,3,")
    path = tmp_path / "export.csv"
    server_module.main([*arguments, "--format", "csv", "--output", str(path)])
    captured = capsys.readouterr()
    assert captured.out == "" and "Saved" in captured.err
    assert path.is_file() and path.with_suffix(".metadata.json").is_file()
    assert fake.calls == 3
    fake.fail = True
    with pytest.raises(SystemExit) as error:
        server_module.main(arguments)
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert not captured.out and "authentication_required" in captured.err


def test_real_stdio_discovery_does_not_need_browser_and_no_modbus_import(tmp_path) -> None:
    async def check():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "solar_stats.server"],
            env={"SOLAR_STATS_PLANT_URL": PLANT_URL, "SOLAR_STATS_PROFILE_DIR": str(tmp_path)},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert [tool.name for tool in (await session.list_tools()).tools] == ["get_generation"]
                result = await session.call_tool("get_generation", {"start_date": "2025-01-01"})
                assert result.is_error and "authentication_required" in str(result.content)
    asyncio.run(check())
    subprocess.run(
        [sys.executable, "-c", "import solar_stats.server, sys; assert 'huawei_solar' not in sys.modules"],
        check=True, timeout=10,
    )
