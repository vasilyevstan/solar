from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import sys
from contextlib import suppress
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_client_server_memory_streams

from solar_stats import server
from solar_stats.models import DateRange, StatsError, make_report
from solar_stats.output import save_report, save_yearly_reports
from solar_stats.server import create_server
from solar_stats.stored import StoredGenerationSource, load_saved_days, missing_periods, read_saved_year

PLANT = "NE=123456"
PLANT_URL = "https://example.fusionsolar.huawei.com/cloud.html#/view/station/NE=123456/report"
OLD_TIME = "2026-10-01T00:00:00+00:00"


def report_for(start, end=None, *, amount="1.25", missing=()):
    period = DateRange.parse(start, end)
    months = {month: {} for month in period.months()}
    for day in period.dates():
        months[(day.year, day.month)][day] = None if day.isoformat() in missing else Decimal(amount)
    report = make_report(period, PLANT, months)
    return replace(
        report, retrieved_at=OLD_TIME,
        daily=[replace(day, source_retrieved_at=OLD_TIME) for day in report.daily],
    )


class Portal:
    def __init__(self, *, fail=False, missing=()):
        self.calls = []
        self.fail = fail
        self.missing = missing

    async def fetch(self, period):
        self.calls.append(period)
        if self.fail:
            raise StatsError("authentication_required", "The browser is unavailable.")
        return report_for(period.start.isoformat(), period.end.isoformat(), amount="2.5", missing=self.missing)


def saved(tmp_path, start, end=None, *, name=None, **kwargs):
    path = tmp_path / (name or f"generation-{start[:4]}.csv")
    save_report(report_for(start, end, **kwargs), "csv", path)
    return path


def test_complete_saved_measurements_and_real_zero_work_without_portal(tmp_path):
    path = saved(tmp_path, "2025-01-01", "2025-01-03", amount="0")
    portal = Portal(fail=True)
    source = StoredGenerationSource(portal, PLANT, tmp_path)
    report = asyncio.run(source.fetch(DateRange.parse("2025-01-02")))
    assert portal.calls == []
    assert report.generation_kwh == 0 and report.source_complete
    assert report.retrieval_mode == "stored_file" and report.cached_days == 1 and report.portal_days == 0
    assert report.daily[0].from_cache and report.daily[0].source_retrieved_at == OLD_TIME
    assert path.is_file()


def test_partial_multiyear_query_fetches_only_the_missing_month_span(tmp_path):
    saved(tmp_path, "2024-12-31", amount="0")
    saved(tmp_path, "2025-01-01", "2025-01-02")
    portal = Portal()
    report = asyncio.run(StoredGenerationSource(portal, PLANT, tmp_path).fetch(
        DateRange.parse("2024-12-31", "2025-01-03")
    ))
    assert portal.calls == [DateRange.parse("2025-01-03")]
    assert report.generation_kwh == 5
    assert report.cached_days == 3 and report.portal_days == 1 and report.retrieval_mode == "mixed"
    assert [day.from_cache for day in report.daily] == [True, True, True, False]
    assert report.source_complete and not report.missing_dates


def test_stored_missing_values_remain_flagged_without_requiring_portal(tmp_path):
    saved(tmp_path, "2025-08-01", "2025-08-02", missing=("2025-08-02",))
    portal = Portal(fail=True)
    report = asyncio.run(StoredGenerationSource(portal, PLANT, tmp_path).fetch(
        DateRange.parse("2025-08-01", "2025-08-02")
    ))
    assert not portal.calls
    assert report.missing_dates == ["2025-08-02"] and not report.source_complete
    assert report.daily[1].generation_kwh == 0 and report.daily[1].source_missing
    assert report.daily[1].from_cache and report.retrieval_mode == "stored_file"


def test_refresh_can_replace_a_stored_missing_placeholder(tmp_path):
    saved(tmp_path, "2025-08-01", "2025-08-02", missing=("2025-08-02",))
    portal = Portal()
    report = asyncio.run(StoredGenerationSource(portal, PLANT, tmp_path).fetch(
        DateRange.parse("2025-08-01", "2025-08-02"), refresh=True
    ))
    assert len(portal.calls) == 1 and not report.missing_dates
    assert report.daily[1].generation_kwh == 2.5 and not report.daily[1].source_missing
    assert report.cached_days == 0 and report.portal_days == 2


def test_portal_failure_is_not_partial_or_cached_success(tmp_path):
    saved(tmp_path, "2025-01-01")
    portal = Portal(fail=True)
    with pytest.raises(StatsError, match="authentication_required"):
        asyncio.run(StoredGenerationSource(portal, PLANT, tmp_path).fetch(
            DateRange.parse("2025-01-01", "2025-01-02")
        ))


def test_explicit_refresh_bypasses_even_corrupt_saved_files(tmp_path):
    path = saved(tmp_path, "2025-01-01", "2025-01-03")
    path.write_text("corrupt")
    portal = Portal()
    interval = DateRange.parse("2025-01-01", "2025-01-03")
    result = asyncio.run(StoredGenerationSource(portal, PLANT, tmp_path).fetch(interval, refresh=True))
    assert portal.calls == [interval]
    assert result.generation_kwh == 7.5 and result.cached_days == 0 and result.portal_days == 3
    assert result.retrieval_mode == "portal"
    assert path.read_text() == "corrupt"


def test_queries_do_not_create_an_implicit_storage_backend(tmp_path):
    directory = tmp_path / "not-created"
    result = asyncio.run(StoredGenerationSource(Portal(), PLANT, directory).fetch(DateRange.parse("2025-01-01")))
    assert result.generation_kwh == 2.5 and not directory.exists()


def test_unreadable_saved_directory_is_not_treated_as_an_empty_cache(tmp_path, monkeypatch):
    def denied(_):
        raise PermissionError
    monkeypatch.setattr(Path, "iterdir", denied)
    portal = Portal()
    with pytest.raises(StatsError, match="saved_data_unavailable"):
        asyncio.run(StoredGenerationSource(portal, PLANT, tmp_path).fetch(DateRange.parse("2025-01-01")))
    assert not portal.calls


def test_missing_periods_reuse_monthly_reporting_without_fetching_complete_months():
    days = [date(2024, 12, 31), date(2025, 1, 1), date(2025, 3, 4), date(2025, 3, 20)]
    assert missing_periods(days) == [
        DateRange.parse("2024-12-31", "2025-01-01"),
        DateRange.parse("2025-03-04", "2025-03-20"),
    ]


def test_partial_filename_compatibility_and_canonical_precedence(tmp_path):
    saved(tmp_path, "2026-01-01", "2026-09-30", name="generation-2026-jan-sep.csv")
    period = DateRange.parse("2026-07-01")
    assert load_saved_days(tmp_path, PLANT, period)[period.start].generation_kwh == 1.25
    saved(tmp_path, "2026-01-01", "2026-09-30", amount="3.5")
    assert load_saved_days(tmp_path, PLANT, period)[period.start].generation_kwh == 3.5


def test_overlapping_partial_exports_must_not_disagree(tmp_path):
    saved(tmp_path, "2025-01-01", name="generation-2025-first.csv")
    saved(tmp_path, "2025-01-01", name="generation-2025-other.csv", amount="2")
    with pytest.raises(StatsError, match="conflicting_saved_data"):
        load_saved_days(tmp_path, PLANT, DateRange.parse("2025-01-01"))


def test_legacy_metadata_without_per_date_timestamps_is_supported(tmp_path):
    path = saved(tmp_path, "2025-01-01")
    sidecar = path.with_suffix(".metadata.json")
    meta = json.loads(sidecar.read_text())
    meta.pop("source_retrieved_at")
    sidecar.write_text(json.dumps(meta))
    assert read_saved_year(path, PLANT, 2025)[date(2025, 1, 1)].source_retrieved_at == OLD_TIME


@pytest.mark.parametrize("field,value", [
    ("plant_id", "NE=999999"), ("unit", "MWh"), ("metric", "Inverter Yield"),
    ("date_basis", "utc"), ("source", "other"), ("generation_kwh", 99),
    ("zero_filled_count", True), ("source_complete", False), ("missing_dates", ["2025-01-02"]),
    ("retrieved_at", "2025-01-01"), ("source_retrieved_at", {}),
])
def test_invalid_metadata_is_explicit_and_never_contacts_portal(tmp_path, field, value):
    path = saved(tmp_path, "2025-01-01")
    sidecar = path.with_suffix(".metadata.json")
    meta = json.loads(sidecar.read_text())
    meta[field] = value
    sidecar.write_text(json.dumps(meta))
    portal = Portal()
    with pytest.raises(StatsError, match="invalid_saved_data"):
        asyncio.run(StoredGenerationSource(portal, PLANT, tmp_path).fetch(DateRange.parse("2025-01-01")))
    assert not portal.calls


@pytest.mark.parametrize("problem", ["checksum", "no_metadata", "no_csv", "header", "invalid_date", "blank_reading", "month"])
def test_bad_csv_and_interrupted_pairs_are_not_silent_cache_misses(tmp_path, problem):
    path = saved(tmp_path, "2025-02-01", "2025-02-28")
    sidecar = path.with_suffix(".metadata.json")
    if problem == "no_metadata":
        sidecar.unlink()
    elif problem == "no_csv":
        path.unlink()
    elif problem == "checksum":
        path.write_text(path.read_text().replace("1.25", "1.5", 1))
    else:
        rows = list(csv.reader(io.StringIO(path.read_text())))
        if problem == "header":
            rows[0][2] = "0"
        elif problem == "invalid_date":
            rows[1][30] = "0"
        elif problem == "blank_reading":
            rows[1][2] = ""
        elif problem == "month":
            rows[1][1] = "January"
        output = io.StringIO()
        csv.writer(output, lineterminator="\n").writerows(rows)
        path.write_text(output.getvalue())
        meta = json.loads(sidecar.read_text())
        meta["csv_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        sidecar.write_text(json.dumps(meta))
    with pytest.raises(StatsError, match="invalid_saved_data"):
        load_saved_days(tmp_path, PLANT, DateRange.parse("2025-02-01"))


def test_oldest_partial_year_and_leap_year_split_round_trip(tmp_path):
    original = report_for("2021-10-01", "2024-02-29", missing=("2022-02-03",))
    paths = save_yearly_reports(original, tmp_path)
    assert [path.name for path in paths] == [f"generation-{year}.csv" for year in range(2021, 2025)]
    expected_counts = [92, 365, 365, 60]
    for path, year, count in zip(paths, range(2021, 2025), expected_counts, strict=True):
        days = read_saved_year(path, PLANT, year)
        assert len(days) == count
        assert all(day.source_retrieved_at == OLD_TIME for day in days.values())
    with paths[0].open() as stream:
        rows = list(csv.reader(stream))
    assert len(rows) == 4 and rows[1][:2] == ["2021", "October"] and rows[-1][:2] == ["2021", "December"]
    assert read_saved_year(paths[1], PLANT, 2022)[date(2022, 2, 3)].source_missing


def test_wrong_live_plant_and_query_deadline_are_errors(tmp_path):
    class WrongPlant(Portal):
        async def fetch(self, period):
            return replace(await super().fetch(period), plant_id="NE=other")
    with pytest.raises(StatsError, match="invalid_source_data"):
        asyncio.run(StoredGenerationSource(WrongPlant(), PLANT, tmp_path).fetch(DateRange.parse("2025-01-01")))
    class Slow(Portal):
        async def fetch(self, period):
            await asyncio.sleep(10)
    with pytest.raises(StatsError, match="timeout"):
        asyncio.run(StoredGenerationSource(Slow(), PLANT, tmp_path, timeout_seconds=0.01).fetch(
            DateRange.parse("2025-01-01")
        ))


def test_mcp_default_file_reads_and_explicit_refresh(tmp_path):
    async def check():
        saved(tmp_path, "2025-01-01")
        portal = Portal()
        app = create_server(StoredGenerationSource(portal, PLANT, tmp_path))
        async with create_client_server_memory_streams() as (client, server_streams):
            task = asyncio.create_task(app._lowlevel_server.run(
                *server_streams, app._lowlevel_server.create_initialization_options()
            ))
            try:
                async with ClientSession(*client) as session:
                    await session.initialize()
                    tool = (await session.list_tools()).tools[0]
                    assert tool.input_schema["properties"]["refresh"]["default"] is False
                    first = await session.call_tool("get_generation", {"start_date": "2025-01-01"})
                    assert not first.is_error and first.structured_content["retrieval_mode"] == "stored_file"
                    assert not portal.calls
                    second = await session.call_tool("get_generation", {"start_date": "2025-01-01", "refresh": True})
                    assert not second.is_error and second.structured_content["generation_kwh"] == 2.5
                    assert len(portal.calls) == 1
                    bad = await session.call_tool("get_generation", {"start_date": "2025-01-01", "refresh": "false"})
                    assert bad.is_error and len(portal.calls) == 1
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
    asyncio.run(check())


def test_cli_cached_stdout_and_yearly_exports(tmp_path, monkeypatch, capsys):
    saved(tmp_path, "2024-12-31")
    saved(tmp_path, "2025-01-01")
    monkeypatch.setenv("SOLAR_STATS_PLANT_URL", PLANT_URL)
    monkeypatch.setenv("SOLAR_STATS_PROFILE_DIR", str(tmp_path / "no-browser"))
    monkeypatch.setenv("SOLAR_STATS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOLAR_STATS_KEYCHAIN_SERVICE", "do-not-read")
    portal = Portal(fail=True)
    monkeypatch.setattr(server, "FusionSolarSource", lambda _: portal)
    args = ["query", "--start-date", "2024-12-31", "--end-date", "2025-01-01"]
    server.main(args)
    result = json.loads(capsys.readouterr().out)
    assert result["cached_days"] == 2 and not portal.calls
    server.main([*args, "--format", "csv", "--output-dir", str(tmp_path / "exports")])
    output = capsys.readouterr()
    assert not output.out and output.err.count("Saved ") == 2
    assert len(list((tmp_path / "exports").glob("*.csv"))) == 2
    with pytest.raises(SystemExit) as error:
        server.main([*args, "--refresh", "--format", "csv", "--output-dir", str(tmp_path / "failed")])
    assert error.value.code == 2 and not (tmp_path / "failed").exists()


def test_registered_style_stdio_query_needs_no_browser_on_cache_hit(tmp_path):
    saved(tmp_path, "2025-01-01", amount="0")
    async def check():
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "solar_stats.server"], env={
                "SOLAR_STATS_PLANT_URL": PLANT_URL,
                "SOLAR_STATS_PROFILE_DIR": str(tmp_path / "unavailable-profile"),
                "SOLAR_STATS_DATA_DIR": str(tmp_path),
                "SOLAR_STATS_KEYCHAIN_SERVICE": "must-not-be-read",
            },
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("get_generation", {"start_date": "2025-01-01", "format": "csv"})
                assert not result.is_error
                assert result.structured_content["source_complete"]
                assert result.structured_content["daily"][0]["source_retrieved_at"] == OLD_TIME
                assert result.structured_content["retrieval_mode"] == "stored_file"
    asyncio.run(check())
