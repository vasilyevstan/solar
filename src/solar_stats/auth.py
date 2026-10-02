from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass, field

from .models import StatsError


@dataclass(frozen=True)
class Credentials:
    username: str = field(repr=False)
    password: str = field(repr=False)


async def _keychain_output(service: str, *, password: bool = False) -> str:
    command = ["/usr/bin/security", "find-generic-password", "-s", service]
    if password:
        command.append("-w")
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        async with asyncio.timeout(30):
            output, _ = await process.communicate()
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode != 0:
        raise StatsError(
            "keychain_unavailable",
            "Unlock the login Keychain and allow the macOS security helper to read the configured entry.",
        )
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError:
        raise StatsError("invalid_credentials", "The Keychain entry must contain UTF-8 text.") from None


async def keychain_credentials(service: str) -> Credentials:
    if sys.platform != "darwin":
        raise StatsError("invalid_config", "Keychain login requires macOS; leave it unset for browser-session reuse.")
    try:
        attributes = await _keychain_output(service)
        match = re.search(r'^\s*"acct"<blob>=(.+)$', attributes, re.MULTILINE)
        if match is None:
            raise ValueError
        encoded = match[1].strip()
        username = (
            bytes.fromhex(encoded.split()[0][2:]).decode("utf-8")
            if encoded.startswith("0x")
            else json.loads(encoded)
        )
        if not isinstance(username, str) or not username.strip():
            raise ValueError
        password = (await _keychain_output(service, password=True)).removesuffix("\n")
        if not password:
            raise ValueError
        return Credentials(username, password)
    except ValueError:
        raise StatsError("invalid_credentials", "The Keychain entry needs a nonempty account name and password.") from None
    except TimeoutError:
        raise StatsError("keychain_unavailable", "Keychain access timed out; approve access locally before retrying.") from None
    except OSError:
        raise StatsError("keychain_unavailable", "The macOS Keychain helper could not be started.") from None
