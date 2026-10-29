# Home Assistant History Exporter

Keep your own copy of your Home Assistant history.

HHE is a command-line tool that reads the raw Recorder state changes through
the Home Assistant REST API and writes them to your disk, one complete local
day at a time, as JSONL, CSV or Parquet. It only reads from Home Assistant.

```console
$ pipx install ha-history-exporter
$ hhe init
$ hhe export
```

## Contents

- [Features](#features)
- [What you need](#what-you-need)
- [Installation](#installation)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [Commands](#commands)
- [Output](#output)
- [Exit codes](#exit-codes)
- [Automating daily exports](#automating-daily-exports)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Security](#security)
- [Links](#links)

## Features

- Exports every raw Recorder state change of every entity in `/api/states`,
  including entities that are currently `unknown` or `unavailable`.
- Writes complete local calendar days, including the 23-hour and 25-hour days
  of daylight-saving changes; every row carries its local UTC offset.
- Writes JSONL, CSV and Parquet with identical content, proven by a logical
  digest before a day is published.
- Records every day in a manifest with size, SHA-256, row count and logical
  digest of each file; a day with a successful manifest is skipped on the next
  run.
- Publishes each day completely or not at all, also after a crash or power
  loss, and lets one run at a time write an archive.
- Checks a stored archive against its manifests (`hhe verify`), shows its gaps
  (`hhe status`) and adds formats to stored days without asking Home Assistant
  again (`hhe convert`).
- Prints machine-readable results with `--json` and distinct exit codes for
  automation.
- Runs on your machine and talks to your Home Assistant only: no cloud
  service, no add-on, no change to your Home Assistant configuration.

## What you need

- Python 3.12 or newer on Linux, macOS or Windows.
- A Home Assistant instance this computer can reach, by host name, IP address
  or Tailscale address.
- A long-lived access token: in Home Assistant, **Profile → Security →
  Long-Lived Access Tokens**.
- The Recorder retention of your Home Assistant (`purge_keep_days`, 10 unless
  you changed it). HHE uses it to refuse days that may already be purged.

## Installation

[pipx](https://pipx.pypa.io/) installs HHE into an environment of its own and
puts the commands on your `PATH`:

```bash
pipx install ha-history-exporter
```

This installs `hhe` and its long name `ha-history-exporter`;
`python -m ha_history_exporter` runs the same program.

Parquet output needs [pyarrow](https://arrow.apache.org/docs/python/), which is
optional:

```bash
pipx inject ha-history-exporter pyarrow
```

In a virtual environment of your own, install the extra:
`pip install "ha-history-exporter[parquet]"`.

## Quickstart

```console
$ hhe init
Home Assistant URL [http://homeassistant.local:8123]: https://ha.example.net
Long-lived access token (input hidden): ********
  -> Contacting https://ha.example.net ...
     OK: Home Assistant 2026.10.1, 1258 entities, Europe/Berlin
Output directory [/home/you/ha-history-exports]:
Output formats (jsonl, csv, parquet, or none) [jsonl]: jsonl,parquet
Time zone of the exported days (Home Assistant's) [Europe/Berlin]:
Recorder retention of Home Assistant in days (purge_keep_days) [10]:

Wrote /home/you/.config/ha-history-exporter/config.toml
Wrote /home/you/.config/ha-history-exporter/credentials.toml (the access token, readable by your account only)

Next:  hhe export
```

Then export:

```bash
hhe export                        # the most recent complete day
hhe export --last-days 7          # the seven most recent complete days
hhe export --start-date 2026-06-01 --end-date yesterday
```

`hhe doctor` checks the installation, the configuration, the archive and the
connection, and prints the command that fixes each problem it finds.

## Configuration

With `HHE_URL` and `HHE_TOKEN` set in the environment, `hhe export` works
without a configuration file and writes to `ha-history-exports` in your home
directory.

### Sources

Settings are merged, highest precedence first:

| # | Source | Example |
|---|---|---|
| 1 | `hhe export` options | `--batch-size 10` |
| 2 | environment variables | `HHE_REQUESTS_BATCH_SIZE=10` |
| 3 | the configuration file | `config.toml`, see `hhe config path` |
| 4 | built-in defaults | |

The configuration file is [TOML](https://toml.io/). `--config FILE` reads
exactly that file for one command, in place of your configuration file.

Paths may use `~`, `$NAME` and `${NAME}`, on Windows also `%NAME%`; naming an
undefined variable is an error. A relative path in the file is relative to the
file; a relative path in a variable or an option is relative to the working
directory.

`hhe config list --origin` shows every effective value and where it comes from.

### Files

| | Linux | macOS | Windows |
|---|---|---|---|
| configuration | `~/.config/ha-history-exporter/config.toml` | `~/Library/Application Support/ha-history-exporter/config.toml` | `%APPDATA%\ha-history-exporter\config.toml` |
| access token | `credentials.toml` in the same directory | same | same |
| archive | `~/ha-history-exports` | same | `%USERPROFILE%\ha-history-exports` |

`hhe config path` prints the paths on your machine. `HHE_CONFIG_DIR` names
another configuration directory.

### Changing settings

```bash
hhe config set export.formats jsonl,parquet
hhe config set export.output_dir /mnt/archive/ha-history
hhe config get export.formats
hhe config unset export.formats       # back to the default
hhe config set homeassistant.token    # asks for the token, hidden
hhe config edit                       # opens the file in your editor
```

`hhe config set` checks the value before it writes the file, and `hhe config
edit` checks the file after the editor closes. `config edit` uses `VISUAL` or
`EDITOR`; without them, Windows opens the file with its default application. The file lists every key with
its documentation; keys you have not set appear as comments with their default
or an example:

```toml
[export]
output_dir = "/mnt/archive/ha-history"
formats = ["jsonl", "parquet"]

[requests]
batch_size = 10
```

### Keys

Every key has an environment variable; the URL and the token have the short
names `HHE_URL` and `HHE_TOKEN`. HHE reports a variable that starts with
`HHE_` but names no key.

<!-- keys:start -->
| Key | Default | Environment variable | `hhe export` option | Meaning |
|---|---|---|---|---|
| `homeassistant.url` | not set | `HHE_URL` |  | Home Assistant base URL. |
| `homeassistant.token` | not set | `HHE_TOKEN` |  | Long-lived access token of Home Assistant; kept in `credentials.toml`. |
| `homeassistant.ca_bundle` | not set | `HHE_HOMEASSISTANT_CA_BUNDLE` |  | PEM file with the certificate authority that signed the HTTPS certificate of Home Assistant; unset uses the system's trusted authorities. |
| `export.output_dir` | `ha-history-exports` in your home directory | `HHE_EXPORT_OUTPUT_DIR` | `--output-dir` | Directory that receives exports, metadata and logs. |
| `export.timezone` | not set | `HHE_EXPORT_TIMEZONE` | `--timezone` | IANA time zone of the calendar days; unset takes the archive's time zone, and for a new archive the time zone of Home Assistant. |
| `export.formats` | `["jsonl"]` | `HHE_EXPORT_FORMATS` | `--format` | Output formats: any of jsonl, csv, parquet; [] captures the entity snapshot only. |
| `requests.batch_size` | `15` | `HHE_REQUESTS_BATCH_SIZE` | `--batch-size` | Entities per history request. |
| `requests.sleep_between_requests` | `0.7` | `HHE_REQUESTS_SLEEP_BETWEEN_REQUESTS` | `--sleep-between-requests` | Pause between history requests, in seconds. |
| `requests.sleep_between_days` | `5` | `HHE_REQUESTS_SLEEP_BETWEEN_DAYS` | `--sleep-between-days` | Pause between exported days, in seconds. |
| `requests.timeout` | `120` | `HHE_REQUESTS_TIMEOUT` | `--timeout` | HTTP read timeout per request, in seconds. |
| `requests.max_retries` | `3` | `HHE_REQUESTS_MAX_RETRIES` | `--max-retries` | Retries of a request after a timeout, a lost connection, HTTP 429, 500 or 502-504. |
| `requests.backoff` | `[2, 5, 15]` | `HHE_REQUESTS_BACKOFF` |  | Wait times between retries, in seconds; the last one repeats. |
| `history_request.minimal_response` | `false` | `HHE_HISTORY_REQUEST_MINIMAL_RESPONSE` |  | Ask Home Assistant for a reduced history payload. |
| `history_request.no_attributes` | `false` | `HHE_HISTORY_REQUEST_NO_ATTRIBUTES` |  | Ask Home Assistant to omit entity attributes. |
| `history_request.significant_changes_only` | `false` | `HHE_HISTORY_REQUEST_SIGNIFICANT_CHANGES_ONLY` |  | Ask Home Assistant for significant changes only. |
| `history_request.skip_initial_state` | `true` | `HHE_HISTORY_REQUEST_SKIP_INITIAL_STATE` |  | Omit the state in effect at midnight that opens each entity's day. |
| `entities.include_unknown` | `true` | `HHE_ENTITIES_INCLUDE_UNKNOWN` |  | Request history for entities currently in state unknown. |
| `entities.include_unavailable` | `true` | `HHE_ENTITIES_INCLUDE_UNAVAILABLE` |  | Request history for entities currently in state unavailable. |
| `entities.exclude_domains` | `[]` | `HHE_ENTITIES_EXCLUDE_DOMAINS` |  | Entity domains to exclude, for example update or button. |
| `entities.exclude_patterns` | `[]` | `HHE_ENTITIES_EXCLUDE_PATTERNS` |  | Case-sensitive glob patterns of entity IDs to exclude, for example sensor.*_debug. |
| `recorder.purge_keep_days` | not set | `HHE_RECORDER_PURGE_KEEP_DAYS` |  | Recorder retention of Home Assistant in days (purge_keep_days, 10 unless changed there); unset means unknown, and an empty day then fails. |
| `storage.temp_dir` | `ha-history-exporter` in the system's temporary directory | `HHE_STORAGE_TEMP_DIR` |  | Directory for the working files of a run. |
| `storage.locked_file_retries` | `5` | `HHE_STORAGE_LOCKED_FILE_RETRIES` |  | Retries while another program locks an output file. |
| `storage.locked_file_retry_sleep` | `2` | `HHE_STORAGE_LOCKED_FILE_RETRY_SLEEP` |  | Pause between file-lock retries, in seconds. |
<!-- keys:end -->

### Request load

`requests.batch_size` entities are asked for per request, with
`requests.sleep_between_requests` seconds between requests and
`requests.sleep_between_days` between days. The defaults suit an instance with
about 1400 entities. If Home Assistant feels slow during an export, lower the
batch size and raise the pauses.

### Capture options

By default, every Recorder row of a day is requested with its attributes, from
local midnight up to the next local midnight; a change at exactly midnight
belongs to the day it opens.

- `history_request.skip_initial_state = false` adds the state in effect at
  midnight as the first row of each entity, stamped at midnight.
- `minimal_response`, `no_attributes` and `significant_changes_only` ask Home
  Assistant for less. Values Home Assistant omits are stored as `null`. Every
  manifest records the options and the resulting fidelity.

## Commands

```
hhe export   [day selection] [--dry-run] [--force] [setting options] [--json]
hhe convert  --format LIST [day selection] [--output-dir DIR] [--json]
hhe verify   [day selection] [--deep] [--output-dir DIR] [--json]
hhe status   [day selection] [--output-dir DIR] [--json]
hhe init     [--non-interactive] [--force] [--url URL] [--output-dir DIR] [--format LIST]
             [--timezone ZONE] [--purge-keep-days N]
hhe config   get KEY | set KEY [VALUE] [--stdin] | unset KEY | list [--origin] | path | edit
hhe doctor   [--offline] [--json]
```

Every command accepts `--config FILE`; `export` and `convert` accept
`--log-level DEBUG|INFO|WARNING|ERROR`.

### Day selection

| Option | Days |
|---|---|
| `--date DAY` | one day: `YYYY-MM-DD`, `yesterday` or `today` |
| `--start-date DAY --end-date DAY` | an inclusive range |
| `--last-days N` | the N most recent complete days, ending yesterday |

Without a selection, `export` takes the most recent complete day; `convert`,
`verify` and `status` take every day of the archive.

### `hhe export`

Fetches the selected days from Home Assistant and publishes them. A day with a
successful manifest is skipped; `--force` exports it again.

| Option | Effect |
|---|---|
| `--dry-run` | shows the plan with the entity count; fetches no history and writes nothing |
| `--force` | exports days again that have a successful manifest |
| `--format LIST` | the complete set of formats for this run: `jsonl`, `csv`, `parquet`, or `none` |
| `--output-dir DIR`, `--timezone ZONE`, `--batch-size N`, `--sleep-between-requests S`, `--sleep-between-days S`, `--timeout N`, `--max-retries N` | override the setting of the same name |

`--format none` records the current entity list and states only: no history
request, no day manifest.

`--date today` exports the running day up to the moment of the run, recorded
as `partial`. The next run that includes the day exports it completely. A
range or `--last-days` covers complete days only.

### `hhe convert`

Adds formats to stored days from a file they already have, without Home
Assistant:

```bash
hhe convert --format parquet
hhe convert --format csv --last-days 30
```

A day is converted when its manifest (schema 1.4) says `ok`. The source file
is compared with its manifest first; the new files hold exactly the same
records, and the manifest gains them. A day whose source is missing or
changed, and a day with a manifest of schema 1.1 to 1.3, which records no
checksums, is reported as `blocked`.

### `hhe verify`

Compares every stored file with its manifest: size and SHA-256, and with
`--deep` also every record against the row count and logical digest. It
reports each file as:

| Result | Meaning |
|---|---|
| `ok` | the file matches its manifest |
| `present` | the file exists; its manifest (schema 1.1 to 1.3) records no checksum |
| `missing` | the manifest declares the file, but it is not there |
| `mismatch` | size, SHA-256, row count or logical digest differ |
| `unreadable` | the file cannot be read or decoded |
| `unexpected` | a day file that no manifest declares |

`verify` exits with `1` for `mismatch`, `unreadable`, `unexpected` and invalid
manifests. A missing file is reported only, so you can move stored days to
other storage.

### `hhe status`

Lists the days of the archive month by month as `ok`, `partial`, `failed`,
`pending`, `invalid` or `missing`. Without a selection, the list runs from the
first to the last exported day.

### `hhe init`

Asks for the URL, the token, the output directory, the formats, the time zone
and the Recorder retention, checks them against Home Assistant and writes
`config.toml` and `credentials.toml` together. It offers the time zone of Home
Assistant.

- `--non-interactive` asks nothing: the URL comes from `--url` or `HHE_URL`,
  the token from `HHE_TOKEN` or standard input.
- `--force` replaces an existing configuration.

### `hhe config`

Reads and changes the configuration file; see
[Changing settings](#changing-settings). `get` and `list` show a stored token as
`<set>`. The token is read by a hidden prompt or, with `--stdin`, from standard
input:

```bash
hhe config set homeassistant.token --stdin < token.txt
```

In PowerShell: `Get-Content token.txt | hhe config set homeassistant.token --stdin`.

### `hhe doctor`

Checks the installation, the configuration and its file permissions, the
directories and free disk space, the archive (time zone, running export,
interrupted transactions, invalid manifests, undeclared or missing day files)
and, unless `--offline`, the connection, version and time zone of Home
Assistant. It changes nothing and exits with `1` when a check fails.

### JSON output

With `--json`, `export`, `convert`, `verify`, `status` and `doctor` print one
JSON document on standard output; logs and messages stay on standard error.
Every document names its `command` and `exit_code`. A command that ends with
an error prints a document with `error.code`, `error.summary`, `error.details`
and `error.remedies`.

```bash
hhe verify --json | jq '.summary'
```

## Output

```
<output_dir>/
  hhe-archive.json                      the archive's time zone
  exports/daily/2026/06/
    2026-06-15.jsonl                    one JSON object per state change
    2026-06-15.csv
    2026-06-15.parquet
    2026-06-15.manifest.json            the record of the day
  metadata/
    entity_snapshot_<local time>_<run id>.json   the entities and states at export time
    export_runs.jsonl                            one line per exported day
  logs/
    ha_history_export_<local time>.log
```

New directories are readable by your account only on Linux and macOS.

### Formats

JSONL holds one object per state change, timestamps in UTC:

```json
{"entity_id": "sensor.kitchen_temperature", "state": "21.5",
 "last_changed": "2026-06-15T08:30:00+00:00",
 "last_updated": "2026-06-15T08:30:00+00:00",
 "attributes": {"unit_of_measurement": "°C"},
 "local_offset": "+02:00"}
```

- `local_offset` is the UTC offset of `last_updated` in the archive's time
  zone, so the rows of a daylight-saving day carry both offsets.
- Fields of the API that HHE does not know stay in the row, after
  `attributes`.
- CSV has the columns `entity_id, state, last_changed, last_updated,
  attributes_json, local_offset, extra_json`, with CRLF line endings.
- Parquet has the same columns with `timestamp[us, UTC]` timestamps, Snappy
  compression and row groups of 200 000 rows.

```sql
SELECT entity_id, state, last_changed
FROM read_parquet('~/ha-history-exports/exports/daily/**/*.parquet')
WHERE entity_id = 'sensor.kitchen_temperature'
ORDER BY last_changed;
```

### Manifests

Each day's `.manifest.json` records its boundaries, timing, entity and row
counts, request options, capture fidelity, Recorder retention, and filename,
size, SHA-256, row count and logical digest of every file. The
[JSON Schema of manifest 1.4](https://github.com/hyperfelixations/ha-history-exporter/blob/main/src/ha_history_exporter/schemas/manifest-1.4.schema.json)
describes it; it is installed with the package as
`ha_history_exporter/schemas/manifest-1.4.schema.json`.

| `status` | Meaning |
|---|---|
| `ok` | the complete day |
| `partial` | the running day, up to the moment of the export |
| `failed` | the last attempt failed; `error` and `error_code` say why |
| `pending` | not completed; never a successful day |

A manifest with `status: ok` marks the day as done, also after you move or
delete its files. HHE reads manifests of schema 1.1 to 1.4 and leaves a
manifest it cannot trust untouched, also with `--force`.

### Archive time zone

All days of an archive are calendar days in one time zone, recorded in
`hhe-archive.json`. A new archive takes `export.timezone`, or the time zone of
Home Assistant when it is not set. An archive without `hhe-archive.json` takes
the time zone its manifests name.

### One writer at a time

`export` and `convert` lock the archive (`.hhe-archive.lock`) for as long as
they run; the lock ends with the process, also after a crash. A day is
published as one transaction: after an interruption, the next writing command
restores the previous files of that day. `status`, `verify`, `doctor` and
`export --dry-run` read without the lock.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | a day failed, `verify` found a problem, `convert` was blocked, or another export holds the archive |
| `2` | usage or configuration problem |
| `3` | Home Assistant rejected the access token |
| `4` | Home Assistant is unreachable or unavailable, answered unusably, or the TLS check failed |
| `70` | unexpected internal error; the message names the log file that holds the traceback |
| `130` | interrupted with Ctrl-C |

When a day fails, the export continues with the next day. A rejected token, an
unavailable Home Assistant, a TLS failure or an internal error ends the run
after the running day is recorded as failed.

## Automating daily exports

`hhe export --last-days 2` once a day keeps the archive complete when a run is
missed; a day that is already exported is skipped.

**Linux and macOS**, `crontab -e`:

```cron
30 3 * * * /home/you/.local/bin/hhe export --last-days 2
```

**Windows**, Task Scheduler, a daily task with the action:

```
Program:   %USERPROFILE%\.local\bin\hhe.exe
Arguments: export --last-days 2
```

The stored `credentials.toml` gives the task its token, and an export never
asks a question.

## Troubleshooting

Run `hhe doctor` first.

**`No Home Assistant URL is configured.` / `No Home Assistant access token is configured.`**
Run `hhe init`, or `hhe config set homeassistant.url …` and
`hhe config set homeassistant.token`.

**`Home Assistant rejected the access token (HTTP 401).`**
Create a new token under Profile → Security → Long-Lived Access Tokens and store
it with `hhe config set homeassistant.token`.

**`Home Assistant is not available at …` / `Home Assistant did not answer in time.`**
Check the URL with `hhe config get homeassistant.url` and in a browser. For a
slow instance, raise `--timeout` or lower `--batch-size`.

**`The HTTPS certificate of Home Assistant could not be verified.`**
For a certificate of your own authority, set
`hhe config set homeassistant.ca_bundle <path-to-ca.pem>`.

**`… is reached over plain HTTP outside the local network …`**
The access token travels unencrypted. Use the `https://` address of your Home
Assistant.

**`export.timezone is …, but this archive's days are in …`**
Run `hhe config unset export.timezone`, or export into a new output directory.

**`Parquet output is enabled but pyarrow is not installed.`**
Run `pipx inject ha-history-exporter pyarrow`, or
`hhe config set export.formats jsonl`.

**`Unknown configuration key '…'`**
The message names the file, the line and the closest valid key.

**`Another export is already running for this output directory.`**
Wait for the run the message names.

**`Recorder retention blocks one or more requested days.`**
A requested day touches the Recorder retention boundary, so its history may
already be purged. Choose days inside `recorder.purge_keep_days`.

**`Empty history … cannot be verified without Recorder retention.`**
Set `recorder.purge_keep_days` to the retention of your Recorder. With a known
retention, an empty day is exported as an empty day.

**`verify` reports `unexpected` files**
A file of a format the day's manifest does not declare. Check it, then move or
delete it.

## Known limitations

- Long-term statistics are not exported; the History API does not serve them.
- Only entities that exist in `/api/states` at export time are exported.
- Spreadsheet applications treat a CSV cell that starts with `=`, `+`, `-` or
  `@` as a formula. Import CSV files as text, or analyse the Parquet or JSONL
  files.
- Locks on network file systems (SMB, NFS) depend on the server; keep one
  machine writing an archive.

## Security

- HHE sends only `GET` requests to Home Assistant.
- The token travels in the `Authorization` header only, never in a URL, a log,
  a manifest or an export file. Redirects are not followed.
- `credentials.toml` is readable by your account only (`0600` on Linux and
  macOS; on Windows the permissions of your profile).
- HTTPS certificates are always verified; `homeassistant.ca_bundle` adds an
  authority of your own.
- Error messages name the HTTP status and endpoint only; response bodies are
  never logged or stored.
- Exports, snapshots and logs describe what happens in your home. Log files
  list entity IDs and, after an internal error, a traceback; review a log
  before you share it.

Report a vulnerability as described in [SECURITY.md](https://github.com/hyperfelixations/ha-history-exporter/blob/main/SECURITY.md).

## Links

- [Issues](https://github.com/hyperfelixations/ha-history-exporter/issues) for
  questions, setups and ideas.

[![GitHub](https://img.shields.io/badge/GitHub-Follow-181717?logo=github&logoColor=white)](https://github.com/hyperfelixations)
[![YouTube](https://img.shields.io/badge/YouTube-Subscribe-FF0000?logo=youtube&logoColor=white)](https://www.youtube.com/@hyperfelixations)
[![Instagram](https://img.shields.io/badge/Instagram-Follow-E4405F?logo=instagram&logoColor=white)](https://www.instagram.com/hyperfelixations/)

MIT License — see [LICENSE](https://github.com/hyperfelixations/ha-history-exporter/blob/main/LICENSE).
