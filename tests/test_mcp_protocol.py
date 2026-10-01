from __future__ import annotations

import asyncio
import os
import struct
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from solar_mcp.reader import HuaweiConfig, HuaweiSolarReader, SolarReadError


class ModbusStub:
    def __init__(self) -> None:
        self.requests: list[tuple[int, int, int, int]] = []
        self.registers: dict[int, int] = {}
        self.connections: set[asyncio.StreamWriter] = set()
        self.fail_reads = False
        self.allow_writes = False
        self.put_words(35300, [0, 0, 1000, 40125])
        self.put_words(40125, [1000])
        self.put_text(30000, 15, "SUN2000-8KTL-M0")
        self.put_text(30015, 10, "TEST-INVERTER-0001")
        self.put_text(30050, 15, "TEST-FIRMWARE")
        self.put_words(32080, [0, 0])
        self.put_words(32089, [0xA000])
        self.put_words(32106, [0, 12345])
        self.put_words(32114, [0, 0])

    def put_words(self, start: int, words: list[int]) -> None:
        self.registers.update((start + offset, value) for offset, value in enumerate(words))

    def put_text(self, start: int, count: int, text: str) -> None:
        data = text.encode("ascii").ljust(count * 2, b"\x00")
        self.put_words(start, list(struct.unpack(f">{count}H", data)))

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections.add(writer)
        try:
            while True:
                header = await reader.readexactly(7)
                transaction, protocol, length, unit = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(length - 1)
                function = pdu[0]
                address, count = struct.unpack(">HH", pdu[1:]) if len(pdu) == 5 else (-1, -1)
                self.requests.append((unit, function, address, count))
                if function == 6 and address == 40125:
                    if self.allow_writes:
                        self.put_words(40125, [count])
                        self.put_words(35301, [0, count])
                        response = pdu
                    else:
                        response = b"\x86\x80"
                elif function != 3:
                    response = bytes([function | 0x80, 1])
                elif self.fail_reads:
                    response = b"\x83\x04"
                elif any(address + offset not in self.registers for offset in range(count)):
                    response = b"\x83\x02"
                else:
                    words = [self.registers[address + offset] for offset in range(count)]
                    response = bytes([3, count * 2]) + struct.pack(f">{count}H", *words)
                writer.write(struct.pack(">HHHB", transaction, protocol, len(response) + 1, unit) + response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            self.connections.discard(writer)

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[int]:
        server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        try:
            yield server.sockets[0].getsockname()[1]
        finally:
            server.close()
            await server.wait_closed()
            for writer in tuple(self.connections):
                writer.close()
                await writer.wait_closed()


def test_stdio_tool_schema_live_cache_errors_and_read_only_wire_contract() -> None:
    async def check() -> None:
        stub = ModbusStub()
        async with stub.serve() as port:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "solar_mcp.server", "--inverter-host", "127.0.0.1", "--inverter-port", str(port)],
                env={
                    "SOLAR_EXPECTED_SERIAL": "TEST-INVERTER-0001",
                    "SOLAR_INVERTER_UNIT_ID": "1",
                    "SOLAR_ALLOW_CONTROL": "0",
                },
            )
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    assert [tool.name for tool in tools] == ["get_solar_status", "get_generation_limit"]
                    assert all(tool.annotations.read_only_hint for tool in tools)
                    assert tools[0].annotations is not None
                    assert tools[0].annotations.read_only_hint is True
                    assert tools[0].annotations.destructive_hint is False
                    assert tools[0].input_schema["properties"] == {}
                    assert tools[0].output_schema is not None
                    forbidden = await session.call_tool(
                        "set_generation_limit", {"percent": 50, "expected_current_percent": 100}
                    )
                    assert forbidden.is_error
                    assert not stub.requests
                    limit = await session.call_tool("get_generation_limit", {})
                    assert not limit.is_error
                    assert limit.structured_content["percent"] == 100
                    assert limit.structured_content["active_percent"] == 100
                    assert limit.structured_content["control_enabled"] is False
                    result = await session.call_tool("get_solar_status", {})
                    assert not result.is_error
                    data = result.structured_content
                    assert data is not None
                    assert set(data) == {
                        "observed_at", "age_seconds", "from_cache", "model", "software_version",
                        "device_status_code", "device_status", "generation_w", "daily_yield_kwh",
                        "lifetime_yield_kwh", "source",
                    }
                    assert data["generation_w"] == 0
                    assert data["lifetime_yield_kwh"] == 123.45
                    assert data["device_status_code"] == 0xA000
                    assert data["source"] == "huawei_modbus"
                    assert data["from_cache"] is False
                    reads = len(stub.requests)
                    cached = await session.call_tool("get_solar_status", {})
                    assert cached.structured_content is not None
                    assert cached.structured_content["from_cache"] is True
                    assert cached.structured_content["observed_at"] == data["observed_at"]
                    assert len(stub.requests) == reads
                    stub.fail_reads = True
                    for connection in tuple(stub.connections):
                        connection.close()
                        await connection.wait_closed()
                    await asyncio.sleep(0.05)
                    failed = await session.call_tool("get_solar_status", {})
                    assert failed.is_error
                    assert failed.structured_content is None
                    assert "no current telemetry" in str(failed.content)
                    stub.fail_reads = False
                    recovered = await session.call_tool("get_solar_status", {})
                    assert not recovered.is_error
                    assert recovered.structured_content is not None
                    assert recovered.structured_content["from_cache"] is False
            await asyncio.sleep(0.05)
            assert not stub.connections
        allowed = {
            (30000, 25), (30050, 15), (32080, 2), (32089, 1), (32106, 2), (32114, 2),
            (35300, 4), (40125, 1),
        }
        assert stub.requests
        assert all(unit == 1 and function == 3 and (address, count) in allowed
                   for unit, function, address, count in stub.requests)

    asyncio.run(check())


@pytest.mark.parametrize(
    ("address", "words"),
    [
        (32080, [0x7FFF, 0xFFFF]),
        (32089, [0xFFFF]),
        (32106, [0xFFFF, 0xFFFF]),
        (32114, [0xFFFF, 0xFFFF]),
    ],
)
def test_real_library_sentinel_decoding_is_reported_as_an_error(address: int, words: list[int]) -> None:
    async def check() -> None:
        stub = ModbusStub()
        stub.put_words(address, words)
        async with stub.serve() as port:
            reader = HuaweiSolarReader(HuaweiConfig("127.0.0.1", port=port))
            try:
                with pytest.raises(SolarReadError, match="Invalid or missing"):
                    await reader.read()
            finally:
                await reader.close()
        assert all(function == 3 for _, function, _, _ in stub.requests)

    asyncio.run(check())


def test_real_library_preserves_negative_power_and_unknown_status() -> None:
    async def check() -> None:
        stub = ModbusStub()
        stub.put_words(32080, [0xFFFF, 0xFFE7])
        stub.put_words(32089, [0x1234])
        async with stub.serve() as port:
            reader = HuaweiSolarReader(HuaweiConfig("127.0.0.1", port=port))
            try:
                result = await reader.read()
                assert result.generation_w == -25
                assert result.device_status_code == 0x1234
                assert result.device_status == "Unknown status (0x1234)"
            finally:
                await reader.close()

    asyncio.run(check())


def test_missing_configuration_is_stderr_only() -> None:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("SOLAR_")}
    result = subprocess.run(
        [sys.executable, "-m", "solar_mcp.server"], env=environment, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "Set SOLAR_INVERTER_HOST" in result.stderr


def test_stdio_percentage_control_schema_precise_write_readback_restore_and_permission_error(tmp_path) -> None:
    async def check() -> None:
        stub = ModbusStub()
        stub.allow_writes = True
        async with stub.serve() as port:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "solar_mcp.server", "--inverter-host", "127.0.0.1", "--inverter-port", str(port)],
                env={
                    "SOLAR_EXPECTED_SERIAL": "TEST-INVERTER-0001",
                    "SOLAR_INVERTER_UNIT_ID": "1",
                    "SOLAR_ALLOW_CONTROL": "1",
                    "SOLAR_CONTROL_LOG": str(tmp_path / "caps.jsonl"),
                },
            )
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                    assert set(tools) == {"get_solar_status", "get_generation_limit", "set_generation_limit"}
                    setter = tools["set_generation_limit"]
                    assert not setter.annotations.read_only_hint
                    assert setter.annotations.destructive_hint
                    assert not setter.annotations.idempotent_hint
                    assert set(setter.input_schema["required"]) == {"percent", "expected_current_percent"}
                    schema = setter.input_schema["properties"]["percent"]
                    assert schema["minimum"] == 0 and schema["maximum"] == 100
                    assert schema["multipleOf"] == 0.1
                    for invalid in [-1, 101, 12.34, True, "50"]:
                        rejected = await session.call_tool(
                            "set_generation_limit", {"percent": invalid, "expected_current_percent": 100}
                        )
                        assert rejected.is_error
                    assert not stub.requests
                    for percent, expected in [(57.9, 100), (0, 57.9), (100, 0)]:
                        result = await session.call_tool(
                            "set_generation_limit", {"percent": percent, "expected_current_percent": expected}
                        )
                        assert not result.is_error, str(result.content)
                        data = result.structured_content
                        assert data["previous_percent"] == expected
                        assert data["limit"]["percent"] == data["limit"]["active_percent"] == percent
                        assert data["changed"] is True
                    stale = await session.call_tool(
                        "set_generation_limit", {"percent": 50, "expected_current_percent": 0}
                    )
                    assert stale.is_error
                    stub.allow_writes = False
                    denied = await session.call_tool(
                        "set_generation_limit", {"percent": 50, "expected_current_percent": 100}
                    )
                    assert denied.is_error and denied.structured_content is None
                    assert "denied write permission" in str(denied.content)
                    assert stub.registers[40125] == 1000
        writes = [(address, value) for _, function, address, value in stub.requests if function != 3]
        assert writes == [(40125, 579), (40125, 0), (40125, 1000), (40125, 500)]
        assert all(unit == 1 and function in {3, 6} for unit, function, _, _ in stub.requests)

    asyncio.run(check())


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"SOLAR_ALLOW_CONTROL": "yes"}, "must be 0 or 1"),
        ({"SOLAR_ALLOW_CONTROL": "1"}, "SOLAR_EXPECTED_SERIAL is required"),
        (
            {"SOLAR_ALLOW_CONTROL": "1", "SOLAR_EXPECTED_SERIAL": "TEST", "SOLAR_CONTROL_LOG": "relative"},
            "must be an absolute path",
        ),
    ],
)
def test_invalid_control_configuration_is_stderr_only(settings, message) -> None:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("SOLAR_")}
    environment.update(SOLAR_INVERTER_HOST="127.0.0.1", **settings)
    result = subprocess.run(
        [sys.executable, "-m", "solar_mcp.server"], env=environment, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert message in result.stderr
