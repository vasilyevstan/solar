from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from dataclasses import asdict
from pathlib import Path

from .auth import environment_credentials
from .browser import FusionSolarSource, StatsConfig
from .models import DateRange, StatsError
from .output import save_contents_unlocked


async def run_request() -> dict[str, object]:
    request_id = os.environ.get("SOLAR_STATS_REQUEST_ID", "")
    source_sha = os.environ.get("SOLAR_STATS_SOURCE_SHA", "")
    granularity = os.environ.get("SOLAR_STATS_GRANULARITY", "")
    if (
        not re.fullmatch(r"[0-9a-f]{32}", request_id)
        or not re.fullmatch(r"[0-9a-f]{40}", source_sha) or granularity not in {"day", "hour"}
        or os.environ.get("GITHUB_ACTIONS") != "true"
    ):
        raise StatsError("invalid_request", "This entry point requires a bound GitHub Actions request.")
    envelope: dict[str, object] = {"request_id": request_id, "source_sha": source_sha, "granularity": granularity}
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10)
        if revision.stdout.strip() != source_sha:
            raise StatsError("source_revision_mismatch", "The runner is not executing the pinned source revision.")
        interval = DateRange.parse(os.environ.get("SOLAR_STATS_START_DATE", ""), os.environ.get("SOLAR_STATS_END_DATE"))
        config = StatsConfig.from_environment()
        if config.backend != "browser" or config.login_source != "environment" or config.browser_mode != "managed":
            raise StatsError("invalid_config", "The Actions runner requires managed browsing and environment credentials.")
        environment_credentials()
        source = FusionSolarSource(config)
        report = await source.fetch(interval) if granularity == "day" else await source.fetch_hourly(interval)
        envelope["report"] = asdict(report)
    except StatsError as error:
        message = str(error).removeprefix(f"{error.code}: ")
        for key in ("SOLAR_STATS_USERNAME", "SOLAR_STATS_PASSWORD"):
            value = os.environ.get(key)
            if value:
                message = message.replace(value, "[redacted]")
        envelope["error"] = {"code": error.code, "message": message}
    return envelope


def main() -> None:
    result = asyncio.run(run_request())
    directory = Path(os.environ["RUNNER_TEMP"]) / "solar-stats-result"
    save_contents_unlocked(json.dumps(result) + "\n", None, directory / "report.json")
    if "error" in result:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
