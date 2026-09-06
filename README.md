# Home Assistant History Exporter

Home Assistant History Exporter is a local command-line tool for exporting raw
Home Assistant state history through the official REST API.

The project is intended for people who want to keep an independent,
analysis-friendly archive of their Home Assistant history without accessing or
modifying the Home Assistant database directly.

## What it does

- Fetches the current entity list from Home Assistant
- Exports Recorder state changes in configurable entity batches
- Uses complete local calendar days with timezone and daylight-saving support
- Writes any configured combination of JSONL, Parquet, and CSV
- Uses JSONL as the sole default output format
- Uses temporary JSONL streaming internally when only Parquet or CSV is wanted
- Records per-day manifests and entity snapshots
- Uses successful manifests for resumable date ranges, even after artifacts
  have been archived elsewhere
- Supports retries and validation before finalization

All communication with Home Assistant is read-only. Authentication uses a
Home Assistant Long-Lived Access Token supplied through an environment
variable.

## Output formats

- **JSONL** preserves the complete exported state objects as a streamable text
  archive.
- **Parquet** provides a compact, typed representation for tools such as
  DuckDB, Pandas, and Polars.
- **CSV** is available for simple inspection and interoperability with tools
  that do not support JSONL or Parquet.

Exports are organized by calendar day and accompanied by a manifest containing
status, timing, request, entity, and row-count information.

If all three history formats are disabled, HHE runs in snapshot-only mode. It
saves the current entity list and states from `/api/states`, makes no History
API request, and does not create a daily success manifest.

## Project status

The project is currently at version `1.3.2` and is under active development.
Its command-line interface, configuration, and output contracts may still
evolve as the project is prepared for public release.

## Installation

### Option A: pipx (recommended)

[pipx](https://pipx.pypa.io/) installs the tool into its own isolated
environment and puts a single `ha-history-exporter` command on your `PATH`:

```bash
pipx install "git+https://github.com/hyperfelixations/ha-history-exporter.git"
```

Without pipx, `pip install` works the same way inside a virtual environment.

This installs JSONL and CSV support. Parquet output depends on
[pyarrow](https://arrow.apache.org/docs/python/), a large, platform-specific
package, so it is an opt-in extra:

```bash
pipx install "ha-history-exporter[parquet] @ git+https://github.com/hyperfelixations/ha-history-exporter.git"
```

Once installed, `ha-history-exporter --date yesterday` and
`python -m ha_history_exporter --date yesterday` are equivalent. `--config`
defaults to the relative path `export_config.yaml`, so either run the command
from the directory containing your configuration file, or pass
`--config /path/to/export_config.yaml` explicitly.

A release on [PyPI](https://pypi.org/) (`pip install ha-history-exporter`
without a Git URL) is planned but not available yet.

### Option B: clone and run

```bash
git clone https://github.com/hyperfelixations/ha-history-exporter.git
cd ha-history-exporter
pip install -r requirements.txt
python ha_history_batch_export.py --date yesterday
```

This installs Parquet support unconditionally and is unaffected by the pipx
path above.

## Configuration

Copy `export_config.example.yaml` to `export_config.yaml` and adjust the local
copy for your environment. `export_config.yaml` is ignored by Git so local
paths and settings are not published accidentally.

The Home Assistant URL and Long-Lived Access Token are not stored in the YAML
file. They are read from the `HA_URL` and `HA_TOKEN` environment variables.

## Testing

The automated test suite uses only synthetic data, temporary directories, and
in-memory Home Assistant substitutes. Network sockets are disabled during
pytest runs, so the suite cannot contact a real Home Assistant instance.

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest --cov=ha_history_exporter --cov-report=term-missing
```

The same isolated suite runs on Linux and Windows through GitHub Actions.

## License

MIT — see [LICENSE](LICENSE).
