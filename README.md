# solar-mcp

A local stdio [Model Context Protocol](https://modelcontextprotocol.io/) server for
Huawei SUN2000 inverter telemetry, **read-only by default**, with optional
percentage generation-cap control.

| Tool | Availability | Purpose |
|---|---|---|
| `get_solar_status` | Always | Generation, energy counters, device status, and freshness |
| `get_generation_limit` | Always | Fresh configured and active percentage-cap readbacks |
| `set_generation_limit` | Explicit local opt-in | Change the percentage power cap with expected-value and readback checks |

Generation is **not** household consumption, grid export, or available surplus.
There are no arbitrary-register, start/stop, grid-code, reactive-power, discovery,
HTTP, or scheduling tools. Installer login and one session heartbeat are used only
for an explicit cap change when an installer password has been configured.

## Requirements

- Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).
- A Huawei SUN2000 inverter reachable through a compatible local Modbus TCP
  connection, with Modbus access enabled by the device owner/installer.
- The inverter's unit ID. The verified SUN2000-8KTL-M0 / SDongleA-05 installation
  uses **unit 1**, not the library's unit-0 default.

Only telemetry and cap **reads** on that inverter/dongle combination have been
exercised on hardware. Live write permission and physical curtailment have not yet
been verified. Cap writes are covered using simulated devices and the real library.
Other SUN2000 models must provide the same registers; missing data is an error rather
than an invented reading. Software updates and commissioning are outside this
server's scope. Do not start it during an active firmware update.

Run **one server instance per inverter endpoint**, not separate instances in
several MCP clients. Close competing commissioning sessions when troubleshooting.
The server serializes its own requests, but does not coordinate other processes.
Keep Modbus on a trusted LAN; never expose port 502 to the internet.

## Install and configure

```sh
git clone https://github.com/vasilyevstan/solar.git
cd solar
uv sync --frozen --no-dev
cp .env.example .env
```

Edit the ignored `.env` with your actual address. `192.0.2.10` in the example
is a documentation placeholder, not a discoverable device.

| Setting | Default | Meaning |
|---|---|---|
| `SOLAR_INVERTER_HOST` | Required | Hostname or LAN IP, without a URL scheme |
| `SOLAR_INVERTER_PORT` | `502` | Modbus TCP port |
| `SOLAR_INVERTER_UNIT_ID` | `1` | Inverter unit, between 0 and 247 |
| `SOLAR_EXPECTED_SERIAL` | Unset | Optional expected inverter serial; kept local and never returned by the tool |
| `SOLAR_ALLOW_CONTROL` | `0` | Set exactly `1` to register the cap setter; requires `SOLAR_EXPECTED_SERIAL` |
| `SOLAR_CONTROL_LOG` | `~/.local/state/solar-mcp/generation-caps.jsonl` | Private, durable cap-change journal; overrides must be absolute paths |
| `SOLAR_INSTALLER_PASSWORD` | Unset | Optional local installer password, used only during an explicit cap change |

The server reads environment variables, not `.env` files itself. For a direct
stdio launch, let uv load the local file:

```sh
uv run --frozen --no-dev --env-file .env solar-mcp
```

This starts an MCP protocol process, not an interactive dashboard. Use an MCP
client to send requests. Host, port, and unit can alternatively be set with
`--inverter-host`, `--inverter-port`, and `--unit-id`; command-line values take
precedence over environment variables.

### MCP client configuration

Use the following stdio entry in your client's MCP server configuration, replacing
the directory and placeholder IP. The exact outer configuration file is
client-specific. Ensure `uv` is on the client's executable search path.

```json
{
  "mcpServers": {
    "solar-mcp": {
      "command": "uv",
      "args": [
        "run", "--directory", "/absolute/path/to/solar",
        "--frozen", "--no-dev", "solar-mcp"
      ],
      "env": {
        "SOLAR_INVERTER_HOST": "192.0.2.10",
        "SOLAR_INVERTER_UNIT_ID": "1"
      }
    }
  }
}
```

## Tool result

`get_solar_status` takes no arguments. It returns structured data with:

| Field | Meaning |
|---|---|
| `model`, `software_version` | Inverter model and running inverter firmware, not dongle firmware |
| `generation_w` | Signed active power in watts; zero is a valid observation |
| `daily_yield_kwh` | Daily inverter generation counter |
| `lifetime_yield_kwh` | Accumulated inverter generation counter |
| `device_status_code`, `device_status` | Raw operating code and description |
| `observed_at` | UTC time at the start of the telemetry read sequence |
| `age_seconds` | Age since that observation, measured with a monotonic clock |
| `from_cache` | Whether this call reused an observation less than 30 seconds old |
| `source` | Always `huawei_modbus` |

Readings are sequential, not an atomic meter snapshot. The daily counter follows
the inverter's own day boundary; the observation timestamp is UTC. The server
does not persist telemetry or contact FusionSolar.

Successful readings are cached on demand for **less than 30 seconds**. Cache hits
do not move the observation timestamp. There is no background polling. A known
disconnected connection invalidates cache use. Call again after the cache window
for another live observation.

Each new connection waits one second before reading and verifies the model and
serial. The first accepted identity is retained for the process lifetime, so a
different device after reconnect is rejected. Set `SOLAR_EXPECTED_SERIAL` to
enforce that identity across process restarts as well.

Connection/response timeouts are 10 seconds. A tool call, including waiting for
another request, has a 30-second deadline, plus at most two seconds for connection
cleanup. There are no hidden reconnect/retry loops: after a failed read, the
connection is closed and the next tool call makes one fresh attempt.

Failures produce an MCP `isError` response, never successful stale values or
fabricated zeroes. Invalid register sentinels and missing/unsupported data are
errors. Unknown operating codes are preserved and explicitly labelled unknown.
Logs go to stderr; stdout is reserved for MCP messages.

## Generation caps: power, not energy

A cap limits instantaneous output **power** (W or kW), not daily **energy**
(kWh). For example, output held at 4 kW for one hour generates 4 kWh; at 2 kW
for half an hour it generates 1 kWh. A cap cannot force more production than
sunlight makes available and is not a household export limit.

This release changes only the existing **percentage** mode, register `40125`.
The percentage is the inverter's own power reference, not a kWh target. The
server does not convert percentages into promised watts. It refuses to replace
fixed-watt or other control modes, and does not change maximum hardware power.

`get_generation_limit` has no arguments and never uses a cache. It returns
`percent` (configured), `active_percent` (active adjustment readback),
`observed_at` (UTC), and `control_enabled`. Unsupported modes and invalid
register values are explicit errors. A mismatch between configured and active
values prevents writing; do not assume the requested cap has taken effect.

### Enable and use control

Control remains off in the example configuration. To opt in, set the following
in your private environment and restart the MCP:

```sh
SOLAR_ALLOW_CONTROL=1
SOLAR_EXPECTED_SERIAL=your-inverter-serial
```

Keep the installer password in private local configuration only if your device
requires authentication. It is never a tool argument or result, is not logged,
and is not used by telemetry or cap reads. An authentication failure is an error;
the server does not guess credentials or retry failed writes.

For an explicitly requested percentage change:

1. Call `get_generation_limit` and check that `percent` and `active_percent` agree.
2. Call `set_generation_limit` with `percent` and the freshly read `expected_current_percent`, both numeric values in **0.1% steps, from 0 to 100**.
3. Keep the result's `previous_percent` if you intend to restore it. Restore explicitly using the same setter with that previous percentage and a new current-value expectation.

For example, only when the current cap is 100% and you actually want 50%:

```json
{
  "percent": 50.0,
  "expected_current_percent": 100.0
}
```

**Zero can stop generation.** 100% removes this percentage restriction, not
other inverter limits. A request for the already active value returns
`changed: false` without writing or authenticating; it does not prove write
permission.

The setter serializes with reads, checks device identity again, rejects stale
expectations, and durably saves the previous value and intent before sending
one exact function-06 write to `40125`. It checks the write acknowledgment and
both configured and active cap values before returning `changed: true`. It may
read back up to three times, but **never automatically retries a cap write**.
Readback verifies the settings, not the actual reduction in generation;
physical curtailment must be checked separately while sufficient sunlight exists.

**Treat changes as persistent.** Exiting or disconnecting does not trigger a
restore. If a write times out, is cancelled, or fails verification, it may still
have applied. Read the current cap before any further action; do not blindly
retry or assume the original cap was restored. The private journal contains
paired `prepared`/`verified` records, including previous/requested percentages
and a request ID. An unpaired `prepared` record has an uncertain outcome and
preserves the information needed for an explicit recovery.

The journal is permission-restricted and must be writable; inability to persist
intent blocks the cap write. It contains device identity, so do not publish it.
The repository ignores `*.jsonl` files.

Do not run competing controllers, including simultaneous commissioning/cloud
setting changes. Expected-value checking is not an atomic lock against other
Modbus clients or FusionSolar; a later external change can override the result.

## Development

```sh
uv sync --frozen --group dev
uv run --frozen pytest
uv build
```

Tests use fake clients and a loopback Modbus server, not physical hardware. The stdio integration tests exercise the actual SDK and Huawei library, verify
schemas and errors, assert that default-mode traffic is only function-03 reads,
and check the exact function-06 percentage words in opt-in mode. Tests also cover
identity and expected-value checks, durable intent, permission failures, explicit
restoration, cancellation, serialization, and unchanged monitoring behavior.

## License and dependencies

Licensed under **AGPL-3.0-only**; see [LICENSE](LICENSE).

Register decoding and Modbus operations use
[huawei-solar](https://github.com/wlcrs/huawei-solar-lib) (AGPLv3). The transport is
composed with its [tmodbus](https://pypi.org/project/tmodbus/) dependency so automatic
reconnect cannot bypass identity checks. The official
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) is MIT-licensed.
Dependency versions are pinned and resolved in `uv.lock`.

Vendor documents and firmware, local settings, device identifiers, and diagnostic
artifacts are not distributed here. The local `docs/` directory is ignored.
