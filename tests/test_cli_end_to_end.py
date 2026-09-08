"""End-to-end behaviour of the command line, driven through cli.main().

What an export *produces* is a stricter contract and lives in
tests/test_output_contract.py, pinned against golden fixtures. This module
covers the parts only reachable through the command line: the entity snapshot,
the log directory, the resume short-circuit, and the exit codes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ha_history_exporter import cli, ha_client
from ha_history_exporter.errors import AuthError
from tests.helpers import FakeCliClient

DAY = "2026-07-28"

ALL_FORMATS = "[jsonl, csv, parquet]"
JSONL_AND_CSV = "[jsonl, csv]"
NO_FORMATS = "[]"


def write_config(directory: Path, *, formats: str = JSONL_AND_CSV) -> Path:
    path = directory / "config.yaml"
    output = (directory / "output").as_posix()
    temp = (directory / "temp").as_posix()
    path.write_text(
        f"""
export:
  output_dir: "{output}"
  timezone: Europe/Berlin
  formats: {formats}
requests:
  batch_size: 2
  sleep_between_requests: 0
  sleep_between_days: 0
  max_retries: 0
storage:
  temp_dir: "{temp}"
  locked_file_retries: 0
  locked_file_retry_sleep: 0
""",
        encoding="utf-8",
    )
    return path


def set_synthetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HA_URL", "http://home-assistant.invalid")
    monkeypatch.setenv("HA_TOKEN", "synthetic-test-token")


def day_dir(root: Path) -> Path:
    return root / "output" / "exports" / "daily" / "2026" / "07"


# ── output layout and formats ─────────────────────────────────────────────────

def test_output_layout_is_unchanged(tmp_path, monkeypatch):
    path = write_config(tmp_path, formats=ALL_FORMATS)
    set_synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["export", "--config", str(path), "--date", DAY]) == 0

    output = tmp_path / "output"
    for suffix in ("jsonl", "csv", "parquet", "manifest.json"):
        assert (day_dir(tmp_path) / f"{DAY}.{suffix}").is_file()
    assert (output / "metadata" / "export_runs.jsonl").is_file()
    assert list((output / "metadata").glob("entity_snapshot_*.json"))
    assert list((output / "logs").glob("ha_history_export_*.log"))


def test_snapshot_only_contract_is_unchanged(tmp_path, monkeypatch):
    path = write_config(tmp_path, formats=NO_FORMATS)
    set_synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["export", "--config", str(path), "--date", DAY]) == 0

    assert FakeCliClient.instances[0].history_calls == []
    assert not (tmp_path / "output" / "exports").exists()
    assert list((tmp_path / "output" / "metadata").glob("entity_snapshot_*.json"))


def test_successful_manifest_prevents_a_second_home_assistant_call(
    tmp_path, monkeypatch
):
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["export", "--config", str(path), "--date", DAY]) == 0
    assert len(FakeCliClient.instances[0].history_calls) == 1

    assert cli.main(["export", "--config", str(path), "--date", DAY]) == 0
    assert len(FakeCliClient.instances) == 1  # no second client was constructed


# ── exit codes ────────────────────────────────────────────────────────────────

def _run_missing_config(tmp_path, monkeypatch) -> int:
    return cli.main(["export", "--config", str(tmp_path / "absent.yaml"), "--date", DAY])


def _run_invalid_timezone(tmp_path, monkeypatch) -> int:
    path = write_config(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "timezone: Europe/Berlin", "timezone: Invalid/Timezone"
        ),
        encoding="utf-8",
    )
    set_synthetic_env(monkeypatch)
    return cli.main(["export", "--config", str(path), "--date", DAY])


def _run_invalid_override(tmp_path, monkeypatch) -> int:
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    return cli.main(["export", "--config", str(path), "--date", DAY, "--batch-size", "0"])


def _run_missing_credentials(tmp_path, monkeypatch) -> int:
    path = write_config(tmp_path)
    return cli.main(["export", "--config", str(path), "--date", DAY])


def _run_auth_failure(tmp_path, monkeypatch) -> int:
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    FakeCliClient.check_error = AuthError("synthetic auth failure")
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)
    return cli.main(["export", "--config", str(path), "--date", DAY])


def _run_interrupt(tmp_path, monkeypatch) -> int:
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    FakeCliClient.check_error = KeyboardInterrupt()
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)
    return cli.main(["export", "--config", str(path), "--date", DAY])


def _run_success(tmp_path, monkeypatch) -> int:
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)
    return cli.main(["export", "--config", str(path), "--date", DAY])


@pytest.mark.parametrize(
    ("scenario", "expected_code"),
    [
        (_run_missing_config, 2),
        (_run_invalid_timezone, 2),
        (_run_invalid_override, 2),
        (_run_missing_credentials, 2),
        (_run_auth_failure, 1),
        (_run_interrupt, 130),
        (_run_success, 0),
    ],
    ids=[
        "missing-config",
        "invalid-timezone",
        "invalid-numeric-override",
        "missing-credentials",
        "auth-failure",
        "keyboard-interrupt",
        "success",
    ],
)
def test_exit_codes_are_unchanged(tmp_path, monkeypatch, scenario, expected_code):
    assert scenario(tmp_path, monkeypatch) == expected_code
