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
