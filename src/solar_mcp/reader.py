from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from huawei_solar import AsyncHuaweiSolarClient, HuaweiSolarException
from huawei_solar import register_names as rn
from huawei_solar.modbus_pdu import PermissionDeniedError
from huawei_solar.register_definitions import Result
from huawei_solar.register_values import DEVICE_STATUS_DEFINITIONS
from huawei_solar.registers import REGISTERS
from tmodbus import AsyncSmartTransport, AsyncTcpTransport
from tmodbus.exceptions import TModbusError

from .models import GenerationLimit, GenerationLimitChange, SolarSnapshot

LOGGER = logging.getLogger(__name__)
CACHE_SECONDS = 30.0
REFRESH_TIMEOUT_SECONDS = 30.0
CLOSE_TIMEOUT_SECONDS = 2.0
CAP_REGISTER = REGISTERS[rn.ACTIVE_POWER_PERCENTAGE_DERATING].register


class SolarReadError(Exception):
    """A telemetry failure safe to report to the MCP client."""


class SolarControlError(Exception):
    """A cap-control failure safe to report to the MCP client."""


@dataclass(frozen=True)
class HuaweiConfig:
    host: str
    port: int = 502
    unit_id: int = 1
    expected_serial: str | None = None
    allow_control: bool = False
    control_log: Path = field(
        default_factory=lambda: Path.home() / ".local/state/solar-mcp/generation-caps.jsonl"
    )
    installer_password: str | None = field(default=None, repr=False)

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
        if type(self.allow_control) is not bool:
            raise ValueError("Control enablement must be a boolean.")
        if self.allow_control and self.expected_serial is None:
            raise ValueError("SOLAR_EXPECTED_SERIAL is required when generation-cap control is enabled.")
        if self.allow_control and not self.control_log.is_absolute():
            raise ValueError("SOLAR_CONTROL_LOG must be an absolute path.")
        if self.installer_password is not None and not self.installer_password:
            raise ValueError("SOLAR_INSTALLER_PASSWORD must not be empty when configured.")


class ReadClient(Protocol):
    @property
    def connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get(self, name: rn.RegisterName) -> Result[object]: ...

    async def get_multiple(self, names: list[rn.RegisterName]) -> list[Result[object]]: ...

    async def read_holding_registers(self, start_address: int, quantity: int) -> list[int]: ...

    async def set(self, name: rn.RegisterName, value: float) -> bool: ...

    async def login(self, username: str, password: str) -> bool: ...

    async def heartbeat(self) -> bool: ...


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


def _percent_tenths(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SolarControlError("A percentage must be a number from 0 to 100, in steps of 0.1.")
    scaled = Decimal(str(value)) * 10
    if not scaled.is_finite() or not 0 <= scaled <= 1000 or scaled != scaled.to_integral_value():
        raise SolarControlError("A percentage must be a number from 0 to 100, in steps of 0.1.")
    return int(scaled)


def _change_result(
    previous: float, actual: GenerationLimit, *, changed: bool, request_id: str | None
) -> GenerationLimitChange:
    active_matches = actual.percent == actual.active_percent
    warning = None
    if not active_matches:
        warning = (
            "The configured cap is verified, but the active-adjustment readback differs. "
            "The output restriction is not confirmed; it may be pending or overridden."
        )
        LOGGER.warning("%s", warning)
    return GenerationLimitChange(previous, actual, changed, request_id, active_matches, warning)


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

    @property
    def control_enabled(self) -> bool:
        return self._config.allow_control

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
        await self._verify_identity(self._client)
        self._version = _text((await self._client.get(rn.SOFTWARE_VERSION)).value, "software version")
        return self._client

    async def _verify_identity(self, client: ReadClient) -> None:
        identity = await client.get_multiple([rn.MODEL_NAME, rn.SERIAL_NUMBER])
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
        self._model = model
        self._identity = model, serial

    async def _cap_operation[T](self, operation: Callable[[ReadClient], Awaitable[T]]) -> T:
        async with self._lock:
            if self._closed:
                raise SolarControlError("The inverter reader is closed.")
            try:
                return await operation(await self._connect())
            except (SolarReadError, SolarControlError, HuaweiSolarException, TModbusError, OSError):
                self._cache = None
                await self._disconnect()
                raise
            except asyncio.CancelledError:
                self._cache = None
                await self._disconnect()
                raise

    async def _read_generation_limit(self, client: ReadClient) -> GenerationLimit:
        configured = _percent_tenths((await client.get(rn.ACTIVE_POWER_PERCENTAGE_DERATING)).value)
        values = await client.get_multiple(
            [
                rn.ACTIVE_POWER_ADJUSTMENT_MODE,
                rn.ACTIVE_POWER_ADJUSTMENT_VALUE,
                rn.ACTIVE_POWER_ADJUSTMENT_COMMAND,
            ]
        )
        if len(values) != 3 or any(type(result.value) is not int for result in values):
            raise SolarControlError("Invalid or missing active generation-cap data.")
        mode, active, command = (result.value for result in values)
        if mode != 0 or command != CAP_REGISTER:
            raise SolarControlError(
                "The inverter is not using the supported percentage-cap mode. "
                "Refusing to replace another control mode."
            )
        if not isinstance(active, int) or not 0 <= active <= 1000:
            raise SolarControlError("Invalid active generation-cap percentage.")
        return GenerationLimit(
            observed_at=datetime.now(UTC),
            percent=configured / 10,
            active_percent=active / 10,
            control_enabled=self.control_enabled,
        )

    async def get_generation_limit(self) -> GenerationLimit:
        try:
            async with asyncio.timeout(REFRESH_TIMEOUT_SECONDS):
                return await self._cap_operation(self._read_generation_limit)
        except (SolarReadError, SolarControlError) as error:
            raise SolarControlError(str(error)) from error
        except (HuaweiSolarException, TModbusError, OSError) as error:
            raise SolarControlError(
                f"Generation-cap read failed ({type(error).__name__}); no current cap is available."
            ) from error

    def _record_cap_event(self, record: dict[str, object], event: str) -> None:
        path = self._config.control_log
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as log:
            os.fchmod(log.fileno(), 0o600)
            log.write(json.dumps({**record, "event": event, "at": datetime.now(UTC).isoformat()}) + "\n")
            log.flush()
            os.fsync(log.fileno())

    async def set_generation_limit(
        self,
        percent: float,
        expected_current_percent: float,
        expected_active_percent: float | None = None,
    ) -> GenerationLimitChange:
        if not self.control_enabled:
            raise SolarControlError("Generation-cap control is disabled; set SOLAR_ALLOW_CONTROL=1 locally.")
        requested = _percent_tenths(percent)
        expected = _percent_tenths(expected_current_percent)
        expected_active = expected if expected_active_percent is None else _percent_tenths(expected_active_percent)
        write_attempted = False

        async def change(client: ReadClient) -> GenerationLimitChange:
            nonlocal write_attempted
            await self._verify_identity(client)
            previous = await self._read_generation_limit(client)
            if previous.percent != expected / 10 or previous.active_percent != expected_active / 10:
                raise SolarControlError(
                    "The current cap differs from the expected value or active readback. "
                    "Read get_generation_limit again; no cap write was attempted."
                )
            if requested == expected:
                return _change_result(previous.percent, previous, changed=False, request_id=None)
            if self._config.installer_password is not None:
                if not await client.login("installer", self._config.installer_password):
                    raise SolarControlError("Installer login failed; no cap write was attempted.")
                if not await client.heartbeat():
                    raise SolarControlError("Installer session heartbeat failed; no cap write was attempted.")
                current = await self._read_generation_limit(client)
                if current.percent != previous.percent or current.active_percent != previous.active_percent:
                    raise SolarControlError("The cap changed during login; no cap write was attempted.")
            request_id = str(uuid4())
            record: dict[str, object] = {
                "request_id": request_id,
                "model": self._model,
                "serial": self._config.expected_serial,
                "previous_percent": previous.percent,
                "previous_active_percent": previous.active_percent,
                "requested_percent": requested / 10,
            }
            self._record_cap_event(record, "prepared")
            self._cache = None
            write_attempted = True
            acknowledged = await client.set(rn.ACTIVE_POWER_PERCENTAGE_DERATING, requested / 10)
            if not acknowledged:
                raise SolarControlError("The inverter did not acknowledge the requested cap.")
            for attempt in range(3):
                actual = await self._read_generation_limit(client)
                if actual.percent == requested / 10 and actual.active_percent == requested / 10:
                    break
                if attempt < 2:
                    await asyncio.sleep(0.25)
            if actual.percent != requested / 10:
                raise SolarControlError("The configured cap did not match the request.")
            self._record_cap_event(
                {
                    **record,
                    "active_percent": actual.active_percent,
                    "active_readback_matches": actual.active_percent == actual.percent,
                },
                "verified",
            )
            return _change_result(previous.percent, actual, changed=True, request_id=request_id)

        try:
            async with asyncio.timeout(REFRESH_TIMEOUT_SECONDS):
                return await self._cap_operation(change)
        except PermissionDeniedError as error:
            message = (
                "The inverter denied write permission. Check local installer permissions; "
                "if required, configure SOLAR_INSTALLER_PASSWORD locally. No write was retried."
            )
            cause: Exception = error
        except (SolarReadError, SolarControlError) as error:
            message, cause = str(error), error
        except (HuaweiSolarException, TModbusError, OSError) as error:
            message = f"Generation-cap operation failed ({type(error).__name__})."
            cause = error
        except asyncio.CancelledError:
            if write_attempted:
                LOGGER.error("Cap request cancelled after transmission began; read the cap before any further change.")
            raise
        if write_attempted:
            message += (
                " The cap may have changed. Read get_generation_limit before retrying or restoring. "
                "The previous value and intent are saved in SOLAR_CONTROL_LOG; no automatic rollback was attempted."
            )
        raise SolarControlError(message) from cause

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
