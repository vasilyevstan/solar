from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .models import SolarSnapshot
from .reader import HuaweiConfig, HuaweiSolarReader, SolarReadError

LOGGER = logging.getLogger(__name__)


def create_server(reader: HuaweiSolarReader) -> MCPServer[None]:
    @asynccontextmanager
    async def lifespan(_server: MCPServer[None]) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            await reader.close()

    server = MCPServer(
        "solar-mcp",
        lifespan=lifespan,
        instructions=(
            "Read-only local Huawei inverter telemetry. Generation is not grid export, "
            "household consumption, or surplus. Respect observation time and cache age. "
            "An error means current readings are unavailable, not zero generation. "
            "No adjustment or write tools are available."
        ),
    )

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    async def get_solar_status() -> SolarSnapshot:
        """Read inverter identity, software, power, energy, and status; cache for at most 30 seconds."""
        try:
            return await reader.read()
        except SolarReadError as error:
            LOGGER.warning("%s", error)
            raise ToolError(str(error)) from None

    return server


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="solar-mcp", description="Read-only Huawei inverter MCP (stdio).")
    parser.add_argument("--inverter-host", default=os.environ.get("SOLAR_INVERTER_HOST"))
    parser.add_argument(
        "--inverter-port", type=int, default=os.environ.get("SOLAR_INVERTER_PORT", "502")
    )
    parser.add_argument("--unit-id", type=int, default=os.environ.get("SOLAR_INVERTER_UNIT_ID", "1"))
    args = parser.parse_args(argv)
    if not args.inverter_host:
        parser.error("Set SOLAR_INVERTER_HOST or pass --inverter-host.")
    try:
        config = HuaweiConfig(
            host=args.inverter_host,
            port=args.inverter_port,
            unit_id=args.unit_id,
            expected_serial=os.environ.get("SOLAR_EXPECTED_SERIAL"),
        )
    except ValueError as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s: %(message)s")
    create_server(HuaweiSolarReader(config)).run(transport="stdio")


if __name__ == "__main__":
    main()
