from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Protocol

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from .browser import FusionSolarSource, StatsConfig
from .models import DateRange, GenerationReport, StatsError
from .output import OutputFormat, render, save_report, save_yearly_reports
from .stored import StoredGenerationSource

LOGGER = logging.getLogger(__name__)


class GenerationSource(Protocol):
    async def fetch(self, interval: DateRange, *, refresh: bool = False) -> GenerationReport: ...


def create_server(source: GenerationSource) -> MCPServer[None]:
    server = MCPServer(
        "solar-stats",
        instructions=(
            "Read-only whole-plant PV generation history in kWh, not instantaneous kW or grid export. "
            "Queries prefer validated local yearly CSV records; only uncovered dates go to FusionSolar. "
            "refresh=true forces a live query. Source-missing dates are zero-filled "
            "but identified in metadata; never describe incomplete totals as all actual production. "
            "Dates follow the plant report calendar. No inverter controls or credential tool arguments/results."
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

    return server


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="solar-stats", description="Read-only FusionSolar history MCP and CLI.")
    subcommands = parser.add_subparsers(dest="command")
    query = subcommands.add_parser("query", help="Query saved/live history; print JSON/CSV or save exports.")
    query.add_argument("--start-date", required=True)
    query.add_argument("--end-date")
    query.add_argument("--format", choices=["json", "csv"], default="json")
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
        source = StoredGenerationSource(
            FusionSolarSource(config), config.plant_id, config.data_dir, config.timeout_seconds
        )
        if args.command == "query":
            interval = DateRange.parse(args.start_date, args.end_date)
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
            create_server(source).run(transport="stdio")
    except StatsError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
