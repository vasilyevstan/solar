from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeoutError

from solar_stats import auth
from solar_stats.auth import Credentials, keychain_credentials
from solar_stats.browser import FusionSolarSource, StatsConfig
from solar_stats.models import DateRange, StatsError

PLANT_URL = "https://uni001example.fusionsolar.huawei.com/cloud.html#/view/station/NE=123456/report"
LOGIN_URL = "https://example.fusionsolar.huawei.com/unisso/login.action"
SERVICE = "test.solar-stats.fusionsolar"


def test_keychain_reads_credentials_without_secret_arguments_or_repr(monkeypatch, capsys) -> None:
    async def check():
        credentials = Credentials("synthetic-account", "synthetic-password")
        attributes = MagicMock(returncode=0, communicate=AsyncMock(return_value=(
            b'    "acct"<blob>="synthetic-account"\n', None,
        )))
        password = MagicMock(returncode=0, communicate=AsyncMock(return_value=(
            b"synthetic-password\n", None,
        )))
        spawn = AsyncMock(side_effect=[attributes, password])
        monkeypatch.setattr(auth.sys, "platform", "darwin")
        monkeypatch.setattr(auth.asyncio, "create_subprocess_exec", spawn)
        result = await keychain_credentials(SERVICE)
        assert result == credentials
        assert "synthetic-account" not in repr(result) and "synthetic-password" not in repr(result)
        for call in spawn.call_args_list:
            assert "synthetic-account" not in call.args and "synthetic-password" not in call.args
            assert call.kwargs["stdout"] == asyncio.subprocess.PIPE
            assert call.kwargs["stderr"] == asyncio.subprocess.DEVNULL
        assert spawn.call_args_list[1].args[-1] == "-w"
        assert capsys.readouterr() == ("", "")
    asyncio.run(check())


def test_keychain_decodes_hex_account_and_preserves_password_spaces(monkeypatch) -> None:
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    output = AsyncMock(side_effect=['    "acct"<blob>=0x75736572  "user"\n', "  private-value  \n"])
    monkeypatch.setattr(auth, "_keychain_output", output)
    assert asyncio.run(keychain_credentials(SERVICE)) == Credentials("user", "  private-value  ")


@pytest.mark.parametrize("attributes", ['"acct"<blob>=""', '"acct"<blob>=not-json', '"svce"<blob>="only-service"'])
def test_bad_account_metadata_fails_before_reading_password(monkeypatch, attributes) -> None:
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    output = AsyncMock(return_value=attributes)
    monkeypatch.setattr(auth, "_keychain_output", output)
    with pytest.raises(StatsError, match="invalid_credentials"):
        asyncio.run(keychain_credentials(SERVICE))
    output.assert_awaited_once()


def test_non_mac_keychain_request_is_explicit_and_does_not_spawn(monkeypatch) -> None:
    monkeypatch.setattr(auth.sys, "platform", "linux")
    spawn = AsyncMock()
    monkeypatch.setattr(auth.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(StatsError, match="requires macOS"):
        asyncio.run(keychain_credentials(SERVICE))
    spawn.assert_not_called()


def test_keychain_denial_is_sanitized(monkeypatch) -> None:
    process = MagicMock(returncode=36, communicate=AsyncMock(return_value=(b"sensitive-output", None)))
    monkeypatch.setattr(auth.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(StatsError, match="keychain_unavailable") as error:
        asyncio.run(auth._keychain_output(SERVICE, password=True))
    assert "sensitive-output" not in str(error.value)


def test_keychain_timeout_is_actionable_and_not_retried(monkeypatch) -> None:
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    output = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(auth, "_keychain_output", output)
    with pytest.raises(StatsError, match="approve access locally"):
        asyncio.run(keychain_credentials(SERVICE))
    output.assert_awaited_once()


def test_keychain_cancellation_reaps_the_helper(monkeypatch) -> None:
    async def check():
        started = asyncio.Event()
        async def wait():
            started.set()
            await asyncio.sleep(10)
        process = MagicMock(returncode=None)
        monkeypatch.setattr(auth.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
        async def communicate():
            if not started.is_set():
                await wait()
            return b"", None
        process.communicate = AsyncMock(side_effect=communicate)
        pending = asyncio.create_task(auth._keychain_output(SERVICE, password=True))
        await started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        process.kill.assert_called_once()
        assert process.communicate.await_count == 2
    asyncio.run(check())


@pytest.mark.parametrize("url", [
    "http://example.fusionsolar.huawei.com/unisso/login.action",
    "https://example.fusionsolar.huawei.com.evil.example/unisso/login.action",
    "https://other.fusionsolar.huawei.com/unisso/login.action",
    "https://example.fusionsolar.huawei.com/another-login",
])
def test_login_origin_is_bound_to_configured_region(tmp_path, url) -> None:
    config = StatsConfig(PLANT_URL, tmp_path)
    assert config.is_login_url(LOGIN_URL)
    assert not config.is_login_url(url)


def test_login_redirect_is_not_an_authenticated_plant_tab(tmp_path) -> None:
    config = StatsConfig(PLANT_URL, tmp_path)
    login_redirect = LOGIN_URL + "#/view/station/NE=123456/report"
    assert config.is_plant_page(PLANT_URL)
    assert not config.is_plant_page(login_redirect)
    with pytest.raises(StatsError, match="authentication_required"):
        config.report_url(login_redirect)


def test_blank_restored_app_reopens_normal_login_once(tmp_path, monkeypatch) -> None:
    async def check():
        config = StatsConfig(PLANT_URL, tmp_path, keychain_service=SERVICE)
        assert config.is_login_url(config.login_url)
        source = FusionSolarSource(config)
        page = MagicMock(url=PLANT_URL)
        page.locator.return_value.inner_text = AsyncMock(return_value="")
        page.locator.return_value.fill = AsyncMock()
        async def navigate(url, **kwargs):
            page.url = url
        async def clicked():
            page.url = PLANT_URL
        page.goto = AsyncMock(side_effect=navigate)
        page.locator.return_value.click = AsyncMock(side_effect=clicked)
        monkeypatch.setattr(source, "_portal_state",
                            AsyncMock(side_effect=[BrowserTimeoutError("blank"), "login", "report"]))
        reader = AsyncMock(return_value=Credentials("synthetic-account", "synthetic-password"))
        monkeypatch.setattr("solar_stats.browser.keychain_credentials", reader)
        assert await source._ensure_report(page) is page
        page.goto.assert_awaited_once_with(config.login_url, wait_until="domcontentloaded")
        reader.assert_awaited_once_with(SERVICE)
        page.locator.return_value.click.assert_awaited_once()
    asyncio.run(check())


def test_nonblank_portal_error_is_not_replaced_with_another_login(tmp_path, monkeypatch) -> None:
    source = FusionSolarSource(StatsConfig(PLANT_URL, tmp_path, keychain_service=SERVICE))
    page = MagicMock(url=PLANT_URL)
    page.locator.return_value.inner_text = AsyncMock(return_value="Maintenance")
    monkeypatch.setattr(source, "_portal_state", AsyncMock(side_effect=BrowserTimeoutError("not ready")))
    reader = AsyncMock()
    monkeypatch.setattr("solar_stats.browser.keychain_credentials", reader)
    with pytest.raises(BrowserTimeoutError):
        asyncio.run(source._ensure_report(page))
    page.goto.assert_not_called()
    reader.assert_not_called()


def test_authenticated_home_landing_opens_configured_report_without_keychain(tmp_path, monkeypatch) -> None:
    async def check():
        config = StatsConfig(PLANT_URL, tmp_path, keychain_service=SERVICE)
        source = FusionSolarSource(config)
        page = MagicMock(url=PLANT_URL.replace("/view/station/NE=123456/report", "/home/list"))
        async def navigate(url, **kwargs):
            page.url = url
        page.goto = AsyncMock(side_effect=navigate)
        monkeypatch.setattr(source, "_portal_state", AsyncMock(side_effect=["application", "report"]))
        credentials = AsyncMock()
        monkeypatch.setattr("solar_stats.browser.keychain_credentials", credentials)
        assert await source._ensure_report(page) is page
        page.goto.assert_awaited_once_with(PLANT_URL, wait_until="domcontentloaded")
        credentials.assert_not_called()
        assert not config.is_application_page("https://other.example/uniportal/portal")
        with pytest.raises(StatsError, match="unexpected_login_origin"):
            config.application_report_url("https://other.example/uniportal/portal")
    asyncio.run(check())


def test_report_matches_the_action_request_not_an_older_response(tmp_path) -> None:
    async def check():
        source = FusionSolarSource(StatsConfig(PLANT_URL, tmp_path))
        payload = {"success": True, "data": {"total": 0, "pageNo": 1, "pageSize": 100, "list": []}}
        response = MagicMock(status=200, json=AsyncMock(return_value=payload), finished=AsyncMock(return_value=None))
        request = MagicMock(url="https://example.fusionsolar.huawei.com/rest/pvms/web/report/v1/station/station-kpi-list",
                            response=AsyncMock(return_value=response))
        pending = asyncio.get_running_loop().create_future()
        pending.set_result(request)
        page = MagicMock(url=PLANT_URL)
        page.expect_request.return_value.__aenter__.return_value.value = pending
        action = AsyncMock()
        result = await source._request(page, action)
        assert result.total == 0
        assert page.expect_request.call_args.args[0](request)
        page.expect_response.assert_not_called()
        request.response.assert_awaited_once()
        response.finished.assert_not_called()
        action.assert_awaited_once()
    asyncio.run(check())


@pytest.mark.parametrize("state", ["report", "challenge"])
def test_existing_session_or_challenge_never_reads_keychain(tmp_path, monkeypatch, state) -> None:
    source = FusionSolarSource(StatsConfig(PLANT_URL, tmp_path, keychain_service=SERVICE))
    page = MagicMock(url=PLANT_URL)
    monkeypatch.setattr(source, "_portal_state", AsyncMock(return_value=state))
    credentials = AsyncMock()
    monkeypatch.setattr("solar_stats.browser.keychain_credentials", credentials)
    if state == "report":
        asyncio.run(source._ensure_report(page))
    else:
        with pytest.raises(StatsError, match="MFA/CAPTCHA"):
            asyncio.run(source._ensure_report(page))
    credentials.assert_not_called()


def test_foreign_login_page_never_reads_credentials(tmp_path, monkeypatch) -> None:
    source = FusionSolarSource(StatsConfig(PLANT_URL, tmp_path, keychain_service=SERVICE))
    page = MagicMock(url="https://phishing.example/unisso/login.action")
    monkeypatch.setattr(source, "_portal_state", AsyncMock(return_value="login"))
    credentials = AsyncMock()
    monkeypatch.setattr("solar_stats.browser.keychain_credentials", credentials)
    with pytest.raises(StatsError, match="unexpected_login_origin"):
        asyncio.run(source._ensure_report(page))
    credentials.assert_not_called()
    page.locator.assert_not_called()


def test_redirect_during_keychain_read_prevents_credential_entry(tmp_path, monkeypatch) -> None:
    source = FusionSolarSource(StatsConfig(PLANT_URL, tmp_path, keychain_service=SERVICE))
    page = MagicMock(url=LOGIN_URL)
    monkeypatch.setattr(source, "_portal_state", AsyncMock(return_value="login"))
    async def redirected(_):
        page.url = "https://other.example/unisso/login.action"
        return Credentials("synthetic-account", "synthetic-password")
    monkeypatch.setattr("solar_stats.browser.keychain_credentials", redirected)
    with pytest.raises(StatsError, match="unexpected_login_origin"):
        asyncio.run(source._ensure_report(page))
    page.locator.assert_not_called()


def test_denied_keychain_does_not_submit_login(tmp_path, monkeypatch) -> None:
    source = FusionSolarSource(StatsConfig(PLANT_URL, tmp_path, keychain_service=SERVICE))
    page = MagicMock(url=LOGIN_URL)
    monkeypatch.setattr(source, "_portal_state", AsyncMock(return_value="login"))
    reader = AsyncMock(side_effect=StatsError("keychain_unavailable", "Access was not granted."))
    monkeypatch.setattr("solar_stats.browser.keychain_credentials", reader)
    with pytest.raises(StatsError, match="keychain_unavailable"):
        asyncio.run(source._ensure_report(page))
    page.locator.assert_not_called()


@pytest.mark.parametrize("outcome", ["report", "rejected", "challenge", "browser_error"])
def test_single_login_attempt_and_safe_errors(tmp_path, monkeypatch, outcome) -> None:
    async def check():
        source = FusionSolarSource(StatsConfig(PLANT_URL, tmp_path, keychain_service=SERVICE))
        page = MagicMock(url=LOGIN_URL)
        page.locator.return_value.fill = AsyncMock()
        async def clicked():
            if outcome == "report":
                page.url = PLANT_URL
        page.locator.return_value.click = AsyncMock(side_effect=clicked)
        states = ["login", BrowserError("synthetic-password") if outcome == "browser_error" else outcome]
        monkeypatch.setattr(source, "_portal_state", AsyncMock(side_effect=states))
        credentials = AsyncMock(return_value=Credentials("synthetic-account", "synthetic-password"))
        monkeypatch.setattr("solar_stats.browser.keychain_credentials", credentials)
        if outcome == "report":
            await source._ensure_report(page)
        else:
            with pytest.raises(StatsError, match="no login retry") as error:
                await source._ensure_report(page)
            assert "synthetic-password" not in str(error.value)
            assert error.value.__cause__ is None
        credentials.assert_awaited_once_with(SERVICE)
        assert page.locator.return_value.fill.await_count == 2
        page.locator.return_value.click.assert_awaited_once()
    asyncio.run(check())


@pytest.mark.parametrize("state", ["report", "challenge"])
def test_login_follows_only_its_own_popup_and_cleans_up_failures(tmp_path, monkeypatch, state) -> None:
    async def check():
        source = FusionSolarSource(StatsConfig(PLANT_URL, tmp_path, keychain_service=SERVICE))
        page = MagicMock(url=LOGIN_URL)
        child = MagicMock(url=PLANT_URL, wait_for_load_state=AsyncMock(), close=AsyncMock())
        page.locator.return_value.fill = AsyncMock()
        async def clicked():
            event, callback = page.on.call_args.args
            assert event == "popup"
            callback(child)
        page.locator.return_value.click = AsyncMock(side_effect=clicked)
        async def portal_state(candidate, *, after_login=False):
            if candidate is child:
                return state
            if after_login:
                await asyncio.sleep(30)
            return "login"
        monkeypatch.setattr(source, "_portal_state", portal_state)
        monkeypatch.setattr("solar_stats.browser.keychain_credentials",
                            AsyncMock(return_value=Credentials("synthetic-account", "synthetic-password")))
        if state == "report":
            assert await source._ensure_report(page) is child
            child.close.assert_not_called()
        else:
            with pytest.raises(StatsError, match="interactive challenge"):
                await source._ensure_report(page)
            child.close.assert_awaited_once()
        page.locator.return_value.click.assert_awaited_once()
        page.remove_listener.assert_called_once()
    asyncio.run(check())


@pytest.mark.parametrize("fails", [False, True])
def test_managed_browser_headless_start_and_cleanup(tmp_path, fails) -> None:
    async def check():
        source = FusionSolarSource(StatsConfig(PLANT_URL, tmp_path, browser_mode="managed", time_zone="Europe/Helsinki"))
        context = MagicMock(close=AsyncMock())
        driver = MagicMock()
        driver.chromium.launch_persistent_context = AsyncMock(return_value=context)
        if fails:
            with pytest.raises(StatsError):
                async with source._browser_context(driver) as actual:
                    assert actual is context
                    raise StatsError("test_failure", "Owned browser must still close.")
        else:
            async with source._browser_context(driver) as actual:
                assert actual is context
        options = driver.chromium.launch_persistent_context.call_args.kwargs
        assert options["headless"] is True and options["channel"] == "chrome"
        assert options["timezone_id"] == "Europe/Helsinki"
        assert not any("remote-debugging-port" in arg for arg in options["args"])
        context.close.assert_awaited_once()
    asyncio.run(check())


def test_owned_cleanup_error_does_not_return_success(tmp_path) -> None:
    async def check():
        source = FusionSolarSource(StatsConfig(PLANT_URL, tmp_path, browser_mode="managed"))
        context = MagicMock(close=AsyncMock(side_effect=BrowserError("internal-browser-details")))
        driver = MagicMock()
        driver.chromium.launch_persistent_context = AsyncMock(return_value=context)
        with pytest.raises(StatsError, match="browser_cleanup_failed"):
            async with source._browser_context(driver):
                pass
    asyncio.run(check())


def test_managed_queries_reuse_the_owned_tab_without_accumulating_popups(tmp_path, monkeypatch) -> None:
    async def check():
        source = FusionSolarSource(StatsConfig(PLANT_URL, tmp_path, browser_mode="managed"))
        page = MagicMock(url=PLANT_URL, goto=AsyncMock())
        context = MagicMock(pages=[page], close=AsyncMock())
        driver = MagicMock()
        driver.chromium.launch_persistent_context = AsyncMock(return_value=context)
        manager = MagicMock()
        manager.__aenter__.return_value = driver
        monkeypatch.setattr("solar_stats.browser.async_playwright", lambda: manager)
        monkeypatch.setattr(source, "_ensure_report", AsyncMock(return_value=page))
        monkeypatch.setattr(source, "_granularity", AsyncMock(side_effect=StatsError("ready", "Owned tab ready.")))
        with pytest.raises(StatsError, match="ready"):
            await source._fetch(DateRange.parse("2023-01-01"))
        page.goto.assert_awaited_once_with(PLANT_URL, wait_until="domcontentloaded")
        page.expect_popup.assert_not_called()
        context.new_page.assert_not_called()
        context.close.assert_awaited_once()
    asyncio.run(check())


def test_auth_config_is_opt_in_and_debug_output_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLAR_STATS_PLANT_URL", PLANT_URL)
    monkeypatch.setenv("SOLAR_STATS_PROFILE_DIR", str(tmp_path))
    monkeypatch.delenv("SOLAR_STATS_BROWSER_MODE", raising=False)
    monkeypatch.delenv("SOLAR_STATS_KEYCHAIN_SERVICE", raising=False)
    assert StatsConfig.from_environment().browser_mode == "attach"
    assert StatsConfig.from_environment().keychain_service is None
    monkeypatch.setenv("SOLAR_STATS_BROWSER_MODE", "managed")
    monkeypatch.setenv("SOLAR_STATS_KEYCHAIN_SERVICE", SERVICE)
    config = StatsConfig.from_environment()
    assert config.headless and config.keychain_service == SERVICE
    monkeypatch.setenv("DEBUG", "pw:api")
    source = FusionSolarSource(config)
    fetch = AsyncMock()
    monkeypatch.setattr(source, "_fetch", fetch)
    with pytest.raises(StatsError, match="unsafe_debug_config"):
        asyncio.run(source.fetch(DateRange.parse("2023-01-01")))
    fetch.assert_not_called()


def test_managed_profile_rejects_group_world_permissions(tmp_path) -> None:
    tmp_path.chmod(0o755)
    source = FusionSolarSource(StatsConfig(PLANT_URL, tmp_path, browser_mode="managed"))
    with pytest.raises(StatsError, match="0700"):
        asyncio.run(source.fetch(DateRange.parse("2023-01-01")))
