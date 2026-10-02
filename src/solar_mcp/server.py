from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .models import GenerationLimit, GenerationLimitChange, PowerLimitChange, PowerLimitMode, SolarSnapshot
from .reader import HuaweiConfig, HuaweiSolarReader, SolarControlError, SolarReadError

LOGGER = logging.getLogger(__name__)
Percentage = Annotated[float, Field(strict=True, ge=0, le=100, multiple_of=0.1)]
PowerValue = Annotated[float, Field(strict=True, ge=0, lt=0xFFFFFFFF)]


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
            "Local Huawei inverter telemetry. Generation is not grid export, "
            "household consumption, or surplus. Respect observation time and cache age. "
            "An error means current readings are unavailable, not zero generation. "
            "Generation caps limit power (kW), not accumulated energy (kWh). "
            + (
                "Only change the cap for an explicit user request. Read the current cap first. "
                "Changes can persist after shutdown; never assume an error means the old cap remains. "
                "Configured and active caps or modes can differ; never claim an active cap from stored configuration alone. "
                "Use set_power_limit for whole-watt caps or explicit mode changes, with fresh mode/value expectations. "
                "A cap is a maximum, not a guarantee of constant output. Restore the saved previous mode/value explicitly."
                if reader.control_enabled
                else "Control is disabled; no write tools are available."
            )
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

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
        )
    )
    async def get_generation_limit() -> GenerationLimit:
        """Read the active cap mode and its configured/active values without caching or changing it.

        mode='percent' returns percent/active_percent; mode='watts' returns watts/active_watts.
        Values in the other unit are null. Other control modes are rejected, not overwritten.
        """
        try:
            return await reader.get_generation_limit()
        except SolarControlError as error:
            LOGGER.warning("%s", error)
            raise ToolError(str(error)) from None

    if reader.control_enabled:
        @server.tool(
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
            )
        )
        async def set_generation_limit(
            percent: Percentage,
            expected_current_percent: Percentage,
            expected_active_percent: Percentage | None = None,
        ) -> GenerationLimitChange:
            """Set a user-requested percentage power cap (0-100, step 0.1), not a kWh/export limit.

            Read get_generation_limit first. Pass its percent as expected_current_percent and
            its active_percent as expected_active_percent (defaults to expected_current_percent).
            Zero can stop generation; 100 removes this percentage restriction, not other limits.
            Only existing percentage mode is supported. No blind retries or automatic rollback.
            To restore, set the saved previous_percent with both freshly read expectations.
            Configuration is verified separately from active readback; heed the warning when they differ.
            An unchanged result does not test write permission.
            """
            try:
                return await reader.set_generation_limit(
                    percent, expected_current_percent, expected_active_percent
                )
            except SolarControlError as error:
                LOGGER.warning("%s", error)
                raise ToolError(str(error)) from None

        @server.tool(
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
            )
        )
        async def set_power_limit(
            mode: PowerLimitMode,
            value: PowerValue,
            expected_mode: PowerLimitMode,
            expected_current_value: PowerValue,
            expected_active_value: PowerValue,
        ) -> PowerLimitChange:
            """Explicitly select a percentage or fixed-watt generation cap, including switching modes.

            Read get_generation_limit first. Match expected_mode and both values in that mode's
            unit: percent/active_percent or watts/active_watts. Percent is 0-100 in 0.1 steps;
            watts must be whole watts from 0 through the freshly read hardware Pmax.
            This sends one journaled command even when the values appear unchanged, allowing
            explicit restoration/reassertion after a pending mode change. No blind write retries.
            configuration_verified verifies the requested register; limit describes the active
            mode's readbacks. Check active_readback_matches before claiming the cap is active.
            Save previous for restoration with this tool and fresh expectations. Caps persist
            after disconnect; zero can stop generation. This is not a grid-export target.
            """
            try:
                return await reader.set_power_limit(
                    mode, value, expected_mode, expected_current_value, expected_active_value
                )
            except SolarControlError as error:
                LOGGER.warning("%s", error)
                raise ToolError(str(error)) from None

    return server


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="solar-mcp", description="Huawei inverter MCP (stdio; read-only by default).")
    parser.add_argument("--inverter-host", default=os.environ.get("SOLAR_INVERTER_HOST"))
    parser.add_argument(
        "--inverter-port", type=int, default=os.environ.get("SOLAR_INVERTER_PORT", "502")
    )
    parser.add_argument("--unit-id", type=int, default=os.environ.get("SOLAR_INVERTER_UNIT_ID", "1"))
    args = parser.parse_args(argv)
    if not args.inverter_host:
        parser.error("Set SOLAR_INVERTER_HOST or pass --inverter-host.")
    allow_control = os.environ.get("SOLAR_ALLOW_CONTROL", "0")
    if allow_control not in {"0", "1"}:
        parser.error("SOLAR_ALLOW_CONTROL must be 0 or 1.")
    try:
        config = HuaweiConfig(
            host=args.inverter_host,
            port=args.inverter_port,
            unit_id=args.unit_id,
            expected_serial=os.environ.get("SOLAR_EXPECTED_SERIAL"),
            allow_control=allow_control == "1",
            control_log=Path(
                os.environ.get(
                    "SOLAR_CONTROL_LOG",
                    str(Path.home() / ".local/state/solar-mcp/generation-caps.jsonl"),
                )
            ),
            installer_password=os.environ.get("SOLAR_INSTALLER_PASSWORD"),
        )
    except ValueError as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s: %(message)s")
    create_server(HuaweiSolarReader(config)).run(transport="stdio")


if __name__ == "__main__":
    main()
