from dataclasses import dataclass
from datetime import datetime
from typing import Literal


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
    percent: float
    active_percent: float
    control_enabled: bool


@dataclass(frozen=True)
class GenerationLimitChange:
    previous_percent: float
    limit: GenerationLimit
    changed: bool
    request_id: str | None
    active_readback_matches: bool
    warning: str | None
    configuration_verified: Literal[True] = True
