from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from huawei_solar import register_names as rn
from huawei_solar.modbus_pdu import PermissionDeniedError
from huawei_solar.registers import REGISTERS

from solar_mcp.models import PowerLimitChange
from solar_mcp.reader import SolarControlError
from test_control import CapClient, make_controller


@pytest.mark.parametrize("watts", [0, 1, 222, 8800])
def test_exact_watts_switch_readback_journal_and_restore(tmp_path: Path, watts: int) -> None:
    async def check():
        client = CapClient()
        reader = make_controller(client, tmp_path)
        await reader.read()
        result = await reader.set_power_limit("watts", watts, "percent", 100, 100)
        assert result.write_performed and result.configuration_verified
        assert result.requested_mode == "watts" and result.requested_value == watts
        assert result.previous.mode == "percent" and result.previous.percent == 100
        assert result.limit.mode == "watts"
        assert result.limit.watts == result.limit.active_watts == watts
        assert result.limit.percent is result.limit.active_percent is None
        assert result.active_readback_matches and result.warning is None
        assert client.writes == [(40126, watts)]
        assert REGISTERS[rn.ACTIVE_POWER_FIXED_VALUE_DERATING].encode(watts) == (watts,)
        assert not (await reader.read()).from_cache
        records = [json.loads(line) for line in client.log_path.read_text().splitlines()]
        assert [record["event"] for record in records] == ["prepared", "verified"]
        assert records[0]["previous_target_value"] == 8800
        assert records[0]["previous_mode"] == "percent" and records[0]["requested_value"] == watts
        assert records[1]["active_mode"] == "watts" and records[1]["active_value"] == watts
        assert records[0]["request_id"] == records[1]["request_id"] == result.request_id
        assert client.log_path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(SolarControlError, match="current cap differs"):
            await reader.set_generation_limit(100, 100)
        assert len(client.writes) == 1
        await reader.close()
        reader = make_controller(client, tmp_path)
        fresh = await reader.get_generation_limit()
        restored = await reader.set_power_limit(
            result.previous.mode, result.previous.percent, fresh.mode, fresh.watts, fresh.active_watts
        )
        assert restored.limit.percent == restored.limit.active_percent == 100
        assert restored.limit.mode == "percent" and restored.active_readback_matches
        assert client.writes == [(40126, watts), (40125, 1000)]
        assert client.login_count == client.heartbeat_count == 0
        await reader.close()
    asyncio.run(check())


@pytest.mark.parametrize("value", [-1, 222.5, True, "222", None, float("nan"), float("inf"), 0xFFFFFFFF])
def test_invalid_whole_watts_and_expectations_do_not_connect(tmp_path, value) -> None:
    async def check():
        client = CapClient()
        reader = make_controller(client, tmp_path)
        with pytest.raises(SolarControlError, match="whole number"):
            await reader.set_power_limit("watts", value, "percent", 100, 100)
        with pytest.raises(SolarControlError, match="whole number"):
            await reader.set_power_limit("watts", 222, "watts", value, 222)
        with pytest.raises(SolarControlError, match="whole number"):
            await reader.set_power_limit("watts", 222, "watts", 222, value)
        assert client.connects == 0 and not client.writes
        assert not client.log_path.exists()
        await reader.close()
    asyncio.run(check())


@pytest.mark.parametrize("mode", ["kw", "fixed", "", None])
def test_invalid_modes_do_not_connect(tmp_path, mode) -> None:
    async def check():
        client = CapClient()
        reader = make_controller(client, tmp_path)
        with pytest.raises(SolarControlError, match="mode must be"):
            await reader.set_power_limit(mode, 222, "percent", 100, 100)
        with pytest.raises(SolarControlError, match="mode must be"):
            await reader.set_power_limit("watts", 222, mode, 100, 100)
        assert client.connects == 0
        await reader.close()
    asyncio.run(check())


@pytest.mark.parametrize("maximum", [0, 221, None, 0xFFFFFFFF])
def test_unavailable_or_insufficient_hardware_pmax_blocks_write(tmp_path, maximum) -> None:
    async def check():
        client = CapClient()
        client.values[rn.P_MAX] = maximum
        reader = make_controller(client, tmp_path)
        with pytest.raises(SolarControlError, match="Pmax"):
            await reader.set_power_limit("watts", 222, "percent", 100, 100)
        assert not client.writes and not client.log_path.exists()
        await reader.close()
    asyncio.run(check())


def test_disabled_control_unknown_mode_and_stale_expectations_never_write(tmp_path) -> None:
    async def check():
        client = CapClient()
        disabled = make_controller(client, tmp_path, enabled=False)
        with pytest.raises(SolarControlError, match="disabled"):
            await disabled.set_power_limit("watts", 222, "percent", 100, 100)
        assert client.connects == 0
        await disabled.close()
        reader = make_controller(client, tmp_path)
        for expected_mode, expected_value, expected_active in [("watts", 222, 222), ("percent", 90, 100), ("percent", 100, 90)]:
            with pytest.raises(SolarControlError, match="current cap differs"):
                await reader.set_power_limit("watts", 222, expected_mode, expected_value, expected_active)
        client.values[rn.ACTIVE_POWER_ADJUSTMENT_COMMAND] = 40120
        with pytest.raises(SolarControlError, match="another control mode"):
            await reader.set_power_limit("watts", 222, "percent", 100, 100)
        assert not client.writes and not client.log_path.exists()
        await reader.close()
    asyncio.run(check())


def test_fixed_watt_reads_are_available_with_control_disabled(tmp_path) -> None:
    async def check():
        client = CapClient()
        client.values.update({
            rn.ACTIVE_POWER_ADJUSTMENT_MODE: 1, rn.ACTIVE_POWER_ADJUSTMENT_COMMAND: 40126,
            rn.ACTIVE_POWER_FIXED_VALUE_DERATING: 222, rn.ACTIVE_POWER_ADJUSTMENT_VALUE: 222,
        })
        reader = make_controller(client, tmp_path, enabled=False)
        result = await reader.get_generation_limit()
        assert result.mode == "watts" and result.watts == result.active_watts == 222
        assert result.percent is result.active_percent is None and not result.control_enabled
        client.values[rn.ACTIVE_POWER_ADJUSTMENT_VALUE] = 9000
        with pytest.raises(SolarControlError, match="exceeds"):
            await reader.get_generation_limit()
        assert not client.writes and client.login_count == 0
        await reader.close()
    asyncio.run(check())


def test_pending_mode_change_warns_and_explicit_reassertion_can_restore(tmp_path) -> None:
    async def check():
        client = CapClient()
        client.update_active = False
        reader = make_controller(client, tmp_path)
        result = await reader.set_power_limit("watts", 222, "percent", 100, 100)
        assert result.configuration_verified and not result.active_readback_matches
        assert "output restriction is not confirmed" in result.warning
        assert result.requested_value == client.values[rn.ACTIVE_POWER_FIXED_VALUE_DERATING] == 222
        assert result.limit.mode == "percent" and result.limit.percent == result.limit.active_percent == 100
        fresh = await reader.get_generation_limit()
        restored = await reader.set_power_limit("percent", 100, fresh.mode, fresh.percent, fresh.active_percent)
        assert restored.write_performed and restored.active_readback_matches
        assert client.writes == [(40126, 222), (40125, 1000)]
        await reader.close()
    asyncio.run(check())


def test_stored_watts_are_distinct_from_active_watts(tmp_path) -> None:
    async def check():
        client = CapClient()
        reader = make_controller(client, tmp_path)
        await reader.set_power_limit("watts", 333, "percent", 100, 100)
        client.update_active = False
        result = await reader.set_power_limit("watts", 222, "watts", 333, 333)
        assert result.limit.watts == 222 and result.limit.active_watts == 333
        assert result.configuration_verified and not result.active_readback_matches
        restored = await reader.set_power_limit("watts", 333, "watts", 222, 333)
        assert restored.limit.watts == restored.limit.active_watts == 333
        assert restored.active_readback_matches
        await reader.close()
    asyncio.run(check())


@pytest.mark.parametrize("error", [PermissionDeniedError(16), TimeoutError(), OSError("test failure")])
def test_fixed_write_errors_are_never_retried_or_rolled_back(tmp_path, error) -> None:
    async def check():
        client = CapClient()
        client.write_error = error
        reader = make_controller(client, tmp_path)
        with pytest.raises(SolarControlError, match="cap may have changed"):
            await reader.set_power_limit("watts", 222, "percent", 100, 100)
        assert client.writes == [(40126, 222)]
        records = [json.loads(line) for line in client.log_path.read_text().splitlines()]
        assert [record["event"] for record in records] == ["prepared"]
        assert records[0]["requested_mode"] == "watts"
        assert records[0]["previous_mode"] == "percent"
        assert not client.connected
        await reader.close()
    asyncio.run(check())


def test_wrong_readback_or_failed_acknowledgment_never_reports_success(tmp_path, monkeypatch) -> None:
    async def check():
        client = CapClient()
        client.apply_write = False
        reader = make_controller(client, tmp_path)
        with pytest.raises(SolarControlError, match="configured cap did not match"):
            await reader.set_power_limit("watts", 222, "percent", 100, 100)
        assert client.writes == [(40126, 222)]
        client.apply_write = True
        write = client.set
        async def no_ack(name, value):
            await write(name, value)
            return False
        monkeypatch.setattr(client, "set", no_ack)
        with pytest.raises(SolarControlError, match="cap may have changed"):
            await reader.set_power_limit("watts", 222, "percent", 100, 100)
        assert (await reader.get_generation_limit()).watts == 222
        assert client.writes == [(40126, 222), (40126, 222)]
        await reader.close()
    asyncio.run(check())


def test_fixed_control_preserves_identity_and_journal_gates(tmp_path) -> None:
    async def check():
        client = CapClient()
        reader = make_controller(client, tmp_path)
        await reader.get_generation_limit()
        client.values[rn.SERIAL_NUMBER] = "OTHER-DEVICE"
        with pytest.raises(SolarControlError, match="identity"):
            await reader.set_power_limit("watts", 222, "percent", 100, 100)
        assert not client.writes
        await reader.close()
        client = CapClient()
        reader = make_controller(client, tmp_path)
        client.log_path.mkdir()
        with pytest.raises(SolarControlError, match="operation failed"):
            await reader.set_power_limit("watts", 222, "percent", 100, 100)
        assert not client.writes
        await reader.close()
    asyncio.run(check())


def test_concurrent_mode_changes_are_serialized(tmp_path) -> None:
    async def check():
        client = CapClient()
        client.delay = 0.001
        reader = make_controller(client, tmp_path)
        results = await asyncio.gather(
            reader.set_power_limit("watts", 222, "percent", 100, 100),
            reader.set_power_limit("watts", 333, "percent", 100, 100),
            return_exceptions=True,
        )
        assert sum(isinstance(value, PowerLimitChange) for value in results) == 1
        assert sum(isinstance(value, SolarControlError) for value in results) == 1
        assert len(client.writes) == 1
        await reader.close()
    asyncio.run(check())


def test_cancellation_after_fixed_write_retains_intent_and_never_restores(tmp_path) -> None:
    async def check():
        client = CapClient()
        client.write_delay = 10
        reader = make_controller(client, tmp_path)
        operation = asyncio.create_task(reader.set_power_limit("watts", 222, "percent", 100, 100))
        await asyncio.wait_for(client.write_started.wait(), 1)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert client.writes == [(40126, 222)]
        assert client.values[rn.ACTIVE_POWER_FIXED_VALUE_DERATING] == 222
        assert len(client.log_path.read_text().splitlines()) == 1
        assert not client.connected
        await reader.close()
        assert len(client.writes) == 1
    asyncio.run(check())
