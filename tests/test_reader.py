from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from huawei_solar import ReadException, Result
from huawei_solar import register_names as rn

from solar_mcp import reader as reader_module
from solar_mcp.reader import HuaweiConfig, HuaweiSolarReader, SolarReadError


class FakeClient:
    def __init__(self) -> None:
        self.connected = False
        self.connects = 0
        self.disconnects = 0
        self.delay = 0.0
        self.failure: Exception | None = None
        self.close_failure = False
        self.calls: list[object] = []
        self.values: dict[rn.RegisterName, object] = {
            rn.MODEL_NAME: "SUN2000-8KTL-M0",
            rn.SERIAL_NUMBER: "TEST-INVERTER-0001",
            rn.SOFTWARE_VERSION: "TEST-FIRMWARE",
            rn.ACTIVE_POWER: 0,
            rn.ACCUMULATED_YIELD_ENERGY: 1234.56,
            rn.DAILY_YIELD_ENERGY: 0,
        }
        self.status: list[int] = [0xA000]

    async def connect(self) -> None:
        self.connects += 1
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.connected = False
        if self.close_failure:
            raise OSError("test close failure")

    async def get(self, name: rn.RegisterName) -> Result[object]:
        self.calls.append(name)
        await asyncio.sleep(self.delay)
        if self.failure is not None:
            raise self.failure
        return Result(self.values[name], None)

    async def get_multiple(self, names: list[rn.RegisterName]) -> list[Result[object]]:
        return [await self.get(name) for name in names]

    async def read_holding_registers(self, start_address: int, quantity: int) -> list[int]:
        self.calls.append((start_address, quantity))
        assert (start_address, quantity) == (32089, 1)
        return self.status

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"Unexpected client operation (including any login/write): {name}")


def make_reader(client: FakeClient) -> HuaweiSolarReader:
    return HuaweiSolarReader(HuaweiConfig("localhost"), client_factory=lambda _: client)


def test_zero_signed_energy_cache_and_shutdown() -> None:
    async def check() -> None:
        client = FakeClient()
        reader = make_reader(client)
        first = await reader.read()
        assert first.generation_w == 0
        assert first.daily_yield_kwh == 0
        assert first.lifetime_yield_kwh == 1234.56
        assert first.device_status_code == 0xA000
        assert first.device_status == "Standby: no irradiation"
        assert first.observed_at.utcoffset() == timedelta(0)
        assert not first.from_cache
        count = len(client.calls)
        second = await reader.read()
        assert second.from_cache
        assert second.observed_at == first.observed_at
        assert first.age_seconds <= second.age_seconds < 30
        assert len(client.calls) == count
        reader._cache_time -= 30
        client.values[rn.ACTIVE_POWER] = -25
        third = await reader.read()
        assert not third.from_cache
        assert third.generation_w == -25
        assert third.observed_at >= first.observed_at
        assert client.connects == 1
        await reader.close()
        await reader.close()
        assert client.disconnects == 1
        with pytest.raises(SolarReadError, match="closed"):
            await reader.read()

    asyncio.run(check())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (rn.ACTIVE_POWER, None),
        (rn.ACTIVE_POWER, True),
        (rn.ACTIVE_POWER, "0"),
        (rn.ACTIVE_POWER, float("nan")),
        (rn.DAILY_YIELD_ENERGY, float("inf")),
        (rn.DAILY_YIELD_ENERGY, -1),
        (rn.ACCUMULATED_YIELD_ENERGY, None),
        (rn.MODEL_NAME, ""),
        (rn.SOFTWARE_VERSION, None),
        (rn.SERIAL_NUMBER, ""),
    ],
)
def test_invalid_values_are_not_zero(name: rn.RegisterName, value: object) -> None:
    async def check() -> None:
        client = FakeClient()
        client.values[name] = value
        reader = make_reader(client)
        with pytest.raises(SolarReadError, match="Invalid or missing|Invalid "):
            await reader.read()
        assert not client.connected
        assert client.disconnects == 1
        assert reader._cache is None
        await reader.close()

    asyncio.run(check())


@pytest.mark.parametrize("status", [[], [0xFFFF], [-1], [True], [1, 2]])
def test_invalid_status_is_an_error(status: list[int]) -> None:
    async def check() -> None:
        client = FakeClient()
        client.status = status
        reader = make_reader(client)
        with pytest.raises(SolarReadError, match="operating status"):
            await reader.read()
        await reader.close()

    asyncio.run(check())


def test_unknown_status_is_preserved() -> None:
    async def check() -> None:
        client = FakeClient()
        client.status = [0x1234]
        reader = make_reader(client)
        snapshot = await reader.read()
        assert snapshot.device_status_code == 0x1234
        assert snapshot.device_status == "Unknown status (0x1234)"
        await reader.close()

    asyncio.run(check())


@pytest.mark.parametrize("failure", [ReadException("unsupported"), TimeoutError("timeout"), OSError("offline")])
def test_failed_refresh_has_no_stale_success_and_next_call_recovers(failure: Exception) -> None:
    async def check() -> None:
        client = FakeClient()
        reader = make_reader(client)
        first = await reader.read()
        reader._cache_time -= 30
        client.failure = failure
        with pytest.raises(SolarReadError) as error:
            await reader.read()
        assert first.observed_at.isoformat() in str(error.value)
        assert reader._cache is None
        assert client.connects == 1
        assert client.disconnects == 1
        client.failure = None
        recovered = await reader.read()
        assert not recovered.from_cache
        assert client.connects == 2
        assert client.calls.count(rn.SERIAL_NUMBER) == 2
        await reader.close()

    asyncio.run(check())


def test_identity_is_checked_on_reconnect_even_with_unexpired_cache() -> None:
    async def check() -> None:
        client = FakeClient()
        reader = make_reader(client)
        await reader.read()
        client.connected = False
        client.values[rn.SERIAL_NUMBER] = "DIFFERENT-DEVICE"
        with pytest.raises(SolarReadError, match="identity changed"):
            await reader.read()
        assert client.connects == 2
        assert not client.connected
        assert client.calls.count(rn.ACTIVE_POWER) == 1
        await reader.close()

    asyncio.run(check())


def test_configured_identity_and_model_are_enforced() -> None:
    async def check() -> None:
        client = FakeClient()
        reader = HuaweiSolarReader(
            HuaweiConfig("localhost", expected_serial="OTHER-DEVICE"),
            client_factory=lambda _: client,
        )
        with pytest.raises(SolarReadError, match="SOLAR_EXPECTED_SERIAL"):
            await reader.read()
        await reader.close()
        client.values[rn.MODEL_NAME] = "SDongleA-05"
        reader = make_reader(client)
        with pytest.raises(SolarReadError, match="not a supported"):
            await reader.read()
        await reader.close()

    asyncio.run(check())


def test_concurrent_requests_share_one_serialized_refresh() -> None:
    async def check() -> None:
        client = FakeClient()
        client.delay = 0.002
        reader = make_reader(client)
        results = await asyncio.gather(*(reader.read() for _ in range(8)))
        assert sum(not result.from_cache for result in results) == 1
        assert client.connects == 1
        assert client.calls.count(rn.ACTIVE_POWER) == 1
        await reader.close()

    asyncio.run(check())


def test_deadline_and_cancellation_close_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check() -> None:
        client = FakeClient()
        client.delay = 1
        reader = make_reader(client)
        monkeypatch.setattr(reader_module, "REFRESH_TIMEOUT_SECONDS", 0.02)
        with pytest.raises(SolarReadError, match="Timed out"):
            await reader.read()
        assert not client.connected
        assert client.disconnects == 1
        monkeypatch.setattr(reader_module, "REFRESH_TIMEOUT_SECONDS", 30)
        pending = asyncio.create_task(reader.read())
        await asyncio.sleep(0.01)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not client.connected
        assert client.disconnects == 2
        await reader.close()

    asyncio.run(check())


def test_close_failure_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    async def check() -> None:
        client = FakeClient()
        reader = make_reader(client)
        await reader.read()
        client.close_failure = True
        await reader.close()
        assert "Failed to close inverter connection: OSError" in caplog.text

    asyncio.run(check())


@pytest.mark.parametrize(
    "config",
    [
        {"host": ""},
        {"host": "http://localhost"},
        {"host": "local host"},
        {"host": "localhost", "port": 0},
        {"host": "localhost", "port": 65536},
        {"host": "localhost", "unit_id": -1},
        {"host": "localhost", "unit_id": 248},
        {"host": "localhost", "expected_serial": " "},
    ],
)
def test_invalid_configuration(config: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        HuaweiConfig(**config)
