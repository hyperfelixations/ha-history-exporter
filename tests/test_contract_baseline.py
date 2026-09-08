"""Characterization tests for the command-line surface of version 1.3.2.

These pin the flag surface, the invocation forms, and the exit codes exactly as
they are today, so that no refactoring changes them by accident.

The *result* of an export - directory layout, file names, row shapes, manifest
fields, Parquet schema - is a separate and stricter contract. It lives in
tests/test_output_contract.py, pinned byte-for-byte against golden fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ha_history_exporter import cli, ha_client
from ha_history_exporter.exceptions import AuthError
from tests.helpers import FakeCliClient

DAY = "2026-07-28"

# The complete flag surface published by version 1.3.2. Every entry must keep
# working; new options may be added, but none of these may disappear.
LEGACY_SWITCH_DESTS = (
    "dry_run",
    "force",
)
LEGACY_VALUE_DESTS = (
    "config",
    "date",
    "start_date",
    "end_date",
    "outdir",
    "timezone",
    "batch_size",
    "sleep_between_requests",
    "sleep_between_days",
    "timeout",
    "max_retries",
    "log_level",
)

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


# ── flag surface ──────────────────────────────────────────────────────────────

def test_legacy_flag_surface_is_unchanged():
    parsed = vars(cli._parse_args(["--date", DAY]))

    for dest in LEGACY_SWITCH_DESTS:
        assert dest in parsed, f"switch for {dest} disappeared"
        assert parsed[dest] is False
    for dest in LEGACY_VALUE_DESTS:
        assert dest in parsed, f"option for {dest} disappeared"
    assert parsed["date"] == DAY
    assert parsed["start_date"] is None
    assert parsed["end_date"] is None


def test_legacy_flags_all_still_parse_together():
    args = cli._parse_args(
        [
            "--config", "somewhere.yaml",
            "--start-date", "2026-07-20",
            "--end-date", DAY,
            "--dry-run",
            "--force",
            "--outdir", "out",
            "--timezone", "Europe/Berlin",
            "--batch-size", "9",
            "--sleep-between-requests", "0.5",
            "--sleep-between-days", "1.5",
            "--timeout", "30",
            "--max-retries", "2",
            "--log-level", "DEBUG",
        ]
    )

    assert args.config == "somewhere.yaml"
    assert (args.start_date, args.end_date) == ("2026-07-20", DAY)
    assert args.dry_run and args.force
    assert args.outdir == "out"
    assert args.timezone == "Europe/Berlin"
    assert args.batch_size == 9
    assert args.sleep_between_requests == 0.5
    assert args.sleep_between_days == 1.5
    assert args.timeout == 30
    assert args.max_retries == 2
    assert args.log_level == "DEBUG"


def test_date_and_range_selectors_remain_mutually_exclusive():
    with pytest.raises(SystemExit) as exc:
        cli._parse_args(["--date", DAY, "--start-date", "2026-07-20"])
    assert exc.value.code == 2


# ── invocation forms ──────────────────────────────────────────────────────────

def test_legacy_invocation_without_subcommand_exports(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["--config", str(path), "--date", DAY]) == 0

    manifest = json.loads(
        (day_dir(tmp_path) / f"{DAY}.manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "ok"


# ── output layout and formats ─────────────────────────────────────────────────

def test_output_layout_is_unchanged(tmp_path, monkeypatch):
    path = write_config(tmp_path, formats=ALL_FORMATS)
    set_synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["--config", str(path), "--date", DAY]) == 0

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

    assert cli.main(["--config", str(path), "--date", DAY]) == 0

    assert FakeCliClient.instances[0].history_calls == []
    assert not (tmp_path / "output" / "exports").exists()
    assert list((tmp_path / "output" / "metadata").glob("entity_snapshot_*.json"))


def test_successful_manifest_prevents_a_second_home_assistant_call(
    tmp_path, monkeypatch
):
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["--config", str(path), "--date", DAY]) == 0
    assert len(FakeCliClient.instances[0].history_calls) == 1

    assert cli.main(["--config", str(path), "--date", DAY]) == 0
    assert len(FakeCliClient.instances) == 1  # no second client was constructed


# ── exit codes ────────────────────────────────────────────────────────────────

def _run_missing_config(tmp_path, monkeypatch) -> int:
    return cli.main(["--config", str(tmp_path / "absent.yaml"), "--date", DAY])


def _run_invalid_timezone(tmp_path, monkeypatch) -> int:
    path = write_config(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "timezone: Europe/Berlin", "timezone: Invalid/Timezone"
        ),
        encoding="utf-8",
    )
    set_synthetic_env(monkeypatch)
    return cli.main(["--config", str(path), "--date", DAY])


def _run_invalid_override(tmp_path, monkeypatch) -> int:
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    return cli.main(["--config", str(path), "--date", DAY, "--batch-size", "0"])


def _run_missing_credentials(tmp_path, monkeypatch) -> int:
    path = write_config(tmp_path)
    return cli.main(["--config", str(path), "--date", DAY])


def _run_auth_failure(tmp_path, monkeypatch) -> int:
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    FakeCliClient.check_error = AuthError("synthetic auth failure")
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)
    return cli.main(["--config", str(path), "--date", DAY])


def _run_interrupt(tmp_path, monkeypatch) -> int:
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    FakeCliClient.check_error = KeyboardInterrupt()
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)
    return cli.main(["--config", str(path), "--date", DAY])


def _run_success(tmp_path, monkeypatch) -> int:
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)
    return cli.main(["--config", str(path), "--date", DAY])


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
