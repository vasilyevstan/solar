from __future__ import annotations

import calendar
import csv
import hashlib
import io
import json
import os
import tempfile
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Literal

from .models import DateRange, GenerationReport, StatsError

OutputFormat = Literal["json", "csv"]
MONTH_NAMES = (
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def csv_matrix(report: GenerationReport) -> str:
    values = {day.date: day.generation_kwh for day in report.daily}
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["year", "month", *range(1, 32)])
    for year, month in DateRange.parse(report.start_date, report.end_date).months():
        row: list[str | int] = [year, MONTH_NAMES[month]]
        for day in range(1, 32):
            key = f"{year:04d}-{month:02d}-{day:02d}"
            if day > calendar.monthrange(year, month)[1] or key not in values:
                row.append("")
            else:
                value = values[key]
                row.append("0" if value == 0 else format(Decimal(str(value)), "f"))
        writer.writerow(row)
    return output.getvalue()


def render(report: GenerationReport, output_format: OutputFormat) -> str:
    return csv_matrix(report) if output_format == "csv" else json.dumps(asdict(report), indent=2) + "\n"


def metadata(report: GenerationReport, csv_content: str) -> dict[str, object]:
    result = asdict(report)
    result.pop("daily")
    result["zero_filled_count"] = len(report.missing_dates)
    result["csv_sha256"] = hashlib.sha256(csv_content.encode()).hexdigest()
    return result


def _stage_file(path: Path, content: str) -> Path:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary_path
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


def save_report(report: GenerationReport, output_format: OutputFormat, path: Path) -> None:
    staged: list[Path] = []
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        content = render(report, output_format)
        data_file = _stage_file(path, content)
        staged.append(data_file)
        if output_format == "csv":
            sidecar = path.with_suffix(".metadata.json")
            meta_file = _stage_file(sidecar, json.dumps(metadata(report, content), indent=2) + "\n")
            staged.append(meta_file)
            # The checksum detects interrupted two-file publication; queries never use these exports as a cache.
            os.replace(meta_file, sidecar)
        os.replace(data_file, path)
    except OSError as error:
        raise StatsError("export_failed", f"Could not finish saving the export ({type(error).__name__}).") from error
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)
