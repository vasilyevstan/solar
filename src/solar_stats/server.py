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

from .browser import FusionSolarSource, StatsConfig
from .models import DateRange, GenerationReport, StatsError
from .output import OutputFormat, render, save_report

LOGGER = logging.getLogger(__name__)


class GenerationSource(Protocol):
    async def fetch(self, interval: DateRange) -> GenerationReport: ...


def create_server(source: GenerationSource) -> MCPServer[None]:
    server = MCPServer(
        "solar-stats",
        instructions=(
            "Read-only whole-plant PV generation history in kWh, not instantaneous kW or grid export. "
            "Every query reads FusionSolar, not a local CSV cache. Source-missing dates are zero-filled "
            "but identified in metadata; never describe incomplete totals as all actual production. "
            "Dates follow the plant report calendar. No inverter controls or password handling."
        ),
    )

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
        )
    )
    async def get_generation(
        start_date: str, end_date: str | None = None, format: OutputFormat = "json"
    ) -> Annotated[CallToolResult, GenerationReport]:
        """Query a day or inclusive period, including across years, using YYYY-MM-DD dates.

        Return daily plant PV generation and its period sum in kWh. Missing source values
        become zero with source_missing flags. format='csv' returns a year/month by day
        matrix as text while retaining structured values and provenance.
        """
        try:
            report = await source.fetch(DateRange.parse(start_date, end_date))
            if report.missing_dates:
                LOGGER.warning("%d source-missing dates were filled with zero.", len(report.missing_dates))
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
    query = subcommands.add_parser("query", help="Query live history; print JSON/CSV to stdout or save an export.")
    query.add_argument("--start-date", required=True)
    query.add_argument("--end-date")
    query.add_argument("--format", choices=["json", "csv"], default="json")
    query.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s: %(message)s")
    try:
        source = FusionSolarSource(StatsConfig.from_environment())
        if args.command == "query":
            interval = DateRange.parse(args.start_date, args.end_date)
            report = asyncio.run(source.fetch(interval))
            if report.missing_dates:
                LOGGER.warning("%d source-missing dates were filled with zero.", len(report.missing_dates))
            if args.output is None:
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
