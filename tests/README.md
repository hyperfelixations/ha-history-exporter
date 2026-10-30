# HHE test suite

The test suite is deliberately isolated from every real Home Assistant
instance.

## Safety contract

- Tests never use a real `HHE_URL` or `HHE_TOKEN`.
- The autouse environment fixture removes every HHE variable the key registry
  declares by its public name before every test. It never inventories the host
  environment.
- Pytest runs with `--disable-socket`; any attempted network connection fails
  immediately. Only the system tests in `tests/system/` open a socket, to a
  stand-in for Home Assistant on `127.0.0.1`.
- Home Assistant is simulated in memory behind the `HistorySource` protocol
  (`tests/support/fake_source.py`); the REST source is tested against a
  scripted transport.
- All files are written below pytest's temporary directories.
- Tests never examine, read, or write a production configuration or archive.

## Test layers

- `tests/unit/`: domain, file primitives, formats, settings, source, archive
  and command-line parts, each on its own.
- `tests/integration/`: the use cases and the commands in-process, with an
  in-memory Home Assistant, a fixed clock and injected terminal services.
- `tests/contract/`: the output contract as golden files, driven through the
  single seam `tests/support/harness.py` (`produce()` for exports, `derive()`
  for conversions), and every command HHE suggests in a message checked
  against the parser and the key registry.
- `tests/system/`: `python -m ha_history_exporter` in a process of its own.
- `tests/repo/` (marker `repo`): the repository rather than the product:
  import contracts, privacy audit, tool pins, package classifiers, the
  workflows under actionlint, shellcheck and zizmor, the release tools. It
  needs the tools of `requirements-dev.txt` and is not part of the source
  distribution.

Tests that need the optional `pyarrow` carry the marker `parquet` or skip
themselves without it. Regenerate golden files only for a reviewed, intended
output change: `python tools/refresh_golden.py`.

## Run locally

```bash
python -m pip install -r requirements-dev.txt
python -m pip install -e ".[parquet]"
python -m pytest --cov=ha_history_exporter
```

Without `pyarrow`, deselect what needs it:

```bash
python -m pytest -m "not parquet"
```
