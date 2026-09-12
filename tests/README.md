# HHE test suite

The test suite is deliberately isolated from every real Home Assistant
instance.

## Safety contract

- Tests must never use a real `HA_URL` or `HA_TOKEN`.
- The autouse environment fixture removes every declared HHE variable by its
  public name before every test. It never inventories the host environment.
- Pytest runs with `--disable-socket`; any attempted network connection fails
  immediately.
- HTTP behavior is simulated with in-memory responses.
- Export integration tests use a `FakeHomeAssistantClient`.
- All files are written below pytest's temporary directories.
- Tests must never examine, read, or write a production configuration or
  History Export directory.

## Test layers

- Pure unit tests for configuration, time handling, planning, manifests,
  snapshots, writers, and validators
- HTTP contract tests with synthetic responses and exceptions
- Export and CLI integration tests with synthetic clients and temporary output
- A strict expected-failure test for the remaining confirmed product defect

Known defects use `xfail(strict=True)`. They keep the normal suite green while
remaining executable specifications. If a defect unexpectedly stops
reproducing, pytest reports an XPASS and fails the suite until the expectation
is reviewed.

## Run locally

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest --cov=ha_history_exporter --cov-report=term-missing
```
