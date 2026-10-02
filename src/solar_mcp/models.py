from dataclasses import dataclass
from datetime import datetime
from typing import Literal

PowerLimitMode = Literal["percent", "watts"]


@dataclass(frozen=True)
class SolarSnapshot:
    observed_at: datetime
    age_seconds: float
    from_cache: bool
    model: str
    software_version: str
    device_status_code: int
    device_status: str
    generation_w: float
    daily_yield_kwh: float
    lifetime_yield_kwh: float
    source: Literal["huawei_modbus"] = "huawei_modbus"


@dataclass(frozen=True)
class GenerationLimit:
    observed_at: datetime
    percent: float | None
    active_percent: float | None
    control_enabled: bool
    mode: PowerLimitMode = "percent"
    watts: int | None = None
    active_watts: int | None = None


@dataclass(frozen=True)
class GenerationLimitChange:
    previous_percent: float
    limit: GenerationLimit
    changed: bool
    request_id: str | None
    active_readback_matches: bool
    warning: str | None
    configuration_verified: Literal[True] = True


@dataclass(frozen=True)
class PowerLimitChange:
    previous: GenerationLimit
    requested_mode: PowerLimitMode
    requested_value: float
    limit: GenerationLimit
    write_performed: bool
    request_id: str | None
    active_readback_matches: bool
    warning: str | None
    configuration_verified: Literal[True] = True
