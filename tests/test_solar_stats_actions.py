from __future__ import annotations

import asyncio
import hashlib
import io
import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile

import pytest

from solar_stats import actions, actions_runner, auth, server
from solar_stats.actions import ActionsConfig, GitHubActionsSource, result_payload
from solar_stats.browser import FusionSolarSource, StatsConfig
from solar_stats.hourly import StoredHourlySource
from solar_stats.models import DateRange, StatsError
from solar_stats.output import save_report
from solar_stats.stored import StoredGenerationSource
from test_solar_stats import PLANT_URL, synthetic_report
from test_solar_stats_hourly import make_hours, ZONE

REPO = "example/private-stats"
SHA = "a" * 40
WORKFLOW_SHA = "f" * 40
REQUEST = "b" * 32
PLANT = "NE=123456"


def archive_for(envelope, *, name="report.json"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, json.dumps(envelope))
    return stream.getvalue()


def envelope_for(kind="day"):
    report = synthetic_report(DateRange.parse("2025-01-01")) if kind == "day" else make_hours("2025-01-01")
    return {"request_id": REQUEST, "source_sha": SHA, "granularity": kind, "report": asdict(report)}


class FakeActions(GitHubActionsSource):
    def __init__(self, kind="day"):
        super().__init__(ActionsConfig(REPO, SHA), PLANT, ZONE)
        self.calls = []
        self.envelope = envelope_for(kind)
        self.private = True
        self.run = {
            "display_title": f"solar-stats:{REQUEST}", "head_sha": WORKFLOW_SHA,
            "workflow_id": 11, "event": "workflow_dispatch", "run_attempt": 1,
            "actor": {"login": "caller"}, "repository": {"full_name": REPO},
            "status": "completed", "conclusion": "success",
        }
        self.bad_digest = False

    async def _api(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        root = f"repos/{REPO}"
        if path == root:
            return {"private": self.private, "full_name": REPO}
        if path == "user":
            return {"login": "caller"}
        if path == f"{root}/actions/workflows/solar-stats.yml":
            return {"id": 11, "path": ".github/workflows/solar-stats.yml", "state": "active"}
        if path == f"{root}/git/ref/heads/main":
            return {"object": {"sha": WORKFLOW_SHA}}
        if path.endswith("/dispatches"):
            assert payload["inputs"]["request_id"] == REQUEST and payload["ref"] == "main"
            return {"workflow_run_id": 42}
        if path == f"{root}/actions/runs/42":
            return self.run
        if path.endswith("/artifacts?per_page=10"):
            archive = archive_for(self.envelope)
            return {"artifacts": [{"id": 43, "name": f"solar-stats-{REQUEST}", "expired": False,
                                   "size_in_bytes": len(archive),
                                   "digest": "bad" if self.bad_digest else f"sha256:{hashlib.sha256(archive).hexdigest()}"}]}
        raise AssertionError((method, path))

    async def _api_bytes(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path.endswith("/zip"):
            return archive_for(self.envelope)
        assert method == "DELETE" or path.endswith("/cancel")
        return b""


@pytest.fixture(autouse=True)
def request_id(monkeypatch):
    monkeypatch.setattr(actions, "uuid4", lambda: SimpleNamespace(hex=REQUEST))


@pytest.mark.parametrize("kind", ["day", "hour"])
def test_bound_remote_reports_and_private_artifact_cleanup(kind):
    source = FakeActions(kind)
    interval = DateRange.parse("2025-01-01")
    result = asyncio.run(source.fetch(interval) if kind == "day" else source.fetch_hourly(interval))
    assert result.plant_id == PLANT
    assert result.generation_kwh == (1.125 if kind == "day" else 30)
    assert any(method == "DELETE" and path.endswith("/artifacts/43") for method, path, _ in source.calls)
    assert sum(path == f"repos/{REPO}" for _, path, _ in source.calls) == 2


def test_public_repository_refused_before_dispatch():
    source = FakeActions()
    source.private = False
    with pytest.raises(StatsError, match="must be private"):
        asyncio.run(source.fetch(DateRange.parse("2025-01-01")))
    assert len(source.calls) == 1


@pytest.mark.parametrize("field,value", [
    ("head_sha", "0" * 40), ("display_title", "other request"), ("run_attempt", 2),
    ("event", "pull_request"), ("actor", {"login": "other"}), ("workflow_id", 99),
])
def test_foreign_or_rerun_operation_is_not_adopted_or_cancelled(field, value):
    source = FakeActions()
    source.run[field] = value
    with pytest.raises(StatsError, match="ownership_mismatch"):
        asyncio.run(source.fetch(DateRange.parse("2025-01-01")))
    assert not any(path.endswith("/zip") or path.endswith("/cancel") for _, path, _ in source.calls)


@pytest.mark.parametrize("field,value", [
    ("request_id", "c" * 32), ("source_sha", "c" * 40), ("granularity", "hour"),
])
def test_wrong_report_envelope_rejected(field, value):
    envelope = envelope_for()
    envelope[field] = value
    with pytest.raises(StatsError, match="actions_invalid_result"):
        result_payload(archive_for(envelope), REQUEST, SHA, "day")


def test_archive_path_tricks_and_bad_digest_are_rejected():
    with pytest.raises(StatsError, match="actions_invalid_result"):
        result_payload(archive_for(envelope_for(), name="../report.json"), REQUEST, SHA, "day")
    source = FakeActions()
    source.bad_digest = True
    with pytest.raises(StatsError, match="checksum"):
        asyncio.run(source.fetch(DateRange.parse("2025-01-01")))


def test_remote_authentication_failure_is_explicit_with_no_local_fallback():
    source = FakeActions()
    source.run["conclusion"] = "failure"
    source.envelope.pop("report")
    source.envelope["error"] = {"code": "invalid_credentials", "message": "Configure the two runner secrets."}
    with pytest.raises(StatsError, match="invalid_credentials"):
        asyncio.run(source.fetch(DateRange.parse("2025-01-01")))
    assert any(method == "DELETE" for method, _, _ in source.calls)
    assert sum(path.endswith("/dispatches") for _, path, _ in source.calls) == 1


@pytest.mark.parametrize("field,value", [("plant_id", "NE=other"), ("generation_kwh", 999), ("missing_dates", ["2025-01-01"])])
def test_wrong_daily_plant_totals_or_provenance_rejected(field, value):
    source = FakeActions()
    source.envelope["report"][field] = value
    with pytest.raises(StatsError, match="actions_invalid_result"):
        asyncio.run(source.fetch(DateRange.parse("2025-01-01")))


def test_timeout_cancels_only_our_owned_run(monkeypatch):
    source = FakeActions()
    source.timeout_seconds = 0.02
    source.run["status"] = "in_progress"
    monkeypatch.setattr(actions, "POLL_SECONDS", 10)
    with pytest.raises(StatsError, match="timed out"):
        asyncio.run(source.fetch(DateRange.parse("2025-01-01")))
    assert source.calls[-1][:2] == ("POST", f"repos/{REPO}/actions/runs/42/cancel")
    assert sum(path.endswith("/dispatches") for _, path, _ in source.calls) == 1


def test_timeout_does_not_cancel_a_later_rerun(monkeypatch):
    class ReplacedRun(FakeActions):
        reads = 0
        async def _api(self, method, path, payload=None):
            if path.endswith("/actions/runs/42"):
                self.reads += 1
                if self.reads > 1:
                    self.run["run_attempt"] = 2
            return await super()._api(method, path, payload)
    source = ReplacedRun()
    source.timeout_seconds = 0.02
    source.run["status"] = "in_progress"
    monkeypatch.setattr(actions, "POLL_SECONDS", 10)
    with pytest.raises(StatsError, match="timed out"):
        asyncio.run(source.fetch(DateRange.parse("2025-01-01")))
    assert not any(path.endswith("/cancel") for _, path, _ in source.calls)


def test_gh_errors_do_not_reveal_captured_output(monkeypatch):
    process = SimpleNamespace(
        returncode=1,
        communicate=AsyncMock(return_value=(b"private report", b"HTTP 403 confidential-token")),
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(actions.asyncio, "create_subprocess_exec", spawn)
    source = GitHubActionsSource(ActionsConfig(REPO, SHA), PLANT, ZONE)
    with pytest.raises(StatsError, match="HTTP 403") as error:
        asyncio.run(source._api("GET", f"repos/{REPO}"))
    assert "confidential-token" not in str(error.value) and "private report" not in str(error.value)
    assert "--hostname" in spawn.call_args.args and "github.com" in spawn.call_args.args


def test_mcp_cli_file_hit_does_not_call_gh_or_keychain(tmp_path, monkeypatch, capsys):
    report = synthetic_report(DateRange.parse("2025-01-01"))
    save_report(report, "csv", tmp_path / "generation-2025.csv")
    monkeypatch.setenv("SOLAR_STATS_BACKEND", "github_actions")
    monkeypatch.setenv("SOLAR_STATS_ACTIONS_REPOSITORY", REPO)
    monkeypatch.setenv("SOLAR_STATS_ACTIONS_SOURCE_SHA", SHA)
    monkeypatch.setenv("SOLAR_STATS_GH_COMMAND", "/missing/gh")
    monkeypatch.setenv("SOLAR_STATS_PLANT_URL", PLANT_URL)
    monkeypatch.setenv("SOLAR_STATS_PROFILE_DIR", str(tmp_path / "missing-browser"))
    monkeypatch.setenv("SOLAR_STATS_DATA_DIR", str(tmp_path))
    browser = AsyncMock(side_effect=AssertionError("No browser on an Actions file hit"))
    monkeypatch.setattr(server, "FusionSolarSource", browser)
    server.main(["query", "--start-date", "2025-01-01"])
    result = json.loads(capsys.readouterr().out)
    assert result["retrieval_mode"] == "stored_file" and result["cached_days"] == 1
    browser.assert_not_called()


def test_environment_credentials_work_on_linux_without_keychain(monkeypatch):
    monkeypatch.setattr(auth.sys, "platform", "linux")
    monkeypatch.setenv("SOLAR_STATS_USERNAME", "test-account")
    monkeypatch.setenv("SOLAR_STATS_PASSWORD", "  test-password  ")
    credentials = auth.environment_credentials()
    assert credentials.username == "test-account" and credentials.password == "  test-password  "
    assert "test-account" not in repr(credentials) and "test-password" not in repr(credentials)
    monkeypatch.delenv("SOLAR_STATS_PASSWORD")
    with pytest.raises(StatsError, match="invalid_credentials"):
        auth.environment_credentials()


def test_environment_login_uses_existing_origin_guards_and_one_submission(tmp_path, monkeypatch):
    async def check():
        config = StatsConfig(PLANT_URL, tmp_path, login_source="environment")
        source = FusionSolarSource(config)
        page = SimpleNamespace(url="https://example.fusionsolar.huawei.com/unisso/login.action")
        from unittest.mock import MagicMock
        page = MagicMock(url=page.url)
        page.locator.return_value.fill = AsyncMock()
        async def submit():
            page.url = PLANT_URL
        page.locator.return_value.click = AsyncMock(side_effect=submit)
        monkeypatch.setattr(source, "_portal_state", AsyncMock(side_effect=["login", "report"]))
        keychain = AsyncMock(side_effect=AssertionError("No Keychain in an Actions login"))
        monkeypatch.setattr("solar_stats.browser.keychain_credentials", keychain)
        monkeypatch.setenv("SOLAR_STATS_USERNAME", "test-account")
        monkeypatch.setenv("SOLAR_STATS_PASSWORD", "test-password")
        assert await source._ensure_report(page) is page
        keychain.assert_not_called()
        page.locator.return_value.click.assert_awaited_once()
        page.url = "https://other.example/unisso/login.action"
        monkeypatch.setattr(source, "_portal_state", AsyncMock(return_value="login"))
        with pytest.raises(StatsError, match="unexpected_login_origin"):
            await source._ensure_report(page)
    asyncio.run(check())


def test_runner_reports_missing_secrets_privately_without_browser(tmp_path, monkeypatch):
    for name, value in {
        "GITHUB_ACTIONS": "true", "SOLAR_STATS_REQUEST_ID": REQUEST, "SOLAR_STATS_SOURCE_SHA": SHA,
        "SOLAR_STATS_GRANULARITY": "day", "SOLAR_STATS_START_DATE": "2025-01-01",
        "SOLAR_STATS_END_DATE": "2025-01-01", "SOLAR_STATS_PLANT_URL": PLANT_URL,
        "SOLAR_STATS_PROFILE_DIR": str(tmp_path / "profile"), "SOLAR_STATS_BROWSER_MODE": "managed",
        "SOLAR_STATS_LOGIN_SOURCE": "environment", "SOLAR_STATS_BACKEND": "browser",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SOLAR_STATS_USERNAME", raising=False)
    monkeypatch.delenv("SOLAR_STATS_PASSWORD", raising=False)
    monkeypatch.delenv("SOLAR_STATS_KEYCHAIN_SERVICE", raising=False)
    monkeypatch.setattr(actions_runner.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=SHA))
    browser = AsyncMock(side_effect=AssertionError("Missing secrets must fail before a browser starts"))
    monkeypatch.setattr(actions_runner, "FusionSolarSource", browser)
    result = asyncio.run(actions_runner.run_request())
    assert result["request_id"] == REQUEST and result["source_sha"] == SHA
    assert result["error"]["code"] == "invalid_credentials" and "report" not in result
    browser.assert_not_called()


def test_backend_and_login_configuration_are_explicit(tmp_path, monkeypatch):
    with pytest.raises(StatsError, match="not both"):
        StatsConfig(PLANT_URL, tmp_path, keychain_service="test", login_source="environment")
    with pytest.raises(StatsError, match="Unsupported"):
        StatsConfig(PLANT_URL, tmp_path, backend="unknown")
    for repo, sha in [("public invalid/name", SHA), (REPO, "main")]:
        with pytest.raises(StatsError, match="invalid_config"):
            ActionsConfig(repo, sha)
    monkeypatch.setenv("SOLAR_STATS_BACKEND", "github_actions")
    monkeypatch.setenv("SOLAR_STATS_PLANT_URL", PLANT_URL)
    monkeypatch.delenv("SOLAR_STATS_PROFILE_DIR", raising=False)
    assert StatsConfig.from_environment().profile_dir is None
