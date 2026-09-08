# Home Assistant History Exporter

Keep your own copy of your Home Assistant history.

HHE is a command-line tool that reads raw Recorder state changes through the
official Home Assistant REST API and writes them to your disk, one complete
local day at a time, as JSONL, Parquet, or CSV. Everything it does with Home
Assistant is read-only.

```console
$ pipx install ha-history-exporter
$ hhe init
$ hhe export --last-days 7
```

## Contents

- [Why](#why)
- [Install](#install)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Credentials and security](#credentials-and-security)
- [Command reference](#command-reference)
- [Output](#output)
- [Troubleshooting](#troubleshooting)
- [Automating daily exports](#automating-daily-exports)
- [Development](#development)
- [Roadmap](#roadmap)
- [License](#license)

## Why

The Home Assistant Recorder keeps a limited window of history — often two or
three weeks — and then purges it. Long-term statistics survive, but they are
hourly aggregates, not the raw state changes.

HHE exports the raw changes before they are purged, into an archive that
outlives your Home Assistant database and can be analysed with DuckDB, Pandas,
or Polars. It needs no add-on, no database access, and no change to your Home
Assistant configuration.

**What is exported**

- every raw Recorder state change of every entity currently in `/api/states`
- entities whose current state is `unknown` or `unavailable` (they may still
  have history)
- complete local calendar days, including the 23-hour and 25-hour days at
  daylight-saving transitions

**What is not exported**

- long-term statistics — the History API does not serve them
- entities that no longer exist in Home Assistant
- today, because the day is not over yet

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

Requires Python 3.10 or newer. Linux, macOS, and Windows are supported and
tested.

### From a clone

```bash
git clone https://github.com/hyperfelixations/ha-history-exporter.git
cd ha-history-exporter
pip install -r requirements.txt
python ha_history_batch_export.py --last-days 7
```

`ha_history_batch_export.py` is the historical entry point and takes exactly
the same arguments as `hhe export`.

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

Next:  hhe export --last-days 7
```

Create the token in Home Assistant under **Profile → Security → Long-Lived
Access Tokens**. Then export:

```bash
hhe export --last-days 7          # the seven most recent complete days
hhe export --date yesterday       # a single day
hhe export --date 2026-06-15 --force
hhe export --start-date 2026-06-01 --end-date 2026-06-15
```

Days that already have a successful manifest are skipped, so running the same
command twice costs nothing and re-running a range only fetches what is
missing.

`hhe doctor` checks everything at once and prints the command that fixes each
problem it finds.

## Configuration

Nothing has to be configured: with `HA_URL` and `HA_TOKEN` in the environment,
`hhe export --last-days 7` already works and writes to
`<your home directory>/ha-history-exports`.

### Where settings come from

Sources are merged, highest precedence first:

| # | Source | Example |
|---|---|---|
| 1 | command-line options | `--batch-size 15` |
| 2 | environment variables | `HHE_REQUESTS_BATCH_SIZE_ENTITIES=15` |
| 3 | the file named by `--config` | `hhe export --config ./my.yaml …` |
| 4 | a file in the working directory | `ha-history-exporter.yaml`, or `export_config.yaml` |
| 5 | your user configuration | see `hhe config path` |
| 6 | built-in defaults | |

`--config` replaces discovery entirely: 4 and 5 are then not read at all.

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
hhe config set formats.parquet true
hhe config set export.output_dir /mnt/archive/ha-history
hhe config set requests.batch_size_entities 15
hhe config get formats.parquet
hhe config unset formats.parquet      # back to the default
hhe config edit                       # open the file in $EDITOR
```

Writes always go to your user configuration, never to a project file or a file
you passed with `--config`. The file lists every key with its documentation;
keys you have not set appear commented out, showing the built-in default.

### Environment variables

Every key has one: `HHE_` followed by the key path in upper case with dots
replaced by underscores, for example `HHE_EXPORT_OUTPUT_DIR`,
`HHE_FORMATS_PARQUET`, `HHE_REQUESTS_BATCH_SIZE_ENTITIES`. `HA_URL` and
`HA_TOKEN` keep their historical names.

### Key reference

| Key | Default | Meaning |
|---|---|---|
| `homeassistant.url` | – | Base URL of your Home Assistant instance |
| `homeassistant.token` | – | Access token; stored in `credentials.yaml` only |
| `home_assistant.url_env` | `HA_URL` | Environment variable holding the URL |
| `home_assistant.token_env` | `HA_TOKEN` | Environment variable holding the token |
| `home_assistant.timezone` | `Europe/Berlin` | IANA time zone defining local days |
| `export.output_dir` | `~/ha-history-exports` | Receives exports, metadata, and logs |
| `export.resume` | `true` | Skip days with a successful manifest |
| `export.force` | `false` | Re-export such days anyway |
| `requests.batch_size_entities` | `5` | Entities per history request |
| `requests.sleep_between_requests_seconds` | `1.0` | Pause between requests |
| `requests.sleep_between_days_seconds` | `5.0` | Pause between days |
| `requests.request_timeout_seconds` | `120` | HTTP timeout per request |
| `requests.max_retries` | `3` | Retries on timeouts and 5xx |
| `requests.backoff_seconds` | `[2, 5, 15]` | Wait times between retries |
| `formats.jsonl` | `true` | Write JSONL |
| `formats.csv` | `false` | Write CSV |
| `formats.parquet` | `false` | Write Parquet (needs pyarrow) |
| `storage.temp_dir` | platform temp | Working directory during a run |
| `storage.cloud_storage_retry_count` | `5` | Retries when a sync client locks a file |
| `storage.cloud_storage_retry_sleep_seconds` | `2.0` | Pause between those retries |
| `history_request.minimal_response` | `false` | Ask HA for a reduced payload |
| `history_request.no_attributes` | `false` | Ask HA to omit attributes |
| `history_request.significant_changes_only` | `false` | Ask HA for significant changes only |
| `recorder.expected_purge_keep_days` | unset | Warn about days older than this |
| `entity_selection.include_unknown` | `true` | Request history for `unknown` entities |
| `entity_selection.include_unavailable` | `true` | Request history for `unavailable` entities |
| `entity_selection.optional_exclude_patterns` | `[]` | Glob patterns of entity IDs to skip |
| `entity_selection.optional_exclude_domains` | `[]` | Entity domains to skip |

Larger batches finish sooner but put more load on Home Assistant. On a
Raspberry Pi, `batch_size_entities: 15` with
`sleep_between_requests_seconds: 0.7` is a good compromise.

## Credentials and security

- Every request is a read-only `GET`. HHE never writes to Home Assistant.
- The token is sent in the `Authorization` header only — never in a URL, a log
  line, a manifest, an error message, or an export file.
- `hhe config get`/`list` report a stored token as `<set>`, never its value.
- The token lives in `credentials.yaml`, separate from `config.yaml`, created
  with owner-only permissions (`0600` on Linux and macOS; on Windows it
  inherits the user-profile ACL). `hhe doctor` checks and reports this.
- `HA_TOKEN` in the environment takes precedence over the stored token, which
  keeps CI and one-off shells free of any file.
- Failed API responses are reported by status code and endpoint only; response
  bodies are never logged or stored, because they can contain private data.

## Command reference

```
hhe [--version] [--help]
hhe export  [selection] [output] [tuning]
hhe init    [--force] [--non-interactive --url … --output-dir … --format … --timezone …]
hhe config  get KEY | set KEY [VALUE] | unset KEY | list [--origin] | path | edit
hhe doctor  [--offline] [--config FILE]
```

The command may be omitted for `export`, so every historical invocation such as
`ha-history-exporter --date yesterday` keeps working unchanged.

### `hhe export`

Exactly one day selection is required:

| Option | Meaning |
|---|---|
| `--last-days N` | The N most recent complete days, ending yesterday |
| `--date DATE` | `YYYY-MM-DD`, `yesterday`, or `today` |
| `--start-date DATE --end-date DATE` | An inclusive range |

| Option | Meaning |
|---|---|
| `--format LIST` | `jsonl`, `csv`, `parquet`, `none`, comma-separated; states the complete set |
| `--outdir DIR` | Output directory for this run |
| `--dry-run` | Show the plan and the entity count; fetch no history |
| `--force` | Re-export days that already have a successful manifest |
| `--config FILE` | Use exactly this configuration file |
| `--timezone TZ`, `--batch-size N`, `--sleep-between-requests S`, `--sleep-between-days S`, `--timeout S`, `--max-retries N` | Override the matching setting |
| `--log-level LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

`--jsonl`, `--parquet`, and `--no-csv` still work and are kept for
compatibility; `--format` supersedes them and cannot be combined with them.

`--format none` runs in snapshot-only mode: HHE records the current entity list
and states, makes no History API request, and writes no daily manifest.

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

Each JSONL line holds the state object as Home Assistant returned it, plus one
added field:

```json
{"entity_id": "sensor.kitchen_temperature", "state": "21.5",
 "last_changed": "2026-06-15T08:30:00+00:00",
 "last_updated": "2026-06-15T08:30:00+00:00",
 "attributes": {"unit_of_measurement": "\u00b0C"},
 "local_offset": "+02:00"}
```

Timestamps are UTC, exactly as the API returns them. `local_offset` is computed
per row, so on a daylight-saving transition day the rows before and after the
change carry different offsets and local time can be reconstructed without
consulting anything else.

Parquet stores the same records with real `timestamp[us, UTC]` columns and
attributes as a JSON string, which is what DuckDB and Pandas want:

```sql
SELECT entity_id, state, last_changed
FROM read_parquet('~/ha-history-exports/exports/daily/**/*.parquet')
WHERE entity_id = 'sensor.kitchen_temperature'
ORDER BY last_changed;
```

### Manifests and resuming

Every exported day gets a manifest recording status, timing, entity counts,
row counts, request counts, and any failed batches. A manifest with
`status: ok` is the single durable record that the day was captured: you may
move, archive, or delete the exported files afterwards, and HHE will still skip
that day instead of asking Home Assistant again. Only `--force` overrides that.

### One run at a time

A run holds a lock on its output directory and works in its own temporary
directory, so two runs cannot interfere. Files become visible only after they
have been written completely and validated.

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
Run `pipx inject ha-history-exporter pyarrow`, or turn Parquet off with
`hhe config set formats.parquet false`.

**`Unknown configuration key '…'`**
A typo in a configuration file. The message names the file, the line, and the
closest valid key; `hhe config list` shows all of them.

**`Another export is already running for this output directory.`**
Wait for the other run. If none is running, the message names the lock file to
remove.

**A day exports as empty**
The Recorder has already purged it. Set `recorder.expected_purge_keep_days` to
your Home Assistant `purge_keep_days` value and HHE will warn before fetching.

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
variables and no token in a script file.

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

## Roadmap

- deriving a missing format from files already on disk, without asking Home
  Assistant again
- an extended manifest schema recording each artifact separately
- a separate viewer for browsing exported history

## License

MIT — see [LICENSE](LICENSE).
