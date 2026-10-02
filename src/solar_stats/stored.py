from __future__ import annotations

import asyncio
import calendar
import csv
import hashlib
import io
import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from .models import DateRange, GenerationDay, GenerationReport, StatsError, parse_kwh, report_from_days
from .output import MONTH_NAMES


class PortalSource(Protocol):
    async def fetch(self, interval: DateRange) -> GenerationReport: ...


def _timestamp(value: object) -> str:
    try:
        if not isinstance(value, str) or datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError
    except ValueError:
        raise ValueError("Source retrieval timestamp is missing or invalid.") from None
    return value


def read_saved_year(path: Path, plant_id: str, year: int) -> dict[date, GenerationDay]:
    try:
        content = path.read_bytes()
        meta = json.loads(path.with_suffix(".metadata.json").read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            raise ValueError("Metadata must be an object.")
        required = {
            "plant_id": plant_id, "unit": "kWh", "metric": "PV Yield",
            "source": "fusionsolar_plant_report", "date_basis": "plant_report_calendar",
            "aggregation": "sum_with_missing_as_zero",
            "csv_sha256": hashlib.sha256(content).hexdigest(),
        }
        if any(meta.get(key) != value for key, value in required.items()):
            raise ValueError("Plant identity, units, provenance, or CSV checksum do not match.")
        if not isinstance(meta.get("start_date"), str) or not isinstance(meta.get("end_date"), str):
            raise ValueError("The saved date interval is missing.")
        interval = DateRange.parse(meta["start_date"], meta["end_date"])
        if interval.start.year != year or interval.end.year != year:
            raise ValueError("The export is not a single-year file for the requested year.")
        retrieved_at = _timestamp(meta.get("retrieved_at"))
        missing = meta.get("missing_dates")
        if not isinstance(missing, list) or any(not isinstance(item, str) for item in missing):
            raise ValueError("Missing-date provenance is invalid.")
        if (
            len(set(missing)) != len(missing)
            or type(meta.get("zero_filled_count")) is not int
            or meta["zero_filled_count"] != len(missing)
            or meta.get("source_complete") is not (not missing)
            or not set(missing) <= {day.isoformat() for day in interval.dates()}
        ):
            raise ValueError("Missing-date provenance disagrees with the interval.")
        timestamps = meta.get("source_retrieved_at")
        if timestamps is not None and (
            not isinstance(timestamps, dict)
            or set(timestamps) != {day.isoformat() for day in interval.dates()}
        ):
            raise ValueError("Per-date source timestamps disagree with the interval.")
        rows = list(csv.reader(io.StringIO(content.decode("utf-8"), newline=""), strict=True))
        if not rows or rows[0] != ["year", "month", *map(str, range(1, 32))]:
            raise ValueError("The CSV matrix header is invalid.")
        months = interval.months()
        if len(rows) != len(months) + 1:
            raise ValueError("The CSV has missing or extra month rows.")
        result = {}
        total = Decimal(0)
        for (row_year, month), row in zip(months, rows[1:], strict=True):
            if len(row) != 33 or row[:2] != [str(row_year), MONTH_NAMES[month]]:
                raise ValueError("The CSV month labels or column count are invalid.")
            for number, cell in enumerate(row[2:], 1):
                day = date(year, month, number) if number <= calendar.monthrange(year, month)[1] else None
                if day is None or not interval.start <= day <= interval.end:
                    if cell:
                        raise ValueError("Nonexistent or unrequested dates must be blank.")
                    continue
                amount = parse_kwh(cell)
                if amount is None:
                    raise ValueError("A requested date cell must contain a numeric value.")
                source_missing = day.isoformat() in missing
                if source_missing and amount != 0:
                    raise ValueError("A source-missing date must contain a zero fill.")
                timestamp = _timestamp(timestamps[day.isoformat()]) if timestamps is not None else retrieved_at
                result[day] = GenerationDay(day.isoformat(), float(amount), source_missing, timestamp, True)
                total += amount
        reported_total = meta.get("generation_kwh")
        if type(reported_total) not in {int, float} or Decimal(str(reported_total)) != total:
            raise ValueError("The metadata total disagrees with the CSV.")
        return result
    except (OSError, ValueError, KeyError, csv.Error, StatsError) as error:
        raise StatsError(
            "invalid_saved_data",
            f"Cannot use {path.name}: {error}. Use refresh=true or --refresh to bypass saved files.",
        ) from None


def load_saved_days(directory: Path, plant_id: str, interval: DateRange) -> dict[date, GenerationDay]:
    try:
        return _load_saved_days(directory, plant_id, interval)
    except OSError as error:
        raise StatsError("saved_data_unavailable", f"Cannot read the saved-data directory ({type(error).__name__}).") from None


def _load_saved_days(directory: Path, plant_id: str, interval: DateRange) -> dict[date, GenerationDay]:
    if directory.exists() and not directory.is_dir():
        raise StatsError("invalid_config", "SOLAR_STATS_DATA_DIR must point to a directory.")
    entries = list(directory.iterdir()) if directory.exists() else []
    result: dict[date, GenerationDay] = {}
    for year in range(interval.start.year, interval.end.year + 1):
        canonical = directory / f"generation-{year}.csv"
        if canonical.exists() or canonical.with_suffix(".metadata.json").exists():
            paths = [canonical]
        else:
            paths = sorted(path for path in entries if path.name.startswith(f"generation-{year}-") and path.suffix == ".csv")
        for path in paths:
            for day, reading in read_saved_year(path, plant_id, year).items():
                if not interval.start <= day <= interval.end:
                    continue
                previous = result.get(day)
                if previous is not None and not previous.source_missing and not reading.source_missing:
                    if previous.generation_kwh != reading.generation_kwh:
                        raise StatsError("conflicting_saved_data", "Overlapping saved exports disagree; use --refresh.")
                    if previous.source_retrieved_at and reading.source_retrieved_at:
                        if datetime.fromisoformat(previous.source_retrieved_at) >= datetime.fromisoformat(reading.source_retrieved_at):
                            continue
                if previous is None or previous.source_missing or not reading.source_missing:
                    result[day] = reading
    return result


def missing_periods(days: list[date]) -> list[DateRange]:
    periods: list[DateRange] = []
    for day in sorted(days):
        if periods:
            previous = periods[-1]
            last_month = previous.end.year * 12 + previous.end.month
            this_month = day.year * 12 + day.month
            if this_month - last_month <= 1:
                periods[-1] = DateRange(previous.start, day)
                continue
        periods.append(DateRange(day, day))
    return periods


class StoredGenerationSource:
    def __init__(self, portal: PortalSource, plant_id: str, directory: Path, timeout_seconds: float = 300) -> None:
        self.portal = portal
        self.plant_id = plant_id
        self.directory = directory
        self.timeout_seconds = timeout_seconds

    async def fetch(self, interval: DateRange, *, refresh: bool = False) -> GenerationReport:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                saved = {} if refresh else load_saved_days(self.directory, self.plant_id, interval)
                readings = dict(saved)
                missing = [day for day in interval.dates() if day not in readings]
                for period in missing_periods(missing):
                    report = await self.portal.fetch(period)
                    if (
                        report.plant_id != self.plant_id
                        or (report.start_date, report.end_date) != (period.start.isoformat(), period.end.isoformat())
                        or [item.date for item in report.daily] != [day.isoformat() for day in period.dates()]
                    ):
                        raise StatsError("invalid_source_data", "The live report does not match the requested plant/period.")
                    for item in report.daily:
                        day = date.fromisoformat(item.date)
                        if day not in readings:
                            readings[day] = replace(
                                item, from_cache=False,
                                source_retrieved_at=item.source_retrieved_at or report.retrieved_at,
                            )
                return report_from_days(interval, self.plant_id, readings)
        except TimeoutError:
            raise StatsError("timeout", "The query timed out; no partial or truncated result was returned.") from None
