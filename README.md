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

## Project status

The project is currently at version `1.3.1` and is under active development.
Its command-line interface, configuration, and output contracts may still
evolve as the project is prepared for public release.

More detailed installation, configuration, and usage instructions will be
added as the project is prepared for public release.

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
