# Home Assistant History Exporter

[![Downloads](https://img.shields.io/pypi/dm/ha-history-exporter?label=Downloads)](https://pypi.org/project/ha-history-exporter/)
[![Discord](https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white)](https://discord.gg/zfGKCVEvwe)

Keep your own copy of your [Home Assistant](https://www.home-assistant.io/)
history.

Home Assistant keeps the detailed history of your devices for 10 days, unless
you changed that setting, and then deletes it. The History Exporter saves that
history to your computer, one day at a time, as ordinary files that stay yours
for as long as you keep them. It runs on your computer, only reads from Home
Assistant, and changes nothing there.

## Features

- **Back up your whole history:** every recorded change of every entity, with
  its attributes, including entities that are unavailable at the moment.
- **Your choice of file format:** JSONL, CSV and Parquet, one or several at
  once, with identical content.
- **Complete days:** each export covers a full calendar day in your time zone,
  including the days the clocks change.
- **Safe to repeat:** days you already saved are skipped, so you can run it
  every day, by hand or [automatically](#automating-daily-exports).
- **No half-finished days:** a day is saved completely or not at all, even
  after a crash or a power cut.
- **Checks its own work:** every file is read back before a day counts as
  saved, and `hhe verify` confirms later that your files are still intact.
- **See what you have:** `hhe status` lists your saved days and any gaps.
- **Add a format later:** `hhe convert` creates another format from the days
  you saved, without contacting Home Assistant.
- **Private:** no cloud service, no add-on and no integration. Your history
  goes from your Home Assistant to your own disk and nowhere else.

## What you need

- **A computer with Windows, macOS or Linux** that can reach your Home
  Assistant. If you can open Home Assistant in a browser on that computer, it
  can. Exports run on this computer, so it needs to be switched on for them.
- **Python 3.12 or newer and pipx.** Python runs the History Exporter, and pipx
  installs it. [Installation](#installation) covers both.
- **A long-lived access token**, a kind of password that lets the History
  Exporter read from your Home Assistant. The
  [Quickstart](#quickstart) shows you how to create one.
- **Disk space.** One exported day of a large home can reach several hundred MB
  as JSONL. Parquet needs much less.

## Installation

If you already have pipx, this is all you need:

```bash
pipx install ha-history-exporter
```

Otherwise, open the guide for your computer below. It starts from the
beginning. Run one command at a time, in the order shown.

<details>
<summary><strong>Windows: step-by-step guide</strong></summary>

1. **Open PowerShell.** Press the Windows key, type `PowerShell` and press
   Enter. A window with a blinking cursor opens. This is where you enter the
   commands below: copy a command, right-click in the window to paste it, and
   press Enter to run it.

2. **Install Python.** Go to [python.org/downloads](https://www.python.org/downloads/),
   download the **Python install manager** and open the file to install it.
   Then close PowerShell and open it again.

3. **Download the current version of Python.** If you are asked whether to add
   a folder to your `PATH`, answer yes.

   ```powershell
   py install 3
   ```

4. **Check the version.** The number you see needs to be 3.12 or higher.

   ```powershell
   py --version
   ```

5. **Install pipx.** A warning that a folder is not on `PATH` is normal here.
   The next step takes care of it.

   ```powershell
   py -m pip install --user pipx
   ```

6. **Make pipx available.**

   ```powershell
   py -m pipx ensurepath
   ```

7. **Close PowerShell and open it again**, so that it picks up the change.

8. **Install the History Exporter.**

   ```powershell
   pipx install ha-history-exporter
   ```

9. **Check that it works.** You should see the version number.

   ```powershell
   hhe --version
   ```

</details>

<details>
<summary><strong>macOS: step-by-step guide</strong></summary>

1. **Open Terminal.** Press Cmd+Space, type `Terminal` and press Enter. This is
   where you enter the commands below: paste a command with Cmd+V and press
   Enter to run it.

2. **Install Homebrew**, the usual way to install tools on a Mac. Follow the
   instructions on [brew.sh](https://brew.sh/). When you enter your password,
   nothing appears on screen. If the installer ends with a **Next steps**
   section, run the commands it lists.

3. **Install pipx.** This also installs a current version of Python.

   ```bash
   brew install pipx
   ```

4. **Make pipx available.**

   ```bash
   pipx ensurepath
   ```

5. **Close the Terminal window and open a new one**, so that it picks up the
   change.

6. **Install the History Exporter.**

   ```bash
   pipx install ha-history-exporter
   ```

7. **Check that it works.** You should see the version number.

   ```bash
   hhe --version
   ```

</details>

<details>
<summary><strong>Linux: step-by-step guide</strong></summary>

1. **Open a terminal** and check your Python version. The number you see needs
   to be 3.12 or higher. If it is lower, the History Exporter cannot run on this
   version of your system. Ubuntu 24.04 and newer meet the requirement.

   ```bash
   python3 --version
   ```

2. **Install pipx.** On Ubuntu or Debian:

   ```bash
   sudo apt update
   ```

   ```bash
   sudo apt install pipx
   ```

   On Fedora:

   ```bash
   sudo dnf install pipx
   ```

3. **Make pipx available.**

   ```bash
   pipx ensurepath
   ```

4. **Close the terminal and open a new one**, so that it picks up the change.

5. **Install the History Exporter.**

   ```bash
   pipx install ha-history-exporter
   ```

6. **Check that it works.** You should see the version number.

   ```bash
   hhe --version
   ```

</details>

### Parquet support

Parquet files need one more component. JSONL and CSV work without it.

```bash
pipx inject ha-history-exporter pyarrow
```

This component is not available for Windows on ARM processors or for 32-bit
ARM systems such as a 32-bit Raspberry Pi OS. Use JSONL or CSV there.

### Updating

```bash
pipx upgrade ha-history-exporter
```

## Quickstart

### 1. Create an access token

In Home Assistant:

1. Open your profile: select your name at the bottom of the sidebar.
2. Open **Security** and scroll down to **Long-lived access tokens**.
3. Select **Create token** and give it a name, such as `History Exporter`.
4. Copy the token right away. Home Assistant shows it only once.

### 2. Run the setup

```bash
hhe init
```

The setup asks a few questions. Press Enter to accept the suggestion shown in
square brackets.

| Question | What to enter |
|---|---|
| **Home Assistant URL** | The address you use in your browser, including `http://` or `https://` and the port, and nothing after that. For example, `http://homeassistant.local:8123`. |
| **Long-lived access token** | Paste the token and press Enter. Nothing appears while you do this, which is intended. To paste, right-click in PowerShell, press Cmd+V on a Mac, or press Ctrl+Shift+V on Linux. |
| **Output directory** | The folder for your history. The suggestion is `ha-history-exports` in your home folder. |
| **Output formats** | `jsonl` is a good start. To save several formats, separate them with commas, for example `jsonl,csv`. Choosing `parquet` needs [Parquet support](#parquet-support). |
| **Time zone of the exported days** | Press Enter to use the time zone of your Home Assistant. |
| **Recorder retention of Home Assistant in days** | The number of days your Home Assistant keeps its history. See below. |

After the token, the setup connects to your Home Assistant and shows
`OK: Home Assistant` with your version and number of entities. If it reports a
problem, answer `n`, look it up under [Troubleshooting](#troubleshooting) and
run `hhe init` again.

**Check the retention before you answer.** Open your `configuration.yaml` in
Home Assistant and look for `purge_keep_days` under `recorder:`. If it is not
there, the value is 10. Enter the real number: with a number that is too high,
days Home Assistant has already deleted are saved empty or incomplete, and they
count as done.

### 3. Save your first backup

This saves everything Home Assistant still has, if it keeps 10 days of history:

```bash
hhe export --last-days 9
```

The right number is always one less than your retention, because Home Assistant
is already deleting the oldest day. With `purge_keep_days: 30`, use
`--last-days 29`. If the number is too high, the export stops before it saves
anything.

The export pauses between its requests to keep your Home Assistant responsive,
so this can take a while. At the end, you see one line for each day:

```text
2026-06-13  ok       184213 state objects
2026-06-14  ok       179846 state objects
```

### 4. Find your files

Your history is in the `ha-history-exports` folder in your home folder, unless
you chose another one. This command lists the days you have:

```bash
hhe status
```

To keep your archive growing, run `hhe export` regularly or
[set up daily exports](#automating-daily-exports). Without further options, it
saves yesterday.

## Everyday commands

| Command | What it does |
|---|---|
| `hhe export` | Saves yesterday. |
| `hhe export --last-days 7` | Saves the last seven complete days, ending yesterday. |
| `hhe export --date 2026-06-15` | Saves one day. |
| `hhe export --start-date 2026-06-01 --end-date 2026-06-07` | Saves all days in this period. |
| `hhe export --date today` | Saves today up to this moment. A later export of this day saves it completely. |
| `hhe export --last-days 7 --dry-run` | Shows what would be saved, without saving anything. |
| `hhe export --date 2026-06-15 --force` | Saves a day again, replacing the saved copy. |
| `hhe status` | Lists your saved days and any gaps, month by month. |
| `hhe verify` | Checks that your saved files are unchanged. |
| `hhe verify --deep` | Also reads every saved entry. This is slower and more thorough. |
| `hhe convert --format parquet` | Adds a format to the days you saved. |
| `hhe doctor` | Checks your installation, settings, saved files and connection. |
| `hhe --help` | Lists all commands. `hhe export --help` shows the options of one command. |

## Configuration

`hhe init` stores your settings. You can change them at any time.

| To change | Command |
|---|---|
| The formats you save | `hhe config set export.formats jsonl,parquet` |
| The folder for your history | `hhe config set export.output_dir <folder>` |
| The retention | `hhe config set recorder.purge_keep_days 30` |
| The Home Assistant address | `hhe config set homeassistant.url http://homeassistant.local:8123` |
| The access token (asked for hidden) | `hhe config set homeassistant.token` |
| Which kinds of entities to leave out | `hhe config set entities.exclude_domains update,button` |
| Which entities to leave out by name | `hhe config set entities.exclude_patterns "sensor.*_debug"` |

These commands help you look at your settings:

| Command | What it does |
|---|---|
| `hhe config list` | Shows every setting. |
| `hhe config get export.formats` | Shows one setting. |
| `hhe config unset export.formats` | Returns a setting to its default. |
| `hhe config edit` | Opens the settings file in your editor and checks it after you close it. |
| `hhe config path` | Shows where your files are on this computer. |

### Where your files are

| | Windows | macOS | Linux |
|---|---|---|---|
| Your history | `ha-history-exports` in your user folder | `ha-history-exports` in your home folder | `ha-history-exports` in your home folder |
| Settings (`config.toml`) | `%APPDATA%\ha-history-exporter` | `~/Library/Application Support/ha-history-exporter` | `~/.config/ha-history-exporter` |
| Access token (`credentials.toml`) | same folder as the settings | same folder as the settings | same folder as the settings |

### If your Home Assistant slows down during an export

The default settings suit a home with about 1400 entities. If Home Assistant
feels slow during an export, ask for fewer entities at a time and pause longer:

```bash
hhe config set requests.batch_size 10
```

```bash
hhe config set requests.sleep_between_requests 1.5
```

<details>
<summary><strong>All settings</strong></summary>

Settings come from these sources. An entry higher in the list wins:

1. options of `hhe export`, such as `--batch-size 10`
2. environment variables, such as `HHE_REQUESTS_BATCH_SIZE=10`
3. the settings file `config.toml`
4. the defaults below

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

The settings file uses the [TOML](https://toml.io/) format and lists every key
with its documentation. The History Exporter reports an environment variable
that starts with `HHE_` but matches no setting.

</details>

<details>
<summary><strong>Advanced options</strong></summary>

**Running without a settings file.** With `HHE_URL` and `HHE_TOKEN` set in the
environment, `hhe export` works without `hhe init` and saves to
`ha-history-exports` in your home folder.

**Another settings file or folder.** `--config FILE` reads exactly that file for
one command. `HHE_CONFIG_DIR` names another folder for your settings.

**Paths.** Paths can use `~`, `$NAME` and `${NAME}`, and on Windows also
`%NAME%`. A variable that is not defined is an error. A relative path in the
settings file is relative to that file. In a variable or an option, it is
relative to the current folder.

**Formats of one run.** `--format` sets the complete list of formats for one
`hhe export`, for example `--format jsonl,csv`. `--format none` saves only the
current list of entities and their states, without any history.

**Log detail.** `hhe export` and `hhe convert` accept
`--log-level DEBUG`, `INFO`, `WARNING` or `ERROR`.

**Storing the token from a file.** Instead of typing the token, you can read it
from a file. On Linux and macOS:

```bash
hhe config set homeassistant.token --stdin < token.txt
```

In PowerShell:

```powershell
Get-Content token.txt | hhe config set homeassistant.token --stdin
```

**Setup without questions.** `hhe init --non-interactive` asks nothing. The URL
comes from `--url` or `HHE_URL`, and the token from `HHE_TOKEN` or standard
input. `--force` replaces your existing settings.

**What is requested.** By default, every recorded change of a day is saved with
its attributes, from midnight to midnight. A change at exactly midnight belongs
to the day it starts. With `history_request.skip_initial_state` set to `false`,
each entity also gets a first entry with its state at midnight. The options
`minimal_response`, `no_attributes` and `significant_changes_only` ask Home
Assistant for less. Values it leaves out are stored as `null`.

</details>

## Output

```text
ha-history-exports/
  exports/daily/2026/06/
    2026-06-15.jsonl            the history of this day
    2026-06-15.csv
    2026-06-15.parquet
    2026-06-15.manifest.json    the record of this day
  metadata/                     your entities at each export, and a list of exported days
  logs/                         a log file for each run
  hhe-archive.json              the time zone of this archive
```

You get the formats you chose. To back up your archive, copy the whole
`ha-history-exports` folder.

### Formats

All formats hold the same content.

| Format | Good for |
|---|---|
| **JSONL** (`.jsonl`) | Long-term archiving and your own scripts. One line for each change. |
| **CSV** (`.csv`) | Spreadsheets such as Excel. One row for each change. |
| **Parquet** (`.parquet`) | Data analysis with tools such as [DuckDB](https://duckdb.org/). Much smaller than the other two. |

Each entry has these fields:

| Field | Meaning |
|---|---|
| `entity_id` | The entity, for example `sensor.kitchen_temperature`. |
| `state` | Its state at that moment, for example `21.5`. |
| `last_changed` | When the state last changed, in UTC. |
| `last_updated` | When the state or an attribute last changed, in UTC. |
| `attributes` | Its attributes. CSV and Parquet call this column `attributes_json`. |
| `local_offset` | The difference between your time zone and UTC at that moment, for example `+02:00`. |
| `extra_json` | CSV and Parquet only: any further data from Home Assistant, usually empty. JSONL keeps it as separate fields. |

A JSONL line looks like this, on one line in the file:

```json
{"entity_id": "sensor.kitchen_temperature", "state": "21.5",
 "last_changed": "2026-06-15T06:30:00+00:00",
 "last_updated": "2026-06-15T06:30:00+00:00",
 "attributes": {"unit_of_measurement": "°C"},
 "local_offset": "+02:00"}
```

This [DuckDB](https://duckdb.org/) query reads one sensor from all your Parquet
files:

```sql
SELECT entity_id, state, last_changed
FROM read_parquet('~/ha-history-exports/exports/daily/**/*.parquet')
WHERE entity_id = 'sensor.kitchen_temperature'
ORDER BY last_changed;
```

### The record of each day

The `.manifest.json` file records what was saved for a day, with checksums of
its files. Keep it with your files: `hhe verify` needs it.

A day with a successful record counts as saved, even if you later move or delete
its files. `hhe verify` then reports those files as `missing`, so you can keep
older days on other storage.

### Day status

`hhe status` shows each day as one of these:

| Status | Meaning |
|---|---|
| `ok` | The complete day is saved. |
| `partial` | Today, saved up to the moment of the export. |
| `failed` | The last attempt failed. Run the export again. |
| `pending` | The export of this day did not finish. Run the export again. |
| `invalid` | The record of this day cannot be read. `hhe verify` shows the reason. |
| `missing` | Nothing is saved for this day. |

### File check

`hhe verify` reports each file as one of these:

| Result | Meaning |
|---|---|
| `ok` | The file matches its record. |
| `missing` | The record names the file, but it is not there. This alone is not an error. |
| `mismatch` | The file differs from its record. |
| `unreadable` | The file cannot be read. |
| `unexpected` | The file belongs to a day, but its record does not name it. |

### Time zone of an archive

All days in an archive use the same time zone, the one set when the archive
was created. To use another time zone, save to a different folder.

## Exit codes

Every command reports how it ended with an exit code. You only need these for
scripts and scheduled tasks. Add `--json` to `hhe export`, `convert`, `verify`,
`status` or `doctor` to get the result in a form that programs can read.

| Code | Meaning |
|---|---|
| `0` | Everything worked. |
| `1` | A day failed, `hhe verify` or `hhe doctor` found a problem, `hhe convert` could not convert a day, or another export is running. |
| `2` | A command or a setting is wrong. |
| `3` | Home Assistant rejected the access token. |
| `4` | Home Assistant could not be reached or gave an unusable answer, or its HTTPS certificate could not be verified. |
| `70` | An unexpected internal error. The message names the file with the details. |
| `130` | You stopped the command with Ctrl+C. |

If one day fails, the export continues with the next day. A rejected token, an
unreachable Home Assistant, a certificate problem or an internal error ends the
run.

## Automating daily exports

This command, run once a day, keeps your archive complete. It covers two days,
so a single missed run does no harm, and it skips days you already saved:

```bash
hhe export --last-days 2
```

If your computer is often off for several days, use a larger number, up to one
less than your retention. A scheduled export needs no input, because your token
is stored.

<details>
<summary><strong>Windows: Task Scheduler</strong></summary>

1. In PowerShell, run this command and copy the path it shows:

   ```powershell
   (Get-Command hhe).Source
   ```

2. Press the Windows key, type `Task Scheduler` and press Enter.
3. Select **Create Basic Task**, enter a name and select **Next**.
4. Select **Daily**, then **Next**, and choose a time when your computer is
   usually on.
5. Select **Start a program**. Paste the path under **Program/script** and enter
   this under **Add arguments**: `export --last-days 2`
6. Select **Finish**.

On a laptop, open the properties of the task and check the **Conditions** tab,
which can limit a task to mains power. The **Settings** tab can start a missed
task as soon as possible.

</details>

<details>
<summary><strong>macOS and Linux: cron</strong></summary>

1. Run this command and copy the path it shows:

   ```bash
   command -v hhe
   ```

2. Open your schedule:

   ```bash
   crontab -e
   ```

3. Add a line like this one, with your path, to export at 03:30 every day:

   ```cron
   30 3 * * * /home/you/.local/bin/hhe export --last-days 2
   ```

cron does not catch up on a run that was missed while the computer was off or
asleep. The two-day range covers one missed day.

</details>

## Troubleshooting

Run `hhe doctor` first. It checks your installation, settings, saved files and
connection, and for most problems it shows the command that fixes them.

**`hhe` or `pipx` is not recognized, or `command not found`.**
Close the terminal and open a new one. This includes terminals inside other
programs such as VS Code, which need a restart of the program. If that does not
help, run `py -m pipx ensurepath` on Windows or `pipx ensurepath` on macOS and
Linux, and open a new terminal again.

**`py` is not recognized on Windows.**
Open a new PowerShell window. If that does not help, search the Start menu for
**Manage app execution aliases** and check that the entries for Python are on.

**pipx reports that the History Exporter needs a newer Python.**
It needs Python 3.12 or newer. See the guide for your computer under
[Installation](#installation).

**`No Home Assistant URL is configured.` or `No Home Assistant access token is configured.`**
Run `hhe init`.

**`… is not a valid Home Assistant base URL.`**
Enter only the start of the address: `http://` or `https://`, the host name and
the port. For example, use `http://homeassistant.local:8123`, without anything
after the port, such as `/lovelace/0`.

**`Home Assistant rejected the access token (HTTP 401).`**
The token is wrong or was deleted. Create a new one as in the
[Quickstart](#1-create-an-access-token) and store it:

```bash
hhe config set homeassistant.token
```

**`Home Assistant is not available at …`**
Check that the address works in a browser on this computer. If
`homeassistant.local` does not work, use the IP address of your Home Assistant
instead, for example `http://<your-ip-address>:8123`. If this computer uses a
proxy, exclude that address from it.

**`Home Assistant did not answer in time.`**
Give each request more time and ask for fewer entities at once:

```bash
hhe export --timeout 300 --batch-size 5
```

**`Home Assistant could not answer …: redirected with HTTP 301.`**
The address forwards to another one, often from `http://` to `https://`. Use the
address your browser ends up on:

```bash
hhe config set homeassistant.url https://homeassistant.example.net
```

**`The HTTPS certificate of Home Assistant could not be verified.`**
If you use a certificate from your own certificate authority, point the History
Exporter to that authority:

```bash
hhe config set homeassistant.ca_bundle <path-to-ca.pem>
```

**`… is reached over plain HTTP outside the local network …`**
Your access token travels unencrypted. Use the `https://` address of your Home
Assistant.

**`Recorder retention blocks one or more requested days.`**
A requested day is too old, and the export stopped without saving anything. Ask
for fewer days: at most one less than your retention. The message names the days
concerned.

**`Empty history … cannot be verified without Recorder retention.`**
Store the retention of your Home Assistant, with your own number in place of 10:

```bash
hhe config set recorder.purge_keep_days 10
```

**`Parquet output is enabled but pyarrow is not installed.`**
Follow [Parquet support](#parquet-support), or save JSONL instead:

```bash
hhe config set export.formats jsonl
```

**`export.timezone is …, but this archive's days are in …`**
Remove the time zone setting, or save to a different folder:

```bash
hhe config unset export.timezone
```

**`Unknown configuration key '…'`**
The name is misspelled. The message suggests a similar valid name if there is
one, and `hhe config list` shows them all.

**`Another export is already running for this output directory.`**
Wait until that run has finished, then start again.

**A day shows as `failed`.**
Run the same export again. Days that failed are not skipped. The log file named
at the end of the run has the details.

**`hhe verify` reports `unexpected` files.**
A file for that day is in the folder, but its record does not name it. Check the
file, then move or delete it.

If none of this helps, please open a
[GitHub issue](https://github.com/hyperfelixations/ha-history-exporter/issues)
and include:

- the version, from `hhe --version`;
- your operating system;
- the exact error message;
- the output of `hhe doctor`, without private addresses.

## Known limitations

- **Older days cannot be recovered.** The History Exporter can only save what
  Home Assistant still has, so start early and export regularly.
- **Long-term statistics are not exported.** Home Assistant keeps these separate
  hourly summaries of sensors beyond the retention, and they are not part of
  the history the History Exporter reads.
- **Only entities that exist at the time of the export are saved.** The history
  of an entity you deleted is not included.
- **Only recorded entities are saved.** An entity you excluded from the Recorder
  in Home Assistant has no history to export.
- **Spreadsheets can misread CSV files.** They treat a cell that starts with `=`,
  `+`, `-` or `@` as a formula. Import CSV files as text, or use JSONL or Parquet
  for analysis.
- **Network drives depend on their server.** Let only one computer at a time
  write to an archive there.

## Security

- The History Exporter only reads from Home Assistant and changes nothing there.
- Your access token is stored in `credentials.toml`, which only your account can
  read. It never appears in an export, a log or an error message.
- The token goes only to the address you configured. The History Exporter
  verifies HTTPS certificates and does not follow redirects.
- Your exports, entity lists and logs describe what happens in your home. Treat
  them as private, and check a log file before you share it: it lists entity
  names and, after an internal error, technical details.

To report a vulnerability, see the
[security policy](https://github.com/hyperfelixations/ha-history-exporter/blob/main/SECURITY.md).

## Links

- [Releases](https://github.com/hyperfelixations/ha-history-exporter/releases):
  every version, with its notes
- [PyPI](https://pypi.org/project/ha-history-exporter/): the package page
- [Issues](https://github.com/hyperfelixations/ha-history-exporter/issues):
  questions, setups and ideas
- [Discord](https://discord.gg/zfGKCVEvwe): the same, as a conversation
- [License](https://github.com/hyperfelixations/ha-history-exporter/blob/main/LICENSE)
  (MIT)
- Elsewhere: [GitHub](https://github.com/hyperfelixations),
  [YouTube](https://www.youtube.com/@hyperfelixations), and
  [Instagram](https://www.instagram.com/hyperfelixations/)
