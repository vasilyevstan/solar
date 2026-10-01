# solar-mcp

A local, **read-only**, stdio [Model Context Protocol](https://modelcontextprotocol.io/)
server for Huawei SUN2000 inverter telemetry. It exposes exactly one tool:
`get_solar_status`.

This release monitors generation; it cannot adjust the inverter. Generation is
**not** household consumption, grid export, or available surplus. There are no
control, arbitrary-register, login, heartbeat, discovery, HTTP, or automation tools.

## Requirements

- Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).
- A Huawei SUN2000 inverter reachable through a compatible local Modbus TCP
  connection, with Modbus access enabled by the device owner/installer.
- The inverter's unit ID. The verified SUN2000-8KTL-M0 / SDongleA-05 installation
  uses **unit 1**, not the library's unit-0 default.

Only that inverter/dongle combination has been exercised on hardware. Other
SUN2000 models must provide the same registers; missing data is an error rather
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

## Development

```sh
uv sync --frozen --group dev
uv run --frozen pytest
uv build
```

Tests use fake clients and a loopback Modbus server, not physical hardware. The
stdio integration test exercises the actual SDK and Huawei library, verifies the
result schema and errors, and asserts that all device traffic is function-03
holding-register reads to the intended addresses.

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
