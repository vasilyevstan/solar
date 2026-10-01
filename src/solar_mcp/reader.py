from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from huawei_solar import AsyncHuaweiSolarClient, HuaweiSolarException
from huawei_solar import register_names as rn
from huawei_solar.register_definitions import Result
from huawei_solar.register_values import DEVICE_STATUS_DEFINITIONS
from huawei_solar.registers import REGISTERS
from tmodbus import AsyncSmartTransport, AsyncTcpTransport
from tmodbus.exceptions import TModbusError

from .models import SolarSnapshot

LOGGER = logging.getLogger(__name__)
CACHE_SECONDS = 30.0
REFRESH_TIMEOUT_SECONDS = 30.0
CLOSE_TIMEOUT_SECONDS = 2.0


class SolarReadError(Exception):
    """A telemetry failure safe to report to the MCP client."""


@dataclass(frozen=True)
class HuaweiConfig:
    host: str
    port: int = 502
    unit_id: int = 1
    expected_serial: str | None = None

    def __post_init__(self) -> None:
        if not self.host or any(c.isspace() for c in self.host) or "/" in self.host:
            raise ValueError("Set a hostname or IP address, without a URL or whitespace.")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Inverter port must be between 1 and 65535.")
        if type(self.unit_id) is not int or not 0 <= self.unit_id <= 247:
            raise ValueError("Inverter unit ID must be between 0 and 247.")
        if self.expected_serial is not None and (
            not self.expected_serial.strip() or not self.expected_serial.isprintable()
        ):
            raise ValueError("Expected serial must be nonempty printable text.")


class ReadClient(Protocol):
    @property
    def connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get(self, name: rn.RegisterName) -> Result[object]: ...

    async def get_multiple(self, names: list[rn.RegisterName]) -> list[Result[object]]: ...

    async def read_holding_registers(self, start_address: int, quantity: int) -> list[int]: ...


def create_read_client(config: HuaweiConfig) -> ReadClient:
    # Reconnect in the reader, so every new connection must pass identity checks.
    transport = AsyncSmartTransport(
        AsyncTcpTransport(config.host, config.port, timeout=10, connect_timeout=10),
        wait_after_connect=1.0,
        wait_between_requests=0.05,
        auto_reconnect=False,
        retry_on_device_busy=False,
        retry_on_device_failure=False,
    )
    return AsyncHuaweiSolarClient(transport, unit_id=config.unit_id)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise SolarReadError(f"Invalid or missing {field}.")
    return value.strip()


def _number(value: object, field: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SolarReadError(f"Invalid or missing {field}.")
    number = float(value)
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise SolarReadError(f"Invalid {field}.")
    return number


class HuaweiSolarReader:
    def __init__(
        self,
        config: HuaweiConfig,
        *,
        client_factory: Callable[[HuaweiConfig], ReadClient] = create_read_client,
    ) -> None:
        self._config = config
        self._client_factory = client_factory
        self._client: ReadClient | None = None
        self._identity: tuple[str, str] | None = None
        self._model = ""
        self._version = ""
        self._cache: SolarSnapshot | None = None
        self._cache_time = 0.0
        self._last_observation: datetime | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                    await client.disconnect()
            except (TimeoutError, OSError, TModbusError) as error:
                LOGGER.warning("Failed to close inverter connection: %s", type(error).__name__)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            self._cache = None
            await self._disconnect()

    async def _connect(self) -> ReadClient:
        if self._client is not None and self._client.connected:
            return self._client
        await self._disconnect()
        self._client = self._client_factory(self._config)
        await self._client.connect()
        identity = await self._client.get_multiple([rn.MODEL_NAME, rn.SERIAL_NUMBER])
        if len(identity) != 2:
            raise SolarReadError("Incomplete inverter identity.")
        model = _text(identity[0].value, "inverter model")
        serial = _text(identity[1].value, "inverter identity")
        if not model.startswith("SUN2000-"):
            raise SolarReadError("The configured unit is not a supported SUN2000 inverter.")
        if self._config.expected_serial is not None and serial != self._config.expected_serial:
            raise SolarReadError("Inverter identity does not match SOLAR_EXPECTED_SERIAL.")
        if self._identity is not None and self._identity != (model, serial):
            raise SolarReadError("Inverter identity changed since the previous connection.")
        self._version = _text((await self._client.get(rn.SOFTWARE_VERSION)).value, "software version")
        self._model = model
        self._identity = model, serial
        return self._client

    async def _refresh(self) -> SolarSnapshot:
        client = await self._connect()
        observed_at = datetime.now(UTC)
        observed_monotonic = time.monotonic()
        power = _number((await client.get(rn.ACTIVE_POWER)).value, "generation")
        status_words = await client.read_holding_registers(REGISTERS[rn.DEVICE_STATUS].register, 1)
        if (
            len(status_words) != 1
            or type(status_words[0]) is not int
            or not 0 <= status_words[0] < 0xFFFF
        ):
            raise SolarReadError("Invalid or missing operating status.")
        status_code = status_words[0]
        status = DEVICE_STATUS_DEFINITIONS.get(status_code, f"Unknown status (0x{status_code:04X})")
        lifetime = _number(
            (await client.get(rn.ACCUMULATED_YIELD_ENERGY)).value, "lifetime yield", nonnegative=True
        )
        daily = _number(
            (await client.get(rn.DAILY_YIELD_ENERGY)).value, "daily yield", nonnegative=True
        )
        snapshot = SolarSnapshot(
            observed_at=observed_at,
            age_seconds=time.monotonic() - observed_monotonic,
            from_cache=False,
            model=self._model,
            software_version=self._version,
            device_status_code=status_code,
            device_status=status,
            generation_w=power,
            daily_yield_kwh=daily,
            lifetime_yield_kwh=lifetime,
        )
        self._cache = snapshot
        self._cache_time = observed_monotonic
        self._last_observation = observed_at
        return snapshot

    async def _read_locked(self) -> SolarSnapshot:
        async with self._lock:
            if self._closed:
                raise SolarReadError("The inverter reader is closed.")
            if self._cache is not None and self._client is not None and self._client.connected:
                age = time.monotonic() - self._cache_time
                if age < CACHE_SECONDS:
                    return replace(self._cache, from_cache=True, age_seconds=age)
            try:
                return await self._refresh()
            except (SolarReadError, HuaweiSolarException, TModbusError, OSError):
                self._cache = None
                await self._disconnect()
                raise
            except asyncio.CancelledError:
                self._cache = None
                await self._disconnect()
                raise

    async def read(self) -> SolarSnapshot:
        try:
            async with asyncio.timeout(REFRESH_TIMEOUT_SECONDS):
                return await self._read_locked()
        except TimeoutError as error:
            message = "Timed out reading the inverter; no current telemetry is available."
            cause: Exception = error
        except SolarReadError as error:
            message, cause = str(error), error
        except (HuaweiSolarException, TModbusError, OSError) as error:
            message = f"Inverter read failed ({type(error).__name__}); no current telemetry is available."
            cause = error
        if self._last_observation is not None:
            message += f" Last successful observation: {self._last_observation.isoformat()}."
        raise SolarReadError(message) from cause
