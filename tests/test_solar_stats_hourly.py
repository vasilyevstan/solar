from __future__ import annotations

import asyncio
import csv
import io
import json
from contextlib import suppress
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_client_server_memory_streams
import sys

from solar_stats import server
from solar_stats.browser import FusionSolarSource, StatsConfig
from solar_stats.hourly import (
    StoredHourlySource, hour_columns, hour_slots, hourly_csv, hourly_days_from_values,
    make_hourly_report, parse_hourly_rows, read_hourly_year, save_hourly_years,
)
from solar_stats.models import DateRange, StatsError
from solar_stats.server import create_server
from test_solar_stats import FakeSource

PLANT = "NE=123456"
URL = "https://example.fusionsolar.huawei.com/cloud.html#/view/station/NE=123456/report"
ZONE = "Europe/Helsinki"


def make_hours(start, end=None, *, amount="1.25", missing=()):
    period = DateRange.parse(start, end)
    values = {
        label: None if column in missing else Decimal(amount)
        for day in period.dates() for column, label in hour_slots(day, ZONE)
    }
    days = hourly_days_from_values(period, ZONE, values)
    return make_hourly_report(period, PLANT, ZONE, days)


class HourPortal:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def fetch_hourly(self, period):
        self.calls.append(period)
        if self.fail:
            raise StatsError("hourly_report_unavailable", "No successful source report.")
        return make_hours(period.start.isoformat(), period.end.isoformat())


@pytest.mark.parametrize("day,count", [
    ("2021-10-11", 24), ("2026-07-01", 24), ("2026-03-29", 23), ("2025-10-26", 25),
])
def test_real_clock_hours_and_dst_labels(day, count):
    slots = hour_slots(date.fromisoformat(day), ZONE)
    assert len(slots) == count and len(set(label for _, label in slots)) == count
    if count == 23:
        assert "03:00" not in dict(slots)
        assert dict(slots)["04:00"].endswith(" DST")
    if count == 25:
        assert dict(slots)["03:00"].endswith(" DST")
        assert not dict(slots)["03:00#2"].endswith(" DST")
    assert "03:00#2" in hour_columns(int(day[:4]), ZONE)


def test_hourly_matrix_keeps_nonexistent_hours_blank_and_missing_readings_flagged(tmp_path):
    report = make_hours("2026-03-29", missing=("04:00",))
    rows = list(csv.reader(io.StringIO(hourly_csv(report))))
    assert rows[0] == ["date", *(f"{hour:02}:00" for hour in range(24)), "03:00#2"]
    row = dict(zip(rows[0], rows[1], strict=True))
    assert row["03:00"] == row["03:00#2"] == ""
    assert row["04:00"] == "0"
    assert report.missing_hours == ["2026-03-29/04:00"]
    assert not report.source_complete and report.generation_kwh == 27.5
    path = save_hourly_years(report, tmp_path)[0]
    loaded = read_hourly_year(path, PLANT, 2026, ZONE)
    assert len(loaded[date(2026, 3, 29)].hours) == 23
    assert path.stat().st_mode & 0o777 == 0o600


def test_repeated_hours_have_separate_cells_and_totals(tmp_path):
    report = make_hours("2025-10-26")
    rows = list(csv.reader(io.StringIO(hourly_csv(report))))
    values = dict(zip(rows[0], rows[1], strict=True))
    assert values["03:00"] == values["03:00#2"] == "1.25"
    assert report.generation_kwh == 31.25
    path = save_hourly_years(report, tmp_path)[0]
    assert len(read_hourly_year(path, PLANT, 2025, ZONE)[date(2025, 10, 26)].hours) == 25


def test_hourly_parser_binds_pv_column_and_rejects_stale_duplicate_or_wrong_clock():
    period = DateRange.parse("2026-07-01")
    label = hour_slots(period.start, ZONE)[0][1]
    headers = ["Inverter Yield (kWh)", "PV Yield (kWh)", "Statistical Period"]
    assert parse_hourly_rows(headers, [["999", "0.125", label]], period, ZONE) == {label: Decimal("0.125")}
    for rows in (
        [["999", "1", label], ["999", "2", label]],
        [["999", "1", "2026-07-02 00:00:00 DST"]],
        [["999", "1", "2026-07-01 00:00:00"]],
        [["999", "-1", label]],
        [["incomplete"]],
    ):
        with pytest.raises(StatsError):
            parse_hourly_rows(headers, rows, period, ZONE)
    with pytest.raises(StatsError, match="columns"):
        parse_hourly_rows(["Statistical Period", "PV Yield (kW)"], [[label, "1"]], period, ZONE)


def test_successful_report_with_absent_hour_marks_missing_but_failed_requests_are_errors(tmp_path):
    period = DateRange.parse("2026-07-01")
    days = hourly_days_from_values(period, ZONE, {})
    report = make_hourly_report(period, PLANT, ZONE, days)
    assert len(report.missing_hours) == 24 and report.generation_kwh == 0
    assert not report.unavailable_dates
    with pytest.raises(StatsError, match="hourly_report_unavailable"):
        asyncio.run(StoredHourlySource(HourPortal(fail=True), PLANT, tmp_path, ZONE).fetch(period))
    assert list(tmp_path.iterdir()) == []


def test_hourly_cache_is_separate_from_daily_and_keeps_missing_flags(tmp_path):
    report = make_hours("2026-07-01", missing=("05:00",))
    save_hourly_years(report, tmp_path)
    assert not (tmp_path / "generation-2026.csv").exists()
    portal = HourPortal(fail=True)
    source = StoredHourlySource(portal, PLANT, tmp_path, ZONE)
    result = asyncio.run(source.fetch(DateRange.parse("2026-07-01")))
    assert not portal.calls and result.retrieval_mode == "stored_file"
    assert result.cached_hours == 24 and result.portal_hours == 0
    assert result.missing_hours == report.missing_hours
    assert result.daily[0].hours[0].source_retrieved_at == report.daily[0].hours[0].source_retrieved_at
    with pytest.raises(StatsError, match="hourly_report_unavailable"):
        asyncio.run(source.fetch(DateRange.parse("2026-07-01"), refresh=True))


def test_hourly_multi_year_cache_and_missing_period_fetch(tmp_path):
    save_hourly_years(make_hours("2024-12-31"), tmp_path)
    portal = HourPortal()
    report = asyncio.run(StoredHourlySource(portal, PLANT, tmp_path, ZONE).fetch(
        DateRange.parse("2024-12-31", "2025-01-01")
    ))
    assert portal.calls == [DateRange.parse("2025-01-01")]
    assert report.retrieval_mode == "mixed" and report.cached_hours == report.portal_hours == 24
    paths = save_hourly_years(report, tmp_path)
    assert [path.name for path in paths] == ["generation-hourly-2024.csv", "generation-hourly-2025.csv"]


def test_hourly_year_appends_preserve_values_timestamps_and_coverage(tmp_path):
    original = make_hours("2026-01-01")
    save_hourly_years(original, tmp_path)
    incoming = make_hours("2026-01-01", "2026-01-03", amount="2.5", missing=("05:00",))
    path = save_hourly_years(incoming, tmp_path)[0]
    days = read_hourly_year(path, PLANT, 2026, ZONE)
    assert len(days) == 3
    kept = next(value for value in days[date(2026, 1, 1)].hours if value.column == "05:00")
    old = next(value for value in original.daily[0].hours if value.column == "05:00")
    assert kept.generation_kwh == 1.25 and not kept.source_missing
    assert kept.source_retrieved_at == old.source_retrieved_at
    assert any(value.source_missing for value in days[date(2026, 1, 3)].hours)
    metadata = json.loads(path.with_suffix(".metadata.json").read_text())
    assert metadata["cached_hours"] == 1 and metadata["portal_hours"] == 71


@pytest.mark.parametrize("field,value", [
    ("plant_id", "NE=other"), ("time_zone", "UTC"), ("unit", "kW"), ("granularity", "day"),
    ("generation_kwh", 99), ("source_complete", False), ("covered_dates", []), ("missing_hours", ["2026-07-01/25:00"]),
])
def test_hourly_metadata_mismatch_fails_closed(tmp_path, field, value):
    path = save_hourly_years(make_hours("2026-07-01"), tmp_path)[0]
    sidecar = path.with_suffix(".metadata.json")
    meta = json.loads(sidecar.read_text())
    meta[field] = value
    sidecar.write_text(json.dumps(meta))
    with pytest.raises(StatsError, match="invalid_saved_data"):
        read_hourly_year(path, PLANT, 2026, ZONE)


def test_hourly_csv_checksum_and_missing_sidecar_are_errors(tmp_path):
    path = save_hourly_years(make_hours("2026-07-01"), tmp_path)[0]
    content = path.read_text()
    path.write_text(content.replace("1.25", "9", 1))
    with pytest.raises(StatsError, match="invalid_saved_data"):
        read_hourly_year(path, PLANT, 2026, ZONE)
    path.write_text(content)
    path.with_suffix(".metadata.json").unlink()
    with pytest.raises(StatsError, match="invalid_saved_data"):
        read_hourly_year(path, PLANT, 2026, ZONE)


def test_hourly_time_zone_is_required_but_does_not_change_daily_config(tmp_path):
    assert StatsConfig(URL, tmp_path).time_zone is None
    with pytest.raises(StatsError, match="invalid_config"):
        StatsConfig(URL, tmp_path, time_zone="not/a/time-zone")
    with pytest.raises(StatsError, match="SOLAR_STATS_TIMEZONE"):
        asyncio.run(StoredHourlySource(HourPortal(), PLANT, tmp_path, None).fetch(DateRange.parse("2026-07-01")))


def test_hourly_request_is_bound_to_the_period_clock_and_page(tmp_path):
    async def check():
        source = FusionSolarSource(StatsConfig(URL, tmp_path, time_zone=ZONE))
        period = DateRange.parse("2026-07-01", "2026-07-02")
        stamp = lambda day: int(datetime.combine(day, datetime.min.time(), ZoneInfo(ZONE)).timestamp() * 1000)
        payload = {"success": True, "data": {"total": 0, "pageNo": 1, "pageSize": 100, "list": []}}
        response = MagicMock(status=200, json=AsyncMock(return_value=payload))
        body = {"statDim": "2", "statTime": stamp(period.start), "statEndTime": stamp(period.end),
                "timeZoneStr": ZONE, "page": 1}
        request = MagicMock(
            url="https://example.fusionsolar.huawei.com/rest/pvms/web/report/v1/station/station-kpi-list",
            post_data_json=body, response=AsyncMock(return_value=response),
        )
        pending = asyncio.get_running_loop().create_future()
        pending.set_result(request)
        page = MagicMock(url=URL)
        page.expect_request.return_value.__aenter__.return_value.value = pending
        assert (await source._hourly_request(page, AsyncMock(), period, 1)).total == 0
        matches = page.expect_request.call_args.args[0]
        assert matches(request)
        assert not matches(MagicMock(url="https://other.example/rest/pvms/web/report/v1/station/station-kpi-list",
                                     post_data_json=body))
        for field, value in [("statDim", "4"), ("statTime", 0), ("page", 2)]:
            assert not matches(MagicMock(url=request.url, post_data_json={**body, field: value}))
        request.post_data_json = {**body, "timeZoneStr": "UTC"}
        with pytest.raises(StatsError, match="different report time zone"):
            await source._hourly_request(page, AsyncMock(), period, 1)
        request.post_data_json = body
        response.json.return_value = {"success": False, "failCode": 0, "message": None, "data": {"list": [], "total": 0}}
        with pytest.raises(StatsError, match="hourly_report_unavailable"):
            await source._hourly_request(page, AsyncMock(), period, 1)
        for failed in (
            {"success": False, "failCode": 123, "data": {"list": [], "total": 0}},
            {"success": False, "failCode": 0, "message": "access denied", "data": {"list": [], "total": 0}},
            {"success": False, "failCode": 0, "data": {"list": [None], "total": 1}},
        ):
            response.json.return_value = failed
            with pytest.raises(StatsError, match="source_unavailable"):
                await source._hourly_request(page, AsyncMock(), period, 1)
    asyncio.run(check())


def test_hourly_mcp_json_csv_and_refresh(tmp_path):
    async def check():
        save_hourly_years(make_hours("2025-10-26"), tmp_path)
        portal = HourPortal(fail=True)
        app = create_server(FakeSource(), StoredHourlySource(portal, PLANT, tmp_path, ZONE))
        async with create_client_server_memory_streams() as (client, server_streams):
            task = asyncio.create_task(app._lowlevel_server.run(
                *server_streams, app._lowlevel_server.create_initialization_options()
            ))
            try:
                async with ClientSession(*client) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    assert [tool.name for tool in tools] == ["get_generation", "get_hourly_generation"]
                    assert tools[1].annotations.read_only_hint and not tools[1].annotations.destructive_hint
                    for output in ("json", "csv"):
                        result = await session.call_tool("get_hourly_generation", {"start_date": "2025-10-26", "format": output})
                        assert not result.is_error
                        assert result.structured_content["cached_hours"] == 25
                        if output == "csv":
                            assert result.content[0].text.startswith("date,00:00,01:00")
                    assert not portal.calls
                    failed = await session.call_tool("get_hourly_generation", {"start_date": "2025-10-26", "refresh": True})
                    assert failed.is_error
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
    asyncio.run(check())


def test_hourly_cli_file_first_and_yearly_output(tmp_path, monkeypatch, capsys):
    save_hourly_years(make_hours("2026-07-01"), tmp_path)
    monkeypatch.setenv("SOLAR_STATS_PLANT_URL", URL)
    monkeypatch.setenv("SOLAR_STATS_PROFILE_DIR", str(tmp_path / "no-browser"))
    monkeypatch.setenv("SOLAR_STATS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOLAR_STATS_TIMEZONE", ZONE)
    arguments = ["query", "--start-date", "2026-07-01", "--granularity", "hour"]
    server.main(arguments)
    assert json.loads(capsys.readouterr().out)["retrieval_mode"] == "stored_file"
    server.main([*arguments, "--format", "csv", "--output-dir", str(tmp_path / "exports")])
    output = capsys.readouterr()
    assert not output.out and "generation-hourly-2026.csv" in output.err
    assert read_hourly_year(tmp_path / "exports/generation-hourly-2026.csv", PLANT, 2026, ZONE)


def test_hourly_stdio_works_without_browser_or_credentials(tmp_path):
    save_hourly_years(make_hours("2026-07-01"), tmp_path)
    async def check():
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "solar_stats.server"], env={
            "SOLAR_STATS_PLANT_URL": URL, "SOLAR_STATS_PROFILE_DIR": str(tmp_path / "absent"),
            "SOLAR_STATS_DATA_DIR": str(tmp_path), "SOLAR_STATS_TIMEZONE": ZONE,
        })
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("get_hourly_generation", {"start_date": "2026-07-01"})
                assert not result.is_error
                assert result.structured_content["source_complete"] and result.structured_content["portal_hours"] == 0
    asyncio.run(check())


def test_bulk_export_keeps_successful_months_and_reports_unavailable_periods(tmp_path):
    class Source:
        def __init__(self):
            self.calls = []

        async def fetch(self, period, *, refresh=False):
            self.calls.append(period)
            if period.start.month == 1:
                raise StatsError("hourly_report_unavailable", "Empty source report.")
            return make_hours(period.start.isoformat(), period.end.isoformat())

    source = Source()
    with pytest.raises(StatsError, match="incomplete_hourly_export"):
        asyncio.run(server.export_hourly_batches(
            source, DateRange.parse("2025-01-01", "2025-03-31"), tmp_path
        ))
    assert len(source.calls) == 3
    days = read_hourly_year(tmp_path / "generation-hourly-2025.csv", PLANT, 2025, ZONE)
    assert len(days) == 59 and min(days) == date(2025, 2, 1)
    assert not any(day.month == 1 for day in days)


@pytest.mark.parametrize("code", ["authentication_required", "rate_limited", "source_unavailable", "wrong_period"])
def test_bulk_export_stops_on_real_failures_without_losing_completed_months(tmp_path, code):
    class Source:
        def __init__(self):
            self.calls = []

        async def fetch(self, period, *, refresh=False):
            self.calls.append(period)
            if period.start.month == 2:
                raise StatsError(code, "Stop the batch.")
            return make_hours(period.start.isoformat(), period.end.isoformat())

    source = Source()
    with pytest.raises(StatsError, match=code):
        asyncio.run(server.export_hourly_batches(
            source, DateRange.parse("2025-01-01", "2025-03-31"), tmp_path
        ))
    assert len(source.calls) == 2
    assert len(read_hourly_year(tmp_path / "generation-hourly-2025.csv", PLANT, 2025, ZONE)) == 31
