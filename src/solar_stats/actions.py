from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from .hourly import HourlyReport, make_hourly_report
from .models import DateRange, GenerationReport, StatsError, report_from_days

LOGGER = logging.getLogger(__name__)
WORKFLOW = "solar-stats.yml"
MAX_RESULT_BYTES = 32 * 1024 * 1024
POLL_SECONDS = 5


@dataclass(frozen=True)
class ActionsConfig:
    repository: str
    source_sha: str
    gh_command: str = "gh"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", self.repository):
            raise StatsError("invalid_config", "SOLAR_STATS_ACTIONS_REPOSITORY must be owner/private-repository.")
        if not re.fullmatch(r"[0-9a-f]{40}", self.source_sha):
            raise StatsError("invalid_config", "SOLAR_STATS_ACTIONS_SOURCE_SHA must be the pinned source commit.")
        if not self.gh_command:
            raise StatsError("invalid_config", "SOLAR_STATS_GH_COMMAND must name the GitHub CLI executable.")

    @classmethod
    def from_environment(cls) -> ActionsConfig:
        return cls(
            os.environ.get("SOLAR_STATS_ACTIONS_REPOSITORY", ""),
            os.environ.get("SOLAR_STATS_ACTIONS_SOURCE_SHA", ""),
            os.environ.get("SOLAR_STATS_GH_COMMAND", "gh"),
        )


def object_result(value: object) -> dict:
    if not isinstance(value, dict):
        raise StatsError("actions_protocol_error", "GitHub returned an unexpected response shape.")
    return value


def result_payload(archive: bytes, request_id: str, source_sha: str, granularity: str) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            members = zipped.infolist()
            if len(members) != 1 or members[0].filename != "report.json" or members[0].file_size > MAX_RESULT_BYTES:
                raise ValueError
            result = json.loads(zipped.read(members[0]))
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError):
        raise StatsError("actions_invalid_result", "The private report artifact is not a valid bounded report.") from None
    result = object_result(result)
    if (result.get("request_id"), result.get("source_sha"), result.get("granularity")) != (request_id, source_sha, granularity):
        raise StatsError("actions_invalid_result", "The report does not belong to this request and source revision.")
    if "error" in result:
        error = object_result(result["error"])
        code, message = error.get("code"), error.get("message")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z_]+", code) or not isinstance(message, str):
            raise StatsError("actions_invalid_result", "The runner returned an invalid error.")
        raise StatsError(code, message)
    return object_result(result.get("report"))


class GitHubActionsSource:
    def __init__(
        self, config: ActionsConfig, plant_id: str, time_zone: str | None, timeout_seconds: float = 600
    ) -> None:
        self.config, self.plant_id, self.time_zone = config, plant_id, time_zone
        self.timeout_seconds = timeout_seconds
        self._lock = asyncio.Lock()

    async def _api_bytes(self, method: str, path: str, payload: dict | None = None) -> bytes:
        arguments = [
            self.config.gh_command, "api", "--hostname", "github.com", "--method", method,
            "-H", "X-GitHub-Api-Version: 2026-03-10", path,
        ]
        if payload is not None:
            arguments.extend(["--input", "-"])
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments, stdin=asyncio.subprocess.PIPE if payload is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            raise StatsError("github_unavailable", "Install/authenticate gh or configure SOLAR_STATS_GH_COMMAND.") from None
        try:
            async with asyncio.timeout(60):
                output, error = await process.communicate(json.dumps(payload).encode() if payload is not None else None)
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        if process.returncode:
            status = re.search(rb"HTTP (\d{3})", error)
            detail = f"HTTP {status[1].decode()}" if status else f"exit {process.returncode}"
            raise StatsError("github_api_failed", f"GitHub {method} failed ({detail}); check gh access to the private runner.")
        return output

    async def _api(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = await self._api_bytes(method, path, payload)
        try:
            return object_result(json.loads(data))
        except ValueError:
            raise StatsError("actions_protocol_error", "GitHub did not return the expected JSON response.") from None

    async def _private_repository(self) -> None:
        repo = await self._api("GET", f"repos/{self.config.repository}")
        if repo.get("private") is not True or repo.get("full_name") != self.config.repository:
            raise StatsError("unsafe_actions_repository", "The Actions runner repository must be private.")

    async def fetch(self, interval: DateRange) -> GenerationReport:
        payload = await self._request(interval, "day")
        try:
            report = TypeAdapter(GenerationReport).validate_json(json.dumps(payload), strict=True)
        except ValidationError:
            raise StatsError("actions_invalid_result", "The runner returned an invalid daily report.") from None
        self._check_interval(report, interval)
        readings = {date.fromisoformat(day.date): day for day in report.daily}
        normalized = report_from_days(interval, self.plant_id, readings)
        if (report.generation_kwh, report.missing_dates, report.source_complete) != (
            normalized.generation_kwh, normalized.missing_dates, normalized.source_complete
        ):
            raise StatsError("actions_invalid_result", "Daily report totals or provenance are inconsistent.")
        return report

    async def fetch_hourly(self, interval: DateRange) -> HourlyReport:
        payload = await self._request(interval, "hour")
        try:
            report = TypeAdapter(HourlyReport).validate_json(json.dumps(payload), strict=True)
        except ValidationError:
            raise StatsError("actions_invalid_result", "The runner returned an invalid hourly report.") from None
        self._check_interval(report, interval)
        if report.time_zone != self.time_zone:
            raise StatsError("actions_invalid_result", "The runner used a different plant time zone.")
        normalized = make_hourly_report(
            interval, self.plant_id, report.time_zone, {date.fromisoformat(day.date): day for day in report.daily}
        )
        if (report.generation_kwh, report.missing_hours, report.source_complete, report.unavailable_dates) != (
            normalized.generation_kwh, normalized.missing_hours, normalized.source_complete, []
        ):
            raise StatsError("actions_invalid_result", "Hourly report totals or provenance are inconsistent.")
        return report

    def _check_interval(self, report: GenerationReport | HourlyReport, interval: DateRange) -> None:
        if (
            report.plant_id != self.plant_id
            or (report.start_date, report.end_date) != (interval.start.isoformat(), interval.end.isoformat())
            or [day.date for day in report.daily] != [day.isoformat() for day in interval.dates()]
        ):
            raise StatsError("actions_invalid_result", "The report does not match the requested plant and dates.")

    def _owns_run(self, run: dict, request_id: str, actor: str, workflow_id: int, workflow_sha: str) -> bool:
        author, repository = run.get("actor"), run.get("repository")
        return (
            run.get("display_title") == f"solar-stats:{request_id}"
            and run.get("head_sha") == workflow_sha and run.get("workflow_id") == workflow_id
            and run.get("event") == "workflow_dispatch" and run.get("run_attempt") == 1
            and isinstance(author, dict) and author.get("login") == actor
            and isinstance(repository, dict) and repository.get("full_name") == self.config.repository
        )

    async def _request(self, interval: DateRange, granularity: Literal["day", "hour"]) -> dict:
        request_id = uuid4().hex
        run_id: int | None = None
        owned = completed = False
        root = f"repos/{self.config.repository}"
        try:
            async with asyncio.timeout(self.timeout_seconds), self._lock:
                await self._private_repository()
                actor = (await self._api("GET", "user")).get("login")
                workflow = await self._api("GET", f"{root}/actions/workflows/{WORKFLOW}")
                if (
                    not isinstance(actor, str) or not actor or type(workflow.get("id")) is not int
                    or workflow.get("path") != f".github/workflows/{WORKFLOW}" or workflow.get("state") != "active"
                ):
                    raise StatsError("actions_not_configured", "The private report workflow is unavailable or disabled.")
                ref = await self._api("GET", f"{root}/git/ref/heads/main")
                workflow_sha = object_result(ref.get("object")).get("sha")
                if not isinstance(workflow_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", workflow_sha):
                    raise StatsError("actions_protocol_error", "GitHub did not identify the runner workflow revision.")
                dispatch = await self._api("POST", f"{root}/actions/workflows/{WORKFLOW}/dispatches", {
                    "ref": "main", "inputs": {
                        "request_id": request_id, "start_date": interval.start.isoformat(),
                        "end_date": interval.end.isoformat(), "granularity": granularity,
                    },
                })
                identifier = dispatch.get("workflow_run_id")
                if type(identifier) is not int or identifier <= 0:
                    raise StatsError("actions_protocol_error", "GitHub did not identify the dispatched run; do not blindly redispatch.")
                run_id = identifier
                while True:
                    run = await self._api("GET", f"{root}/actions/runs/{run_id}")
                    owned = self._owns_run(run, request_id, actor, workflow["id"], workflow_sha)
                    if not owned:
                        raise StatsError("actions_ownership_mismatch", "The workflow run does not match this caller/request/revision.")
                    if run.get("status") == "completed":
                        completed = True
                        break
                    await asyncio.sleep(POLL_SECONDS)
                await self._private_repository()
                listing = await self._api("GET", f"{root}/actions/runs/{run_id}/artifacts?per_page=10")
                artifacts = listing.get("artifacts")
                if not isinstance(artifacts, list):
                    raise StatsError("actions_protocol_error", "The run has no valid artifact listing.")
                matches = [item for item in artifacts if isinstance(item, dict) and item.get("name") == f"solar-stats-{request_id}"]
                if len(matches) != 1 or matches[0].get("expired") is not False:
                    raise StatsError("actions_run_failed", f"The runner produced no report ({run.get('conclusion')}); inspect private run {run_id}.")
                artifact = matches[0]
                if type(artifact.get("id")) is not int or type(artifact.get("size_in_bytes")) is not int or not 0 < artifact["size_in_bytes"] <= MAX_RESULT_BYTES:
                    raise StatsError("actions_invalid_result", "The report artifact exceeds the allowed size.")
                archive = await self._api_bytes("GET", f"{root}/actions/artifacts/{artifact['id']}/zip")
                if len(archive) > MAX_RESULT_BYTES or artifact.get("digest") != f"sha256:{hashlib.sha256(archive).hexdigest()}":
                    raise StatsError("actions_invalid_result", "The artifact checksum does not match GitHub's digest.")
                try:
                    result = result_payload(archive, request_id, self.config.source_sha, granularity)
                finally:
                    try:
                        await self._api_bytes("DELETE", f"{root}/actions/artifacts/{artifact['id']}")
                    except StatsError:
                        LOGGER.warning("Could not remove a consumed private artifact; the workflow retention limit still applies.")
                if run.get("conclusion") != "success":
                    raise StatsError("actions_run_failed", "The workflow failed despite returning report content.")
                return result
        except TimeoutError:
            raise StatsError("timeout", "The GitHub-backed query timed out; no cached or partial result was substituted.") from None
        finally:
            if run_id is not None and owned and not completed:
                try:
                    async with asyncio.timeout(10):
                        current = await self._api("GET", f"{root}/actions/runs/{run_id}")
                        if self._owns_run(current, request_id, actor, workflow["id"], workflow_sha):
                            if current.get("status") != "completed":
                                await self._api_bytes("POST", f"{root}/actions/runs/{run_id}/cancel")
                        else:
                            LOGGER.warning("Run ownership changed; no cancellation was attempted.")
                except (StatsError, TimeoutError):
                    LOGGER.warning("Could not cancel the owned timed-out run; its workflow deadline still applies.")
