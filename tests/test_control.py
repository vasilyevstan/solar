from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from huawei_solar import register_names as rn
from huawei_solar.modbus_pdu import PermissionDeniedError
from huawei_solar.registers import REGISTERS

from solar_mcp import reader as reader_module
from solar_mcp.models import GenerationLimitChange
from solar_mcp.reader import HuaweiConfig, HuaweiSolarReader, SolarControlError, _percent_tenths
from test_reader import FakeClient


class CapClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.values.update(
            {
                rn.ACTIVE_POWER_PERCENTAGE_DERATING: 100.0,
                rn.ACTIVE_POWER_ADJUSTMENT_MODE: 0,
                rn.ACTIVE_POWER_ADJUSTMENT_VALUE: 1000,
                rn.ACTIVE_POWER_ADJUSTMENT_COMMAND: 40125,
            }
        )
        self.writes: list[tuple[int, int]] = []
        self.login_count = 0
        self.heartbeat_count = 0
        self.login_succeeds = True
        self.heartbeat_succeeds = True
        self.write_error: Exception | None = None
        self.apply_write = True
        self.update_active = True
        self.write_delay = 0.0
        self.write_started = asyncio.Event()
        self.log_path: Path | None = None

    async def set(self, name: rn.RegisterName, percent: float) -> bool:
        assert name == rn.ACTIVE_POWER_PERCENTAGE_DERATING
        address, value = 40125, _percent_tenths(percent)
        if self.log_path is not None:
            record = json.loads(self.log_path.read_text().splitlines()[-1])
            assert record["event"] == "prepared"
            assert record["requested_percent"] == value / 10
        self.writes.append((address, value))
        self.write_started.set()
        if self.write_error is not None:
            raise self.write_error
        if self.apply_write:
            self.values[rn.ACTIVE_POWER_PERCENTAGE_DERATING] = value / 10
            if self.update_active:
                self.values[rn.ACTIVE_POWER_ADJUSTMENT_VALUE] = value
        await asyncio.sleep(self.write_delay)
        return True

    async def login(self, username: str, password: str) -> bool:
        assert username == "installer"
        assert password == "TEST-PASSWORD"
        self.login_count += 1
        return self.login_succeeds

    async def heartbeat(self) -> bool:
        self.heartbeat_count += 1
        return self.heartbeat_succeeds


def make_controller(
    client: CapClient, tmp_path: Path, *, enabled: bool = True, password: str | None = None
) -> HuaweiSolarReader:
    client.log_path = tmp_path / "generation-caps.jsonl"
    return HuaweiSolarReader(
        HuaweiConfig(
            "localhost",
            expected_serial="TEST-INVERTER-0001",
            allow_control=enabled,
            control_log=client.log_path,
            installer_password=password,
        ),
        client_factory=lambda _: client,
    )


def test_percentage_precision_for_every_supported_step() -> None:
    for tenths in range(1001):
        assert _percent_tenths(tenths / 10) == tenths
        assert REGISTERS[rn.ACTIVE_POWER_PERCENTAGE_DERATING].encode(tenths / 10) == (tenths,)


@pytest.mark.parametrize("value", [-0.1, 100.1, 3.14, True, "50", None, float("nan"), float("inf")])
def test_invalid_target_or_expectation_performs_no_io(tmp_path: Path, value: object) -> None:
    async def check() -> None:
        client = CapClient()
        reader = make_controller(client, tmp_path)
        with pytest.raises(SolarControlError, match="steps of 0.1"):
            await reader.set_generation_limit(value, 100)
        with pytest.raises(SolarControlError, match="steps of 0.1"):
            await reader.set_generation_limit(50, value)
        if value is not None:
            with pytest.raises(SolarControlError, match="steps of 0.1"):
                await reader.set_generation_limit(50, 100, value)
        assert client.connects == 0
        assert not client.writes
        assert not client.log_path.exists()
        await reader.close()

    asyncio.run(check())


def test_control_disabled_but_cap_reads_work_without_login(tmp_path: Path) -> None:
    async def check() -> None:
        client = CapClient()
        reader = make_controller(client, tmp_path, enabled=False, password="TEST-PASSWORD")
        with pytest.raises(SolarControlError, match="disabled"):
            await reader.set_generation_limit(50, 100)
        result = await reader.get_generation_limit()
        assert result.percent == result.active_percent == 100
        assert not result.control_enabled
        assert client.login_count == client.heartbeat_count == 0
        assert not client.writes
        assert not client.log_path.exists()
        await reader.close()

    asyncio.run(check())


def test_control_requires_expected_identity_and_absolute_log(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SOLAR_EXPECTED_SERIAL"):
        HuaweiConfig("localhost", allow_control=True)
    with pytest.raises(ValueError, match="absolute"):
        HuaweiConfig(
            "localhost", allow_control=True, expected_serial="TEST", control_log=Path("relative.jsonl")
        )
    with pytest.raises(ValueError, match="must not be empty"):
        HuaweiConfig("localhost", installer_password="")
    config = HuaweiConfig("localhost", installer_password="TEST-PASSWORD")
    assert "TEST-PASSWORD" not in repr(config)


@pytest.mark.parametrize("percent", [0.0, 0.1, 50.0, 57.9, 99.9])
def test_verified_change_journal_cache_invalidation_and_guarded_restoration(
    tmp_path: Path, percent: float
) -> None:
    async def check() -> None:
        client = CapClient()
        reader = make_controller(client, tmp_path)
        await reader.read()
        result = await reader.set_generation_limit(percent, 100)
        assert result.changed
        assert result.previous_percent == 100
        assert result.limit.percent == result.limit.active_percent == percent
        assert result.request_id is not None
        assert result.configuration_verified and result.active_readback_matches
        assert result.warning is None
        assert client.writes == [(40125, _percent_tenths(percent))]
        assert client.login_count == client.heartbeat_count == 0
        assert not (await reader.read()).from_cache
        records = [json.loads(line) for line in client.log_path.read_text().splitlines()]
        assert [record["event"] for record in records] == ["prepared", "verified"]
        assert all(record["previous_percent"] == 100 for record in records)
        assert all(record["request_id"] == result.request_id for record in records)
        assert client.log_path.stat().st_mode & 0o777 == 0o600
        await reader.close()
        # Restoring is explicit and uses a fresh expectation, even after a process restart.
        restored_reader = make_controller(client, tmp_path)
        restored = await restored_reader.set_generation_limit(result.previous_percent, percent)
        assert restored.changed and restored.limit.percent == 100
        assert client.writes[-1] == (40125, 1000)
        await restored_reader.close()

    asyncio.run(check())


def test_noop_does_not_test_permissions_or_authenticate(tmp_path: Path) -> None:
    async def check() -> None:
        client = CapClient()
        reader = make_controller(client, tmp_path, password="TEST-PASSWORD")
        result = await reader.set_generation_limit(100, 100)
        assert not result.changed
        assert result.request_id is None
        assert not client.writes
        assert client.login_count == client.heartbeat_count == 0
        assert not client.log_path.exists()
        await reader.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    ("register", "value", "message"),
    [
        (rn.ACTIVE_POWER_ADJUSTMENT_MODE, 1, "another control mode"),
        (rn.ACTIVE_POWER_ADJUSTMENT_COMMAND, 40126, "another control mode"),
        (rn.ACTIVE_POWER_ADJUSTMENT_VALUE, None, "Invalid or missing"),
        (rn.ACTIVE_POWER_ADJUSTMENT_VALUE, 0xFFFFFFFF, "Invalid active"),
        (rn.ACTIVE_POWER_PERCENTAGE_DERATING, None, "steps of 0.1"),
        (rn.ACTIVE_POWER_ADJUSTMENT_VALUE, 900, "current cap differs"),
    ],
)
def test_unsupported_or_inconsistent_control_state_is_not_overwritten(
    tmp_path: Path, register: rn.RegisterName, value: object, message: str
) -> None:
    async def check() -> None:
        client = CapClient()
        client.values[register] = value
        reader = make_controller(client, tmp_path)
        with pytest.raises(SolarControlError, match=message):
            await reader.set_generation_limit(50, 100)
        assert not client.writes
        assert not client.log_path.exists()
        await reader.close()

    asyncio.run(check())


def test_stale_expectation_or_changed_identity_prevents_write(tmp_path: Path) -> None:
    async def check() -> None:
        client = CapClient()
        reader = make_controller(client, tmp_path)
        with pytest.raises(SolarControlError, match="current cap differs"):
            await reader.set_generation_limit(50, 90)
        await reader.get_generation_limit()
        client.values[rn.SERIAL_NUMBER] = "OTHER-DEVICE"
        with pytest.raises(SolarControlError, match="identity"):
            await reader.set_generation_limit(50, 100)
        assert not client.writes
        assert not client.log_path.exists()
        await reader.close()

    asyncio.run(check())


@pytest.mark.parametrize("error", [PermissionDeniedError(6), TimeoutError(), OSError("connection lost")])
def test_write_failure_is_not_retried_or_automatically_rolled_back(
    tmp_path: Path, error: Exception
) -> None:
    async def check() -> None:
        client = CapClient()
        client.write_error = error
        reader = make_controller(client, tmp_path)
        with pytest.raises(SolarControlError, match="cap may have changed"):
            await reader.set_generation_limit(50, 100)
        assert client.writes == [(40125, 500)]
        records = [json.loads(line) for line in client.log_path.read_text().splitlines()]
        assert [record["event"] for record in records] == ["prepared"]
        assert records[0]["previous_percent"] == 100
        assert not client.connected
        await reader.close()

    asyncio.run(check())


def test_unchanged_readback_is_not_reported_as_success(tmp_path: Path) -> None:
    async def check() -> None:
        client = CapClient()
        client.apply_write = False
        reader = make_controller(client, tmp_path)
        with pytest.raises(SolarControlError, match="configured cap did not match"):
            await reader.set_generation_limit(50, 100)
        assert client.writes == [(40125, 500)]
        assert len(client.log_path.read_text().splitlines()) == 1
        await reader.close()

    asyncio.run(check())


def test_mixed_readback_is_explicit_and_can_be_restored_with_fresh_expectations(tmp_path: Path) -> None:
    async def check() -> None:
        client = CapClient()
        client.update_active = False
        reader = make_controller(client, tmp_path)
        changed = await reader.set_generation_limit(99, 100, 100)
        assert changed.changed and changed.configuration_verified
        assert changed.limit.percent == 99 and changed.limit.active_percent == 100
        assert not changed.active_readback_matches
        assert "output restriction is not confirmed" in changed.warning
        records = [json.loads(line) for line in client.log_path.read_text().splitlines()]
        assert records[-1]["event"] == "verified"
        assert records[-1]["active_readback_matches"] is False
        with pytest.raises(SolarControlError, match="current cap differs"):
            await reader.set_generation_limit(100, 99)
        current = await reader.get_generation_limit()
        restored = await reader.set_generation_limit(
            changed.previous_percent, current.percent, current.active_percent
        )
        assert restored.changed and restored.active_readback_matches
        assert restored.warning is None
        assert restored.limit.percent == restored.limit.active_percent == 100
        assert client.writes == [(40125, 990), (40125, 1000)]
        await reader.close()

    asyncio.run(check())


def test_journal_failure_prevents_cap_write(tmp_path: Path) -> None:
    async def check() -> None:
        client = CapClient()
        reader = make_controller(client, tmp_path)
        client.log_path.mkdir()
        with pytest.raises(SolarControlError, match="operation failed"):
            await reader.set_generation_limit(50, 100)
        assert not client.writes
        await reader.close()

    asyncio.run(check())


def test_verification_journal_failure_reports_uncertain_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        client = CapClient()
        reader = make_controller(client, tmp_path)
        record = reader._record_cap_event

        def fail_verified(data: dict[str, object], event: str) -> None:
            if event == "verified":
                raise OSError("test full disk")
            record(data, event)

        monkeypatch.setattr(reader, "_record_cap_event", fail_verified)
        with pytest.raises(SolarControlError, match="cap may have changed"):
            await reader.set_generation_limit(50, 100)
        assert client.values[rn.ACTIVE_POWER_PERCENTAGE_DERATING] == 50
        assert client.writes == [(40125, 500)]
        await reader.close()

    asyncio.run(check())


@pytest.mark.parametrize("stage", ["login", "heartbeat", "success"])
def test_optional_installer_session_is_only_used_for_an_explicit_change(
    tmp_path: Path, stage: str
) -> None:
    async def check() -> None:
        client = CapClient()
        client.login_succeeds = stage != "login"
        client.heartbeat_succeeds = stage != "heartbeat"
        reader = make_controller(client, tmp_path, password="TEST-PASSWORD")
        await reader.get_generation_limit()
        assert client.login_count == client.heartbeat_count == 0
        if stage == "success":
            assert (await reader.set_generation_limit(50, 100)).changed
            assert client.writes == [(40125, 500)]
        else:
            with pytest.raises(SolarControlError, match="no cap write was attempted"):
                await reader.set_generation_limit(50, 100)
            assert not client.writes
        assert client.login_count == 1
        assert client.heartbeat_count == (0 if stage == "login" else 1)
        await reader.close()

    asyncio.run(check())


def test_concurrent_changes_are_serialized_and_do_not_reuse_stale_expectations(tmp_path: Path) -> None:
    async def check() -> None:
        client = CapClient()
        client.delay = 0.001
        reader = make_controller(client, tmp_path)
        results = await asyncio.gather(
            reader.set_generation_limit(50, 100), reader.set_generation_limit(75, 100),
            return_exceptions=True,
        )
        assert sum(isinstance(result, GenerationLimitChange) for result in results) == 1
        assert sum(isinstance(result, SolarControlError) for result in results) == 1
        assert len(client.writes) == 1
        await reader.close()

    asyncio.run(check())


def test_cancelled_write_is_not_retried_and_shutdown_does_not_restore(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def check() -> None:
        client = CapClient()
        client.write_delay = 10
        reader = make_controller(client, tmp_path)
        pending = asyncio.create_task(reader.set_generation_limit(50, 100))
        await asyncio.wait_for(client.write_started.wait(), 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert "cancelled after transmission" in caplog.text
        assert client.values[rn.ACTIVE_POWER_PERCENTAGE_DERATING] == 50
        assert client.writes == [(40125, 500)]
        assert not client.connected
        assert len(client.log_path.read_text().splitlines()) == 1
        await reader.close()
        assert client.writes == [(40125, 500)]

    asyncio.run(check())


def test_control_deadline_includes_lock_wait_without_interrupting_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        client = CapClient()
        reader = make_controller(client, tmp_path)
        await reader.get_generation_limit()
        monkeypatch.setattr(reader_module, "REFRESH_TIMEOUT_SECONDS", 0.02)
        async with reader._lock:
            with pytest.raises(SolarControlError, match="TimeoutError"):
                await reader.set_generation_limit(50, 100)
            assert client.connected
        assert not client.writes
        await reader.close()
        with pytest.raises(SolarControlError, match="closed"):
            await reader.get_generation_limit()
        with pytest.raises(SolarControlError, match="closed"):
            await reader.set_generation_limit(50, 100)

    asyncio.run(check())


def test_deadline_after_transmission_preserves_uncertain_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        client = CapClient()
        client.write_delay = 10
        reader = make_controller(client, tmp_path)
        monkeypatch.setattr(reader_module, "REFRESH_TIMEOUT_SECONDS", 0.02)
        with pytest.raises(SolarControlError, match="cap may have changed"):
            await reader.set_generation_limit(50, 100)
        assert client.values[rn.ACTIVE_POWER_PERCENTAGE_DERATING] == 50
        assert client.writes == [(40125, 500)]
        assert not client.connected
        assert len(client.log_path.read_text().splitlines()) == 1
        await reader.close()

    asyncio.run(check())


def test_external_change_during_login_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        client = CapClient()
        reader = make_controller(client, tmp_path, password="TEST-PASSWORD")

        async def concurrent_login(username: str, password: str) -> bool:
            client.values[rn.ACTIVE_POWER_PERCENTAGE_DERATING] = 90
            client.values[rn.ACTIVE_POWER_ADJUSTMENT_VALUE] = 900
            return True

        monkeypatch.setattr(client, "login", concurrent_login)
        with pytest.raises(SolarControlError, match="cap changed during login"):
            await reader.set_generation_limit(50, 100)
        assert not client.writes
        assert not client.log_path.exists()
        await reader.close()

    asyncio.run(check())


def test_generation_limit_reads_are_never_cached(tmp_path: Path) -> None:
    async def check() -> None:
        client = CapClient()
        reader = make_controller(client, tmp_path)
        first = await reader.get_generation_limit()
        client.values[rn.ACTIVE_POWER_PERCENTAGE_DERATING] = 75
        client.values[rn.ACTIVE_POWER_ADJUSTMENT_VALUE] = 750
        second = await reader.get_generation_limit()
        assert first.percent == 100
        assert second.percent == second.active_percent == 75
        assert second.observed_at >= first.observed_at
        assert not client.writes
        await reader.close()

    asyncio.run(check())
