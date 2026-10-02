from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

MONTH_NAMES = (
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


class StatsError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    @classmethod
    def parse(cls, start_date: str, end_date: str | None = None) -> DateRange:
        values = [start_date, start_date if end_date is None else end_date]
        if any(not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in values):
            raise StatsError("invalid_request", "Dates must use YYYY-MM-DD.")
        try:
            start, end = (date.fromisoformat(value) for value in values)
        except ValueError as error:
            raise StatsError("invalid_request", "A requested date does not exist.") from error
        if end < start:
            raise StatsError("invalid_request", "end_date must not precede start_date.")
        return cls(start, end)

    def months(self) -> list[tuple[int, int]]:
        months = []
        year, month = self.start.year, self.start.month
        while (year, month) <= (self.end.year, self.end.month):
            months.append((year, month))
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return months

    def dates(self) -> list[date]:
        return [self.start + timedelta(days=offset) for offset in range((self.end - self.start).days + 1)]

    def check_not_future(self, portal_today: date) -> None:
        if self.end > portal_today:
            raise StatsError("invalid_request", "The requested range includes future plant-calendar dates.")


@dataclass(frozen=True)
class GenerationDay:
    date: str
    generation_kwh: float
    source_missing: bool
    source_retrieved_at: str | None = None
    from_cache: bool = False


@dataclass(frozen=True)
class GenerationReport:
    start_date: str
    end_date: str
    plant_id: str
    generation_kwh: float
    daily: list[GenerationDay]
    missing_dates: list[str]
    source_complete: bool
    retrieved_at: str
    unit: Literal["kWh"] = "kWh"
    metric: Literal["PV Yield"] = "PV Yield"
    source: Literal["fusionsolar_plant_report"] = "fusionsolar_plant_report"
    date_basis: Literal["plant_report_calendar"] = "plant_report_calendar"
    aggregation: Literal["sum_with_missing_as_zero"] = "sum_with_missing_as_zero"
    retrieval_mode: Literal["portal", "stored_file", "mixed"] = "portal"
    cached_days: int = 0
    portal_days: int = 0


def parse_kwh(text: str) -> Decimal | None:
    text = text.strip()
    if text in {"", "--", "\u2014", "\u2013"}:
        return None
    if not re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", text):
        raise StatsError("invalid_source_data", "PV yield is not a nonnegative kWh number or a missing marker.")
    value = Decimal(text.replace(",", ""))
    if not math.isfinite(float(value)):
        raise StatsError("invalid_source_data", "PV yield is outside the supported numeric range.")
    return value


def parse_month_rows(
    headers: list[str], rows: list[list[str]], year: int, month: int
) -> dict[date, Decimal | None]:
    labels = [" ".join(header.split()) for header in headers]
    required = ["Statistical Period", "PV Yield (kWh)"]
    if any(labels.count(label) != 1 for label in required):
        raise StatsError("portal_changed", "The daily Plant Report columns or units changed.")
    date_index, value_index = (labels.index(label) for label in required)
    result: dict[date, Decimal | None] = {}
    for row in rows:
        if len(row) <= max(date_index, value_index):
            raise StatsError("invalid_source_data", "An incomplete report row was returned.")
        interval = DateRange.parse(row[date_index].strip())
        day = interval.start
        if (day.year, day.month) != (year, month):
            raise StatsError("wrong_period", "The portal returned a stale or out-of-period row.")
        if day in result:
            raise StatsError("invalid_source_data", "The portal returned duplicate daily records.")
        result[day] = parse_kwh(row[value_index])
    return result


def make_report(
    interval: DateRange,
    plant_id: str,
    months: dict[tuple[int, int], dict[date, Decimal | None]],
) -> GenerationReport:
    if set(months) != set(interval.months()):
        raise StatsError("incomplete_source", "Not every requested month was successfully retrieved.")
    for (year, month), rows in months.items():
        if len(rows) > calendar.monthrange(year, month)[1] or any(
            (day.year, day.month) != (year, month) for day in rows
        ):
            raise StatsError("invalid_source_data", "Monthly report coverage is inconsistent.")
        for value in rows.values():
            if value is not None and (not value.is_finite() or value < 0):
                raise StatsError("invalid_source_data", "Invalid source yield; it cannot be replaced with zero.")
    days = []
    missing = []
    total = Decimal(0)
    retrieved_at = datetime.now(UTC).isoformat()
    for day in interval.dates():
        value = months[(day.year, day.month)].get(day)
        if value is None:
            missing.append(day.isoformat())
        amount = Decimal(0) if value is None else value
        total += amount
        days.append(GenerationDay(day.isoformat(), float(amount), value is None, retrieved_at))
    if not math.isfinite(float(total)):
        raise StatsError("invalid_source_data", "The period total is outside the supported numeric range.")
    return GenerationReport(
        start_date=interval.start.isoformat(),
        end_date=interval.end.isoformat(),
        plant_id=plant_id,
        generation_kwh=float(total),
        daily=days,
        missing_dates=missing,
        source_complete=not missing,
        retrieved_at=retrieved_at,
        portal_days=len(days),
    )


def report_from_days(
    interval: DateRange, plant_id: str, readings: dict[date, GenerationDay]
) -> GenerationReport:
    months: dict[tuple[int, int], dict[date, Decimal | None]] = {month: {} for month in interval.months()}
    for day in interval.dates():
        if day not in readings:
            raise StatsError("incomplete_source", "Not every requested date was retrieved.")
        item = readings[day]
        if item.date != day.isoformat() or (item.source_missing and item.generation_kwh != 0):
            raise StatsError("invalid_source_data", "Daily reading provenance is inconsistent.")
        months[(day.year, day.month)][day] = None if item.source_missing else Decimal(str(item.generation_kwh))
    report = make_report(interval, plant_id, months)
    daily = [
        replace(day, from_cache=readings[date.fromisoformat(day.date)].from_cache,
                source_retrieved_at=readings[date.fromisoformat(day.date)].source_retrieved_at)
        for day in report.daily
    ]
    cached = sum(day.from_cache for day in daily)
    return replace(
        report, daily=daily, cached_days=cached, portal_days=len(daily) - cached,
        retrieval_mode="stored_file" if cached == len(daily) else "mixed" if cached else "portal",
    )
