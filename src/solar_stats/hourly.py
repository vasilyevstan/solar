from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import DateRange, StatsError, parse_kwh
from .output import save_contents_unlocked
from .stored import missing_periods, saved_data_lock


def report_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise StatsError("invalid_config", "Set SOLAR_STATS_TIMEZONE to the report's IANA time zone.") from None


@lru_cache(maxsize=4096)
def hour_slots(day: date, time_zone: str) -> tuple[tuple[str, str], ...]:
    zone = report_zone(time_zone)
    cursor = datetime.combine(day, time.min, zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
    counts: dict[str, int] = {}
    result = []
    while cursor < end:
        local = cursor.astimezone(zone)
        if local.minute or local.second:
            raise StatsError("unsupported_clock", "This report requires non-whole-hour clock transitions.")
        clock = local.strftime("%H:00")
        counts[clock] = counts.get(clock, 0) + 1
        column = clock if counts[clock] == 1 else f"{clock}#{counts[clock]}"
        label = local.strftime("%Y-%m-%d %H:%M:%S") + (" DST" if local.dst() else "")
        if label in {item[1] for item in result}:
            raise StatsError("unsupported_clock", "The report clock cannot distinguish repeated source labels.")
        result.append((column, label))
        cursor += timedelta(hours=1)
    return tuple(result)


@lru_cache(maxsize=32)
def hour_columns(year: int, time_zone: str) -> tuple[str, ...]:
    extra = {
        column
        for day in DateRange(date(year, 1, 1), date(year, 12, 31)).dates()
        for column, _ in hour_slots(day, time_zone)
        if "#" in column
    }
    return (*(f"{hour:02}:00" for hour in range(24)), *sorted(extra))


@dataclass(frozen=True)
class HourValue:
    column: str
    source_label: str
    generation_kwh: float
    source_missing: bool
    source_retrieved_at: str | None = None
    from_cache: bool = False


@dataclass(frozen=True)
class HourlyDay:
    date: str
    hours: list[HourValue]
    source_retrieved_at: str
    from_cache: bool = False


@dataclass(frozen=True)
class HourlyReport:
    start_date: str
    end_date: str
    plant_id: str
    time_zone: str
    generation_kwh: float
    daily: list[HourlyDay]
    missing_hours: list[str]
    unavailable_dates: list[str]
    source_complete: bool
    retrieved_at: str
    cached_hours: int
    portal_hours: int
    retrieval_mode: Literal["portal", "stored_file", "mixed"]
    granularity: Literal["hour"] = "hour"
    unit: Literal["kWh"] = "kWh"
    metric: Literal["PV Yield"] = "PV Yield"
    source: Literal["fusionsolar_plant_report"] = "fusionsolar_plant_report"
    date_basis: Literal["plant_report_calendar"] = "plant_report_calendar"
    aggregation: Literal["sum_available_hours_with_missing_as_zero"] = "sum_available_hours_with_missing_as_zero"


def make_hourly_report(
    interval: DateRange, plant_id: str, time_zone: str, days: dict[date, HourlyDay], *, partial: bool = False
) -> HourlyReport:
    expected = set(interval.dates())
    if set(days) - expected or (not partial and set(days) != expected):
        raise StatsError("incomplete_source", "Not every requested hourly date was successfully retrieved.")
    daily = []
    missing = []
    total = Decimal(0)
    cached = portal = 0
    for day in sorted(days):
        item = days[day]
        if item.date != day.isoformat() or [(value.column, value.source_label) for value in item.hours] != list(hour_slots(day, time_zone)):
            raise StatsError("invalid_source_data", "Hourly labels do not match the requested report clock.")
        if datetime.fromisoformat(item.source_retrieved_at).utcoffset() is None:
            raise StatsError("invalid_source_data", "An hourly source timestamp is missing its time zone.")
        normalized = []
        for value in item.hours:
            amount = Decimal(str(value.generation_kwh))
            if not amount.is_finite() or amount < 0 or (value.source_missing and amount != 0):
                raise StatsError("invalid_source_data", "Invalid hourly yield or zero-fill provenance.")
            total += amount
            if value.source_missing:
                missing.append(f"{item.date}/{value.column}")
            stamp = value.source_retrieved_at or item.source_retrieved_at
            if datetime.fromisoformat(stamp).utcoffset() is None:
                raise StatsError("invalid_source_data", "An hour's source timestamp is invalid.")
            normalized.append(replace(value, source_retrieved_at=stamp, from_cache=item.from_cache or value.from_cache))
        cached_count = sum(value.from_cache for value in normalized)
        cached += cached_count
        portal += len(normalized) - cached_count
        daily.append(replace(item, hours=normalized, from_cache=cached_count == len(normalized)))
    if not math.isfinite(float(total)):
        raise StatsError("invalid_source_data", "The hourly total is outside the supported numeric range.")
    unavailable = [day.isoformat() for day in sorted(expected - days.keys())]
    return HourlyReport(
        interval.start.isoformat(), interval.end.isoformat(), plant_id, time_zone, float(total),
        daily, missing, unavailable, not missing and not unavailable, datetime.now(UTC).isoformat(),
        cached, portal, "stored_file" if cached and not portal else "mixed" if cached else "portal",
    )


def parse_hourly_rows(
    headers: list[str], rows: list[list[str]], interval: DateRange, time_zone: str
) -> dict[str, Decimal | None]:
    labels = [" ".join(value.split()) for value in headers]
    if any(labels.count(value) != 1 for value in ("Statistical Period", "PV Yield (kWh)")):
        raise StatsError("portal_changed", "The hourly PV-yield columns or units changed.")
    date_index, value_index = labels.index("Statistical Period"), labels.index("PV Yield (kWh)")
    expected = {label for day in interval.dates() for _, label in hour_slots(day, time_zone)}
    result = {}
    for row in rows:
        if len(row) <= max(date_index, value_index):
            raise StatsError("invalid_source_data", "An hourly report row is incomplete.")
        label = row[date_index].strip()
        if label not in expected:
            raise StatsError("wrong_period", "An hourly label is outside the requested dates or report time zone.")
        if label in result:
            raise StatsError("invalid_source_data", "The hourly report repeated a source label.")
        result[label] = parse_kwh(row[value_index])
    return result


def hourly_days_from_values(
    interval: DateRange, time_zone: str, values: dict[str, Decimal | None]
) -> dict[date, HourlyDay]:
    retrieved = datetime.now(UTC).isoformat()
    return {
        day: HourlyDay(day.isoformat(), [
            HourValue(column, label, float(values[label]) if values.get(label) is not None else 0.0, values.get(label) is None)
            for column, label in hour_slots(day, time_zone)
        ], retrieved)
        for day in interval.dates()
    }


def hourly_csv(report: HourlyReport) -> str:
    years = sorted({date.fromisoformat(day.date).year for day in report.daily})
    columns = [*(f"{hour:02}:00" for hour in range(24))]
    columns.extend(sorted({column for year in years for column in hour_columns(year, report.time_zone) if "#" in column}))
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["date", *columns])
    for day in report.daily:
        values = {value.column: value.generation_kwh for value in day.hours}
        writer.writerow([day.date, *(
            "" if column not in values else "0" if values[column] == 0 else format(Decimal(str(values[column])), "f")
            for column in columns
        )])
    return stream.getvalue()


def hourly_metadata(report: HourlyReport, content: str) -> dict[str, object]:
    result = asdict(report)
    result.pop("daily")
    result["covered_dates"] = [day.date for day in report.daily]
    result["day_retrieved_at"] = {day.date: day.source_retrieved_at for day in report.daily}
    result["source_retrieved_at"] = {
        f"{day.date}/{value.column}": value.source_retrieved_at or day.source_retrieved_at
        for day in report.daily for value in day.hours
    }
    result["csv_sha256"] = hashlib.sha256(content.encode()).hexdigest()
    return result


def read_hourly_year(path: Path, plant_id: str, year: int, time_zone: str) -> dict[date, HourlyDay]:
    try:
        content = path.read_bytes()
        meta = json.loads(path.with_suffix(".metadata.json").read_text())
        required = {
            "plant_id": plant_id, "time_zone": time_zone, "unit": "kWh", "metric": "PV Yield",
            "source": "fusionsolar_plant_report", "granularity": "hour",
            "date_basis": "plant_report_calendar", "aggregation": "sum_available_hours_with_missing_as_zero",
            "csv_sha256": hashlib.sha256(content).hexdigest(),
        }
        if not isinstance(meta, dict) or any(meta.get(key) != value for key, value in required.items()):
            raise ValueError("Hourly identity, time zone, units or checksum do not match.")
        if not isinstance(meta.get("start_date"), str) or not isinstance(meta.get("end_date"), str):
            raise ValueError("The hourly export interval is missing.")
        interval = DateRange.parse(meta["start_date"], meta["end_date"])
        if interval.start.year != year or interval.end.year != year:
            raise ValueError("Hourly exports must stay within their named year.")
        rows = list(csv.reader(io.StringIO(content.decode("utf-8"), newline=""), strict=True))
        columns = hour_columns(year, time_zone)
        if not rows or rows[0] != ["date", *columns] or [row[0] for row in rows[1:] if row] != meta.get("covered_dates"):
            raise ValueError("Hourly CSV labels or coverage changed.")
        missing = meta.get("missing_hours")
        timestamps = meta.get("source_retrieved_at")
        day_times = meta.get("day_retrieved_at")
        if not isinstance(missing, list) or any(not isinstance(key, str) for key in missing) or len(set(missing)) != len(missing):
            raise ValueError("Missing-hour provenance is invalid.")
        if not isinstance(timestamps, dict) or not isinstance(day_times, dict) or set(day_times) != set(meta["covered_dates"]):
            raise ValueError("Hourly source timestamps are incomplete.")
        days = {}
        for row in rows[1:]:
            if len(row) != len(columns) + 1:
                raise ValueError("An hourly CSV row has the wrong number of columns.")
            day = DateRange.parse(row[0]).start
            if day in days or not interval.start <= day <= interval.end:
                raise ValueError("An hourly CSV row is duplicated or out of range.")
            expected = hour_slots(day, time_zone)
            cells = dict(zip(columns, row[1:], strict=True))
            if any(cells[column] for column in columns if column not in dict(expected)):
                raise ValueError("Nonexistent clock hours must be blank.")
            values = []
            for column, label in expected:
                amount = parse_kwh(cells[column])
                if amount is None:
                    raise ValueError("A real clock hour must be numeric, with missing readings explicitly flagged.")
                key = f"{day.isoformat()}/{column}"
                stamp = timestamps[key]
                if not isinstance(stamp, str) or datetime.fromisoformat(stamp).utcoffset() is None:
                    raise ValueError("An hour's source timestamp is invalid.")
                values.append(HourValue(column, label, float(amount), key in missing, stamp, True))
            stamp = day_times[day.isoformat()]
            if not isinstance(stamp, str) or datetime.fromisoformat(stamp).utcoffset() is None:
                raise ValueError("An hourly source timestamp is invalid.")
            days[day] = HourlyDay(day.isoformat(), values, stamp, True)
        report = make_hourly_report(interval, plant_id, time_zone, days, partial=True)
        if (
            set(timestamps) != {f"{day.date}/{value.column}" for day in report.daily for value in day.hours}
            or type(meta.get("generation_kwh")) not in {int, float}
            or report.generation_kwh != meta.get("generation_kwh")
            or report.missing_hours != missing
            or report.unavailable_dates != meta.get("unavailable_dates")
            or report.source_complete is not meta.get("source_complete")
        ):
            raise ValueError("Hourly totals or provenance do not match the CSV.")
        return days
    except (OSError, ValueError, KeyError, TypeError, csv.Error, StatsError) as error:
        raise StatsError("invalid_saved_data", f"Cannot use hourly export {path.name} ({type(error).__name__}).") from None


def save_hourly_years(report: HourlyReport, directory: Path) -> list[Path]:
    paths = []
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with saved_data_lock(directory, exclusive=True):
            for year in sorted({date.fromisoformat(day.date).year for day in report.daily}):
                path = directory / f"generation-hourly-{year}.csv"
                days = read_hourly_year(path, report.plant_id, year, report.time_zone) if path.exists() or path.with_suffix(".metadata.json").exists() else {}
                for item in report.daily:
                    day = date.fromisoformat(item.date)
                    if day.year != year:
                        continue
                    previous = days.get(day)
                    if previous is not None:
                        old = {value.column: value for value in previous.hours}
                        item = replace(item, hours=[
                            old[value.column] if not old[value.column].source_missing and (
                                value.source_missing
                                or datetime.fromisoformat(old[value.column].source_retrieved_at or previous.source_retrieved_at)
                                > datetime.fromisoformat(value.source_retrieved_at or item.source_retrieved_at)
                            ) else value
                            for value in item.hours
                        ])
                    days[day] = item
                yearly = make_hourly_report(DateRange(min(days), max(days)), report.plant_id, report.time_zone, days, partial=True)
                content = hourly_csv(yearly)
                save_contents_unlocked(content, hourly_metadata(yearly, content), path)
                paths.append(path)
    except OSError as error:
        raise StatsError("export_failed", f"Could not save hourly exports ({type(error).__name__}).") from error
    return paths


class HourlyPortal(Protocol):
    async def fetch_hourly(self, interval: DateRange) -> HourlyReport: ...


class StoredHourlySource:
    def __init__(self, portal: HourlyPortal, plant_id: str, directory: Path, time_zone: str | None, timeout_seconds: float = 300) -> None:
        self.portal, self.plant_id, self.directory = portal, plant_id, directory
        self.time_zone, self.timeout_seconds = time_zone, timeout_seconds

    async def fetch(self, interval: DateRange, *, refresh: bool = False) -> HourlyReport:
        if not self.time_zone:
            raise StatsError("invalid_config", "Set SOLAR_STATS_TIMEZONE before querying hourly history.")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                days = {}
                if not refresh:
                    with saved_data_lock(self.directory):
                        for year in range(interval.start.year, interval.end.year + 1):
                            path = self.directory / f"generation-hourly-{year}.csv"
                            if path.exists() or path.with_suffix(".metadata.json").exists():
                                days.update({
                                    day: item for day, item in read_hourly_year(path, self.plant_id, year, self.time_zone).items()
                                    if interval.start <= day <= interval.end
                                })
                for period in missing_periods([day for day in interval.dates() if day not in days]):
                    report = await self.portal.fetch_hourly(period)
                    if report.plant_id != self.plant_id or report.time_zone != self.time_zone or report.unavailable_dates:
                        raise StatsError("invalid_source_data", "The hourly source returned a different plant, clock or incomplete period.")
                    if [item.date for item in report.daily] != [day.isoformat() for day in period.dates()]:
                        raise StatsError("wrong_period", "The hourly source returned different dates.")
                    for item in report.daily:
                        day = date.fromisoformat(item.date)
                        if day not in days:
                            days[day] = replace(item, from_cache=False)
                return make_hourly_report(interval, self.plant_id, self.time_zone, days)
        except TimeoutError:
            raise StatsError("timeout", "The hourly query timed out; no partial result was substituted.") from None
        except OSError as error:
            raise StatsError("saved_data_unavailable", f"Cannot access hourly exports ({type(error).__name__}).") from error
