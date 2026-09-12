# Home Assistant History Exporter

Keep your own copy of your Home Assistant history.

HHE is a command-line tool that reads raw Recorder state changes through the
official Home Assistant REST API and writes them to your disk, one complete
local day at a time, as JSONL, Parquet, or CSV. Everything it does with Home
Assistant is read-only.

```console
$ pipx install ha-history-exporter
$ hhe init
$ hhe export
```

## Contents

- [Why](#why)
- [Install](#install)
- [Quick start](#quick-start)
- [Join the community](#join-the-community)
- [Configuration](#configuration)
- [Credentials and security](#credentials-and-security)
- [Command reference](#command-reference)
- [Output](#output)
- [Troubleshooting](#troubleshooting)
- [Automating daily exports](#automating-daily-exports)
- [Development](#development)
- [License](#license)

## Why

The Home Assistant Recorder keeps a limited window of history — often two or
three weeks — and then purges it. Long-term statistics survive, but they are
hourly aggregates: they tell you the average temperature of an hour, not that a
door opened at 18:42:07 and closed nineteen seconds later.

HHE exports those raw state changes before they are purged, into an archive
that outlives your Home Assistant database and can be analysed with DuckDB,
Pandas, or Polars.

**It runs entirely on your machine.** HHE talks to one address: the Home
Assistant instance you name. A local hostname, a LAN IP, a Tailscale address —
anything this computer can reach. It needs no internet connection, no cloud
account, and no service in between; nothing is uploaded anywhere, and your
history never leaves your own disk. It also needs no add-on, no database
access, and no change to your Home Assistant configuration.

**What is exported**

- every raw Recorder state change of every entity currently in `/api/states`
- entities whose current state is `unknown` or `unavailable` (they may still
  have history)
- complete local calendar days, including the 23-hour and 25-hour days at
  daylight-saving transitions

**What is not exported**

- long-term statistics — the History API does not serve them
- entities that no longer exist in Home Assistant
- today, unless you ask for it by name (see
  [Exporting today](#exporting-today))

## Install

### pipx (recommended)

[pipx](https://pipx.pypa.io/) puts the tool in its own environment and the
commands on your `PATH`:

```bash
pipx install ha-history-exporter
```

Two commands are installed and behave identically: the long
`ha-history-exporter` and the short `hhe`. `python -m ha_history_exporter`
works as well.

Parquet output needs [pyarrow](https://arrow.apache.org/docs/python/), a large
platform-specific package, so it is optional:

```bash
pipx inject ha-history-exporter pyarrow
#   or, when installing with pip inside a virtual environment:
pip install "ha-history-exporter[parquet]"
```

Requires Python 3.10 or newer. The release test matrix covers Linux and Windows
on Python 3.10 and 3.13. macOS uses the same Python interfaces but is not yet
part of the continuous test matrix.

### From a clone

```bash
git clone https://github.com/hyperfelixations/ha-history-exporter.git
cd ha-history-exporter
pip install .
hhe export
```

`python -m ha_history_exporter export` works from a checkout without installing
anything, as long as the dependencies are present.

## Quick start

```console
$ hhe init
Home Assistant URL [http://homeassistant.local:8123]: https://ha.example.net
Long-lived access token (input hidden): ********
  -> Contacting https://ha.example.net ...
     OK (1258 entities)
Output directory [/home/you/ha-history-exports]:
Output formats [jsonl]: jsonl,parquet
Timezone [Europe/Berlin]:

Wrote /home/you/.config/ha-history-exporter/config.yaml
Wrote /home/you/.config/ha-history-exporter/credentials.yaml (access token, not readable by other users)

Next:  hhe export
```

Create the token in Home Assistant under **Profile → Security → Long-Lived
Access Tokens**. Then export:

```bash
hhe export                        # the most recent complete day
hhe export --last-days 7          # the seven most recent complete days
hhe export --date 2026-06-15 --force
hhe export --start-date 2026-06-01 --end-date yesterday
```

Days that already have a successful manifest are skipped, so running the same
command twice costs nothing and re-running a range only fetches what is
missing.

`hhe doctor` checks everything at once and prints the command that fixes each
problem it finds.

## Join the community

Questions, setups and ideas are welcome as
[issues](https://github.com/hyperfelixations/ha-history-exporter/issues). The
links below are for following along as new things are built.

[![GitHub](https://img.shields.io/badge/GitHub-Follow-181717?logo=github&logoColor=white)](https://github.com/hyperfelixations)
[![YouTube](https://img.shields.io/badge/YouTube-Subscribe-FF0000?logo=youtube&logoColor=white)](https://www.youtube.com/@hyperfelixations)
[![Instagram](https://img.shields.io/badge/Instagram-Follow-E4405F?logo=instagram&logoColor=white)](https://www.instagram.com/hyperfelixations/)

## Configuration

Nothing has to be configured: with `HHE_URL` and `HHE_TOKEN` in the
environment, `hhe export` already works and writes to
`<your home directory>/ha-history-exports`.

### Where settings come from

Sources are merged, highest precedence first:

| # | Source | Example |
|---|---|---|
| 1 | command-line options | `--batch-size 15` |
| 2 | environment variables | `HHE_REQUESTS_BATCH_SIZE=15` |
| 3 | your configuration file | see `hhe config path` |
| 4 | built-in defaults | |

There is **one** configuration file. HHE never picks one up from the directory
you happen to be standing in — a tool whose behaviour depends on that is a tool
nobody can reason about. To use a different file for one run, name it:
`hhe export --config ./other.yaml`. That file then replaces the usual one
entirely.

`hhe config list --origin` shows every effective value together with the source
it came from, which is the fastest way to answer "why is it doing that?".

### Where the files live

| | Linux | macOS | Windows |
|---|---|---|---|
| configuration | `~/.config/ha-history-exporter/config.yaml` | `~/Library/Application Support/ha-history-exporter/config.yaml` | `%APPDATA%\ha-history-exporter\config.yaml` |
| credentials | same directory, `credentials.yaml` | same | same |
| exports | `~/ha-history-exports` | same | `%USERPROFILE%\ha-history-exports` |

`hhe config path` prints the resolved paths on your machine.

### Changing settings

```bash
hhe config set export.formats jsonl,parquet
hhe config set export.output_dir /mnt/archive/ha-history
hhe config set requests.batch_size 15
hhe config get export.formats
hhe config unset export.formats      # back to the default
hhe config edit                      # open the file in $EDITOR
```

Writes always go to your configuration file, never to a file you passed with
`--config`. The file lists every key with its documentation; keys you have not
set appear commented out, showing the built-in default for your machine, so it
is also the reference you can read offline.

### Environment variables

Every key has one: `HHE_` followed by the key path in upper case with dots
replaced by underscores, for example `HHE_EXPORT_OUTPUT_DIR`,
`HHE_EXPORT_FORMATS`, `HHE_REQUESTS_BATCH_SIZE`.

The two credential keys have short names of their own: `HHE_URL` and
`HHE_TOKEN`. `HA_URL` and `HA_TOKEN` keep working and take precedence, so an
existing setup does not have to change.

### Key reference

| Key | Default | Meaning |
|---|---|---|
| `homeassistant.url` | – | Base URL of your Home Assistant instance |
| `homeassistant.token` | – | Access token; stored in `credentials.yaml` only |
| `export.output_dir` | `~/ha-history-exports` | Receives exports, metadata, and logs |
| `export.timezone` | `Europe/Berlin` | IANA time zone defining local days |
| `export.formats` | `[jsonl]` | Any of `jsonl`, `csv`, `parquet`; empty for snapshot only |
| `requests.batch_size` | `15` | Entities per history request |
| `requests.sleep_between_requests` | `0.7` | Seconds between requests |
| `requests.sleep_between_days` | `5.0` | Seconds between days |
| `requests.timeout` | `120` | HTTP timeout per request, in seconds |
| `requests.max_retries` | `3` | Retries on timeouts and 5xx |
| `requests.backoff` | `[2, 5, 15]` | Seconds to wait between retries |
| `history_request.minimal_response` | `false` | Ask HA for a reduced payload |
| `history_request.no_attributes` | `false` | Ask HA to omit attributes |
| `history_request.significant_changes_only` | `false` | Ask HA for significant changes only |
| `history_request.skip_initial_state` | `true` | Omit the synthetic state at the window start |
| `entities.include_unknown` | `true` | Request history for `unknown` entities |
| `entities.include_unavailable` | `true` | Request history for `unavailable` entities |
| `entities.exclude_domains` | `[]` | Entity domains to skip |
| `entities.exclude_patterns` | `[]` | Glob patterns of entity IDs to skip |
| `recorder.purge_keep_days` | unset | Block new exports at the known Recorder boundary |
| `storage.temp_dir` | platform temp | Working directory during a run |
| `storage.locked_file_retries` | `5` | Retries when another program locks an output file |
| `storage.locked_file_retry_sleep` | `2.0` | Seconds between those retries |

Every key name matches the command-line option that overrides it:
`--batch-size` sets `requests.batch_size`, `--timeout` sets `requests.timeout`.

Larger batches finish sooner but put more load on Home Assistant. The
defaults are the sizing proven over months of daily exports on an instance with
roughly 1400 entities. If your Home Assistant runs on modest hardware and feels
sluggish during an export, lower `requests.batch_size` and raise
`requests.sleep_between_requests`.

The default profile sends `significant_changes_only=0` explicitly and requests
every Recorder state row strictly inside the day. `skip_initial_state=true`
omits Home Assistant's synthetic carry-in state at the exact start boundary;
set it to `false` only when that context row is useful to you.

`minimal_response`, `no_attributes`, and `significant_changes_only` all request
less information. Minimal responses omit attribute snapshots and other fields
from reduced follow-up rows and may remove repeated equal states. HHE restores
a consistent row structure and entity identity, but it never invents omitted
values: unavailable attributes are stored as `null`. The selected fidelity is
recorded in every schema 1.4 manifest.

## Credentials and security

- Every request is a read-only `GET`. HHE never writes to Home Assistant.
- The token is sent in the `Authorization` header only — never in a URL, a log
  line, a manifest, an error message, or an export file. It is not part of the
  configuration object at all, so it cannot reach a log through one.
- `hhe config get`/`list` report a stored token as `<set>`, never its value.
- The token lives in `credentials.yaml`, separate from `config.yaml`, created
  with owner-only permissions (`0600` on Linux and macOS; on Windows it
  inherits the user-profile ACL). `hhe doctor` checks and reports this. A token
  written into `config.yaml` by hand is refused with an explanation.
- `HHE_TOKEN` (or `HA_TOKEN`) in the environment takes precedence over the
  stored token, which keeps CI and one-off shells free of any file.
- A token is never accepted as a command-line value. Use the hidden prompt with
  `hhe config set homeassistant.token`, or pipe an intentional stdin source with
  `Get-Content <token-file> | hhe config set homeassistant.token --stdin` on
  PowerShell.
- Failed API responses are reported by status code and endpoint only; response
  bodies are never logged or stored, because they can contain private data.

## Command reference

```
hhe [--version] [--help]
hhe export  [selection] [output] [tuning]
hhe init    [--force] [--non-interactive --url … --output-dir … --format … --timezone …]
hhe config  get KEY | set KEY [VALUE] [--stdin] | unset KEY | list [--origin] | path | edit
hhe doctor  [--offline] [--config FILE]
```

Every invocation names its command; there is no implicit one.

### `hhe export`

At most one day selection. Without one, the most recent complete day is
exported — which is what a daily run wants:

| Option | Meaning |
|---|---|
| *(none)* | The most recent complete day |
| `--last-days N` | The N most recent complete days, ending yesterday |
| `--date DATE` | `YYYY-MM-DD`, `yesterday`, or `today` |
| `--start-date DATE --end-date DATE` | An inclusive range |

`yesterday` and `today` work in all three date options, so
`--start-date 2026-08-01 --end-date yesterday` is a valid range.

| Option | Meaning |
|---|---|
| `--format LIST` | `jsonl`, `csv`, `parquet`, `none`, comma-separated; states the complete set |
| `--output-dir DIR` | Output directory for this run |
| `--dry-run` | Show the plan and the entity count; fetch no history |
| `--force` | Re-export days that already have a successful manifest |
| `--config FILE` | Use exactly this configuration file |
| `--timezone TZ`, `--batch-size N`, `--sleep-between-requests S`, `--sleep-between-days S`, `--timeout S`, `--max-retries N` | Override the matching setting |
| `--log-level LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

`--format none` runs in snapshot-only mode: HHE records the current entity list
and states, makes no History API request, and writes no daily manifest.

### Exporting today

Today is skipped by default, because it is not over. To capture it anyway —
useful while debugging, or to look at what happened this morning — name it:

```bash
hhe export --date today
```

The day is fetched from local midnight up to the moment of the run and its
manifest is recorded as `status: partial`. Because only `status: ok` counts as
exported, the next run that includes that day fetches it again in full and
replaces the files. Nothing else reaches into an unfinished day: a range or
`--last-days` that happens to span today still skips it.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Runtime failure — authentication, connectivity, a failed day |
| `2` | Usage or configuration problem |
| `130` | Interrupted with Ctrl-C |

## Output

```
<output_dir>/
  exports/daily/2026/06/
    2026-06-15.jsonl            one JSON object per state change
    2026-06-15.parquet          typed, compressed, ~15x smaller than JSONL
    2026-06-15.csv              optional, flat
    2026-06-15.manifest.json    what the run did
  metadata/
    entity_snapshot_<time>_<run>.json   the entity list at export time
    export_runs.jsonl                   one line per exported day
  logs/
    ha_history_export_<time>.log
```

Each JSONL line holds one validated canonical history record. Home Assistant
timestamps are normalized to UTC, missing fields are represented explicitly,
unknown top-level API fields are retained, and HHE adds `local_offset`:

```json
{"entity_id": "sensor.kitchen_temperature", "state": "21.5",
 "last_changed": "2026-06-15T08:30:00+00:00",
 "last_updated": "2026-06-15T08:30:00+00:00",
 "attributes": {"unit_of_measurement": "°C"},
 "local_offset": "+02:00"}
```

`local_offset` is computed per row from the event time in `last_updated`, so on
a daylight-saving transition day the rows before and after the change carry
the correct offsets even when `last_changed` is older.

CSV stores attributes in `attributes_json`; Parquet uses required
`timestamp[us, UTC]` columns and the same JSON representation. Both formats
preserve unknown top-level API fields in `extra_json`, and all enabled formats
must produce the same logical digest before any file is published.

```sql
SELECT entity_id, state, last_changed
FROM read_parquet('~/ha-history-exports/exports/daily/**/*.parquet')
WHERE entity_id = 'sensor.kitchen_temperature'
ORDER BY last_changed;
```

### Manifests and resuming

Every exported day gets a schema 1.4 manifest recording status, timing, entity
and row counts, request options, capture fidelity, Retention assessment, safe
failure codes, and per-artifact filename, byte size, SHA-256, row count, and
logical digest. `status` is one of:

| Status | Meaning |
|---|---|
| `ok` | The day was captured completely |
| `partial` | The day was captured while it was still running |
| `failed` | The run reached the day but could not finish it |
| `pending` | Incomplete in-memory or legacy state; never a successful day |

A manifest with `status: ok` is the single durable record that the day was
captured: you may move, archive, or delete the exported files afterwards, and
HHE will still skip that day instead of asking Home Assistant again. Only
`--force` overrides that. Every other status means the day is not done, so a
later run exports it again.

Legacy schema 1.1–1.3 manifests remain readable and an existing legacy
`status: ok` remains a completed day. Malformed, unknown, or contradictory
manifests fail closed and are not overwritten, including with `--force`.

### One run at a time

A run holds a lock on its output directory and works in its own temporary
directory, so two runs cannot interfere. All requested formats are written and
validated first. A crash-recoverable day transaction then publishes the
artifacts and the manifest last; a failed force-export restores the previous
generation byte-for-byte.

## Troubleshooting

Run `hhe doctor` first — it checks configuration, credentials, directories,
formats, and connectivity, and prints the command that fixes each problem.

**`No Home Assistant URL configured.` / `No Home Assistant access token configured.`**
Nothing is set up yet. Run `hhe init`, or set the values directly with
`hhe config set homeassistant.url …` and `hhe config set homeassistant.token`.

**`Authentication failed: Home Assistant rejected the access token (HTTP 401).`**
The token is wrong, expired, or was revoked. Create a new one under Profile →
Security → Long-Lived Access Tokens and store it with
`hhe config set homeassistant.token`.

**`Cannot reach Home Assistant at …`**
The address is not reachable from this machine, or Home Assistant did not
answer in time. Check the URL with `hhe config get homeassistant.url`, try it in
a browser, and raise `--timeout` if the instance is simply slow.

**`Parquet output is enabled but pyarrow is not installed.`**
Run `pipx inject ha-history-exporter pyarrow`, or drop Parquet from the format
list with `hhe config set export.formats jsonl`.

**`Unknown configuration key '…'`**
A typo in the configuration file. The message names the file, the line, and the
closest valid key; `hhe config list` shows all of them.

**`Another export is already running for this output directory.`**
Wait for the other run. If none is running, the message names the lock file to
remove.

**`Recorder retention blocks one or more requested days.`**
Set `recorder.purge_keep_days` to the value used by your Home Assistant
Recorder. HHE refuses a new export when a requested local day touches or
exceeds that known boundary; `--force` does not bypass it. Existing successful
legacy manifests are still skipped without being reclassified.

**`Empty history ... cannot be verified without Recorder retention.`**
With unknown retention an empty response could mean either a genuinely quiet
day or purged data, so HHE publishes neither daily artifacts nor `status: ok`.
Within a configured safe retention window, an empty day is valid.

## Automating daily exports

A daily run of `hhe export --last-days 2` keeps the archive complete even if a
day is missed: the extra day is skipped when it is already there.

**Linux and macOS** — `crontab -e`:

```cron
30 3 * * * /home/you/.local/bin/hhe export --last-days 2
```

**Windows** — Task Scheduler, daily, action:

```
Program:   %USERPROFILE%\.local\bin\hhe.exe
Arguments: export --last-days 2
```

The stored `credentials.yaml` means the scheduled task needs no environment
variables and no token in a script file. An export run never asks a question,
so it cannot block waiting for an answer nobody is there to give.

## Development

```bash
python -m pip install -r requirements-dev.txt
python -m pytest --cov=ha_history_exporter --cov-report=term-missing
python -m ruff check .
python -m mypy
python -m build
```

The test suite is fully isolated: network sockets are disabled during pytest
runs, `HA_URL`/`HA_TOKEN` are removed before every test, the user configuration
directory and the working directory are redirected into temporary paths, and
Home Assistant is always represented by an in-memory substitute. The suite
cannot reach a real instance. The same suite runs on Linux and Windows against
Python 3.10 and 3.13 in GitHub Actions, together with lint, type check, and a
package build.

What an export produces is pinned byte-for-byte against golden fixtures in
`tests/golden/`. If a change alters them, that is the change asking to be
looked at: review the diff, then regenerate with `python tools/refresh_golden.py`.

## License

MIT — see [LICENSE](LICENSE).
