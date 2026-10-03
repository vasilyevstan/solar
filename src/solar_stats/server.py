from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Annotated, Protocol

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from .actions import ActionsConfig, GitHubActionsSource
from .browser import FusionSolarSource, StatsConfig
from .hourly import HourlyReport, StoredHourlySource, hourly_csv, hourly_metadata, save_hourly_years
from .models import DateRange, GenerationReport, StatsError
from .output import OutputFormat, render, save_contents_unlocked, save_report, save_yearly_reports
from .stored import StoredGenerationSource, saved_data_lock

LOGGER = logging.getLogger(__name__)


class GenerationSource(Protocol):
    async def fetch(self, interval: DateRange, *, refresh: bool = False) -> GenerationReport: ...


class HourlySource(Protocol):
    async def fetch(self, interval: DateRange, *, refresh: bool = False) -> HourlyReport: ...


async def export_hourly_batches(
    source: HourlySource, interval: DateRange, directory: Path, *, refresh: bool = False
) -> None:
    unavailable = []
    for year, month in interval.months():
        period = DateRange(
            max(interval.start, date(year, month, 1)),
            min(interval.end, date(year, month, calendar.monthrange(year, month)[1])),
        )
        try:
            report = await source.fetch(period, refresh=refresh)
        except StatsError as error:
            if error.code != "hourly_report_unavailable":
                raise
            LOGGER.warning("%s", error)
            unavailable.append(f"{period.start} through {period.end}")
            continue
        for path in save_hourly_years(report, directory):
            print(f"Saved {path} ({period.start} through {period.end})", file=sys.stderr, flush=True)
    if unavailable:
        raise StatsError(
            "incomplete_hourly_export",
            "Successful batches were saved; no hourly records were returned for: " + "; ".join(unavailable),
        )


def create_server(source: GenerationSource, hourly_source: HourlySource | None = None) -> MCPServer[None]:
    server = MCPServer(
        "solar-stats",
        instructions=(
            "Read-only whole-plant PV generation history in kWh, not instantaneous kW or grid export. "
            "Queries prefer validated local yearly CSV records; only uncovered dates go to FusionSolar. "
            "refresh=true forces a live query. Source-missing dates are zero-filled "
            "but identified in metadata; never describe incomplete totals as all actual production. "
            "Dates follow the plant report calendar. No inverter controls or credential tool arguments/results."
            " Hourly queries preserve the report clock, including repeated/skipped DST hours; "
            "never derive missing hourly production from daily totals."
        ),
    )

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
        )
    )
    async def get_generation(
        start_date: str, end_date: str | None = None, format: OutputFormat = "json",
        refresh: Annotated[bool, Field(strict=True)] = False,
    ) -> Annotated[CallToolResult, GenerationReport]:
        """Query a day or inclusive period, including across years, using YYYY-MM-DD dates.

        Return daily plant PV generation and its period sum in kWh. Missing source values
        become zero with source_missing flags. format='csv' returns a year/month by day
        matrix as text while retaining structured values and provenance. Saved measured dates,
        including real zeroes and explicitly flagged missing readings, need no portal access.
        refresh=True bypasses saved files to recheck the source. Querying alone does not rewrite saved exports.
        """
        try:
            report = await source.fetch(DateRange.parse(start_date, end_date), refresh=refresh)
            if report.missing_dates:
                LOGGER.warning("%d source-missing dates are represented as zero.", len(report.missing_dates))
            return CallToolResult(
                content=[TextContent(type="text", text=render(report, format))],
                structured_content=asdict(report),
            )
        except StatsError as error:
            LOGGER.warning("%s", error)
            raise ToolError(str(error)) from None

    if hourly_source is not None:
        @server.tool(
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
            )
        )
        async def get_hourly_generation(
            start_date: str, end_date: str | None = None, format: OutputFormat = "json",
            refresh: Annotated[bool, Field(strict=True)] = False,
        ) -> Annotated[CallToolResult, HourlyReport]:
            """Read hourly plant PV energy in kWh for completed dates, preferring saved hourly files.

            CSV has date rows and hour columns. Repeated clock hours have a #2 suffix;
            nonexistent clock hours are blank. Missing source readings are zero with flags.
            Failed source requests are errors, not all-zero days. No daily-to-hourly estimates.
            """
            try:
                report = await hourly_source.fetch(DateRange.parse(start_date, end_date), refresh=refresh)
                if report.missing_hours:
                    LOGGER.warning("%d source-missing hours are represented as zero.", len(report.missing_hours))
                text = hourly_csv(report) if format == "csv" else json.dumps(asdict(report), indent=2) + "\n"
                return CallToolResult(
                    content=[TextContent(type="text", text=text)], structured_content=asdict(report)
                )
            except StatsError as error:
                LOGGER.warning("%s", error)
                raise ToolError(str(error)) from None

    return server


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="solar-stats", description="Read-only FusionSolar history MCP and CLI.")
    subcommands = parser.add_subparsers(dest="command")
    query = subcommands.add_parser("query", help="Query saved/live history; print JSON/CSV or save exports.")
    query.add_argument("--start-date", required=True)
    query.add_argument("--end-date")
    query.add_argument("--format", choices=["json", "csv"], default="json")
    query.add_argument("--granularity", choices=["day", "hour"], default="day")
    query.add_argument("--refresh", action="store_true", help="Bypass stored files and query FusionSolar.")
    destination = query.add_mutually_exclusive_group()
    destination.add_argument("--output", type=Path)
    destination.add_argument("--output-dir", type=Path, help="Save separate generation-YEAR.csv files and metadata.")
    args = parser.parse_args(argv)
    if args.command == "query" and args.output_dir is not None and args.format != "csv":
        parser.error("--output-dir requires --format csv.")
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s: %(message)s")
    try:
        config = StatsConfig.from_environment()
        portal = (
            GitHubActionsSource(ActionsConfig.from_environment(), config.plant_id, config.time_zone, config.timeout_seconds)
            if config.backend == "github_actions" else FusionSolarSource(config)
        )
        source = StoredGenerationSource(
            portal, config.plant_id, config.data_dir, config.timeout_seconds
        )
        hourly_source = StoredHourlySource(
            portal, config.plant_id, config.data_dir, config.time_zone, config.timeout_seconds
        )
        if args.command == "query":
            interval = DateRange.parse(args.start_date, args.end_date)
            if args.granularity == "hour":
                if args.output_dir is not None:
                    asyncio.run(export_hourly_batches(
                        hourly_source, interval, args.output_dir, refresh=args.refresh
                    ))
                    return
                hourly = asyncio.run(hourly_source.fetch(interval, refresh=args.refresh))
                if hourly.missing_hours:
                    LOGGER.warning("%d source-missing hours are represented as zero.", len(hourly.missing_hours))
                text = hourly_csv(hourly) if args.format == "csv" else json.dumps(asdict(hourly), indent=2) + "\n"
                if args.output is None:
                    sys.stdout.write(text)
                else:
                    try:
                        args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        with saved_data_lock(args.output.parent, exclusive=True):
                            save_contents_unlocked(
                                text, hourly_metadata(hourly, text) if args.format == "csv" else None, args.output
                            )
                    except OSError as error:
                        raise StatsError("export_failed", f"Could not save the hourly export ({type(error).__name__}).") from error
                    print(f"Saved {args.output}", file=sys.stderr)
                return
            report = asyncio.run(source.fetch(interval, refresh=args.refresh))
            if report.missing_dates:
                LOGGER.warning("%d source-missing dates are represented as zero.", len(report.missing_dates))
            if args.output_dir is not None:
                for path in save_yearly_reports(report, args.output_dir):
                    print(f"Saved {path}", file=sys.stderr)
            elif args.output is None:
                sys.stdout.write(render(report, args.format))
            else:
                save_report(report, args.format, args.output)
                print(f"Saved {args.output}", file=sys.stderr)
        else:
            create_server(source, hourly_source).run(transport="stdio")
    except StatsError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
