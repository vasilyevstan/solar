# solar-mcp and solar-stats

Two independent stdio MCP servers: **`solar-mcp`** connects locally to an inverter;
**[`solar-stats`](#solar-stats-fusionsolar-history)** reads historical plant
production from an authenticated FusionSolar browser session. The cloud-history
server never connects to or controls the inverter.

## solar-mcp: local inverter

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

Telemetry, percentage-cap configuration writes, and restoration on that
inverter/dongle combination have been exercised on hardware. Physical curtailment
under sufficient sunlight has not yet been verified.
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
register values are explicit errors. Configured and active values can differ;
do not assume a stored cap has taken effect.

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

1. Call `get_generation_limit` and inspect both `percent` and `active_percent`.
2. Call `set_generation_limit` with the requested `percent`, `expected_current_percent` from the configured readback, and `expected_active_percent` from the active readback. All are numeric values in **0.1% steps, from 0 to 100**.
3. Keep the result's `previous_percent` if you intend to restore it. Restore explicitly using the same setter with that previous percentage and both freshly read expectations.

`expected_active_percent` defaults to `expected_current_percent` for callers
whose readbacks agree. When they differ, supply the actual active expectation
explicitly; otherwise the setter rejects the request without writing. This also
allows guarded restoration of a stored change that has not become active.

For example, only when the current cap is 100% and you actually want 50%:

```json
{
  "percent": 50.0,
  "expected_current_percent": 100.0,
  "expected_active_percent": 100.0
}
```

**Zero can stop generation.** 100% removes this percentage restriction, not
other inverter limits. A request for the already active value returns
`changed: false` without writing or authenticating; it does not prove write
permission.

The setter serializes with reads, checks device identity again, rejects stale
configured or active expectations, and durably saves the previous value and
intent before sending one function-06 write to `40125` through the Huawei library.
It checks the acknowledgment and configured cap before returning `changed: true`
with `configuration_verified: true`. It also reads the active adjustment, up to
three times, but **never automatically retries a cap write**.

`changed: true` means the **stored setting** changed, not that output is already
limited. `active_readback_matches` reports whether the active adjustment agrees.
If it does not, the result includes a warning: the cap may be pending or overridden,
and its output restriction is unconfirmed. This distinction is necessary because
the two readbacks can differ while the inverter is in no-irradiation standby.
Physical curtailment must be checked separately while sufficient sunlight exists.
The hardware check changed the stored cap from 100% to 99% and restored 100%;
the active readback stayed at 100% in standby. Both final readbacks were 100%.

**Treat changes as persistent.** Exiting or disconnecting does not trigger a
restore. If a write times out, is cancelled, or fails verification, it may still
have applied. Read the current cap before any further action; do not blindly
retry or assume the original cap was restored. The private journal contains
paired `prepared`/`verified` records, including previous/requested percentages,
active readback, and a request ID. `verified` means the configured value was
verified; its `active_readback_matches` field records the separate active result.
An unpaired `prepared` record has an uncertain outcome and
preserves the information needed for an explicit recovery.

The journal is permission-restricted and must be writable; inability to persist
intent blocks the cap write. It contains device identity, so do not publish it.
The repository ignores `*.jsonl` files.

Do not run competing controllers, including simultaneous commissioning/cloud
setting changes. Expected-value checking is not an atomic lock against other
Modbus clients or FusionSolar; a later external change can override the result.

## solar-stats: FusionSolar history

`solar-stats` exposes one read-only tool:

```text
get_generation(start_date, end_date=None, format="json")
```

Dates use `YYYY-MM-DD`. Omitting the end date selects one day; a period is
inclusive and may cross years. The default structured result includes:

- `generation_kwh`: the requested period's sum, in **kWh**, not instantaneous kW.
- `daily`: each requested date, its `generation_kwh`, and `source_missing`.
- `missing_dates`, `source_complete`, source/plant identity, and UTC `retrieved_at`.
- `date_basis: "plant_report_calendar"`: source daily labels are not shifted to UTC.

The metric is **Plant Report / PV Yield (kWh)**, not inverter lifetime counters,
household consumption, export, or revenue. Queries read FusionSolar each time;
saved CSVs are exports, **never a default cache or fallback**.

### Browser-session setup

Two browser modes are available. The default **`attach`** mode preserves existing
behavior: reuse a dedicated, signed-in Chrome session through loopback remote
debugging. **`managed`** mode starts installed Google Chrome automatically, with
a persistent private profile, and closes only that owned browser after the query.
Managed mode is headless by default and does not expose a debugging port.

Do not use your normal browsing profile. For `attach` mode, keep your configured
plant open and do not expose its debugging port to the network. For example, on
macOS, launch a separate Chrome window:

```sh
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir=/absolute/private/path/solar-stats-browser \
  --remote-debugging-port=0 --remote-debugging-address=127.0.0.1 \
  "https://your-region.fusionsolar.huawei.com"
```

Sign in directly in that window and open your plant. Copy `.env.solar-stats.example` to the ignored
`.env.solar-stats` and configure:

| Setting | Meaning |
|---|---|
| `SOLAR_STATS_PLANT_URL` | Your authenticated plant page containing `/view/station/NE=...`; no query parameters, passwords, or tokens |
| `SOLAR_STATS_PROFILE_DIR` | Absolute dedicated profile path; managed mode creates it privately and requires directory mode `0700` |
| `SOLAR_STATS_TIMEOUT_SECONDS` | Whole-query deadline, including waiting for the shared profile; default 300 |
| `SOLAR_STATS_BROWSER_MODE` | `attach` (default) or `managed`; never run both modes against the same profile concurrently |
| `SOLAR_STATS_HEADLESS` | `true` (default) or `false`, used by managed mode |
| `SOLAR_STATS_KEYCHAIN_SERVICE` | Optional macOS generic-password item name for automatic login; unset by default |

For automatic startup, set `SOLAR_STATS_BROWSER_MODE=managed` and use a separate
private profile on persistent storage. Chrome must already be installed; no
browser download, desktop, scheduler, or background service is required.
macOS and Linux browser sessions are supported (`flock` is required); the optional
credential reader described below is macOS-only.

Treat the profile as sensitive: it retains normal browser session credentials.
The server does not export cookies or copy another browser's authentication
storage. Missing permissions, changed portal layouts, and rate limits remain
explicit errors. This is a web-report integration, **not** an official
northbound/OpenAPI account.

The adapter opens a native child tab from the authenticated plant page, retaining
the browser's normal tab-scoped session without extracting it. It reads monthly reports and
checks all reported pages, dates, units, and plant identity. A local profile lock
serializes simultaneous `solar-stats` processes. Attach mode closes only its own
tab and connection, never the shared Chrome browser. Managed mode restores its
own profile's last session before attempting login.

### Optional macOS Keychain login

In **Keychain Access**, select the **login** keychain and create a new password
item named `solar-stats.fusionsolar`. Its account name is your FusionSolar
username/email; its password is your FusionSolar password. Enter both directly
in Keychain Access, not in chat, shell arguments, environment variables, or Git.
Configure only this reference:

```dotenv
SOLAR_STATS_KEYCHAIN_SERVICE=solar-stats.fusionsolar
```

The reader uses the native `/usr/bin/security` helper. Authorize that application
for this specific item through Keychain Access when prompted; do **not** allow
all applications. That authorization trusts the helper executable, not just this
Python project. The login Keychain must be accessible to the signed-in macOS
account. No new Python credential dependency or plaintext fallback is used.

Queries reuse an authenticated session first. Only a recognized login page on
the configured FusionSolar region triggers a Keychain read and one login attempt.
The username/password stay in local process memory and the browser login form;
the helper's output is captured privately, never returned through MCP or logs.
`DEBUG`/`PWDEBUG` must be unset when Keychain login is enabled to avoid browser
diagnostics exposing form values.

Keychain access is bounded; denial, a locked Keychain, rejected credentials,
MFA, and CAPTCHA are explicit errors, not zero generation. There are no automatic
login retries. Resolve an authentication error before repeating queries. For an
interactive challenge, stop managed queries, open that profile manually with
the Chrome command above, and sign in normally. Close that manual browser before
resuming managed queries. Challenges are never bypassed. Without the Keychain
option, sign in manually and reuse the profile. Headless Linux credential
provisioning is not implemented by this macOS integration.

### MCP and terminal use

With no subcommand, this starts the stdio MCP:

```sh
uv run --no-sync --env-file .env.solar-stats solar-stats
```

For other MCP clients, use the same command with absolute project/configuration
paths and enable only `get_generation`. This registration is independent of
`solar-mcp`; it requires no inverter address or installer password.

Terminal query mode prints raw JSON or CSV to stdout; diagnostics go to stderr:

```sh
uv run --no-sync --env-file .env.solar-stats solar-stats query \
  --start-date 2025-01-01

uv run --no-sync --env-file .env.solar-stats solar-stats query \
  --start-date 2024-12-31 --end-date 2025-01-02

uv run --no-sync --env-file .env.solar-stats solar-stats query \
  --start-date 2025-01-01 --end-date 2025-12-31 --format csv \
  --output state/solar-stats/generation-2025.csv
```

Use a separate query/output filename for each year, including 2023 or 2024.
`--output` writes the result
instead of printing it. CSV output also creates a `.metadata.json` sidecar with
the source, interval, missing dates, and CSV checksum. Keep both together: the
checksum detects an interrupted export or subsequent edits. Existing CSVs are
not modified if fetching fails.

In MCP mode, `format="csv"` places CSV in the tool's text content and retains
structured data/provenance. It does **not** print raw CSV into the MCP protocol.

### Missing data and matrix interpretation

Missing source readings for real requested dates are deliberately returned as
**0**, with `source_missing: true`. A measured zero has `source_missing: false`.
When any day is missing, `source_complete` is false and the total is a
**sum with missing readings treated as zero**, not a verified total of all
electricity actually produced. No interpolation or historical repair is done.

Authentication errors, failed requests, incomplete pagination, invalid values,
or wrong-period data are **not** converted to zero.

The CSV columns are `year,month,1,2,...,31`. Rows follow chronological year/month
order. A full year has January at the top and December at the bottom; partial
and multi-year periods use only their intersecting months. Cells outside the
requested period or on nonexistent dates remain blank. Thus 2024 has 366
numeric date cells (including February 29) and six nonexistent-date blanks;
2025 has 365 numeric date cells and seven blanks.

Exports contain private plant data. The repository excludes `state/`, real
`.env` files, and vendor documentation; never commit report data, profiles,
cookies, or credentials.

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
restoration with differing stored/active readbacks, cancellation, serialization,
and unchanged monitoring behavior.

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
