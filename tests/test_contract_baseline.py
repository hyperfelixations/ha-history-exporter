"""Characterization tests for the contract that must survive refactoring.

These tests pin the user-visible behaviour of version 1.3.2: the legacy flag
surface, the invocation forms, the output layout, the manifest field set, the
row shapes, and the exit codes. They assert the contract rather than the
internals, so a module may move as long as the observable behaviour does not.
"""

from __future__ import annotations

import csv
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
    "resume",
    "no_csv",
    "jsonl",
    "parquet",
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

MANIFEST_FIELDS = {
    "schema_version",
    "status",
    "source",
    "date",
    "timezone",
    "start_local",
    "end_local",
    "start_utc",
    "end_utc",
    "export_started_at",
    "export_finished_at",
    "duration_seconds",
    "entity_count_current",
    "entity_count_requested",
    "entity_count_with_history",
    "entity_count_zero_history",
    "state_object_count",
    "batch_size_entities",
    "request_count",
    "failed_request_count",
    "retried_request_count",
    "history_request_options",
    "output_files",
    "zero_history_entities",
    "failed_batches",
    "skipped_reason",
    "error",
    "script_version",
}

JSONL_FIELDS = [
    "entity_id",
    "state",
    "last_changed",
    "last_updated",
    "attributes",
    "local_offset",
]
CSV_HEADER = [
    "entity_id",
    "state",
    "last_changed",
    "last_updated",
    "attributes_json",
    "local_offset",
]

ALL_FORMATS = "jsonl: true\n  csv: true\n  parquet: true"
JSONL_AND_CSV = "jsonl: true\n  csv: true\n  parquet: false"
NO_FORMATS = "jsonl: false\n  csv: false\n  parquet: false"


def write_config(directory: Path, *, formats: str = JSONL_AND_CSV) -> Path:
    path = directory / "export_config.yaml"
    output = (directory / "output").as_posix()
    temp = (directory / "temp").as_posix()
    path.write_text(
        f"""
home_assistant:
  timezone: Europe/Berlin
export:
  output_dir: "{output}"
  resume: true
requests:
  batch_size_entities: 2
  sleep_between_requests_seconds: 0
  sleep_between_days_seconds: 0
  max_retries: 0
formats:
  {formats}
storage:
  temp_dir: "{temp}"
  cloud_storage_retry_count: 0
  cloud_storage_retry_sleep_seconds: 0
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
            "--resume",
            "--outdir", "out",
            "--no-csv",
            "--jsonl",
            "--parquet",
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
    assert args.dry_run and args.force and args.resume
    assert args.outdir == "out"
    assert args.no_csv and args.jsonl and args.parquet
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


def test_project_config_is_used_without_an_explicit_config_flag(
    tmp_path, monkeypatch
):
    """Running inside a directory that holds export_config.yaml must work."""
    write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["--date", DAY]) == 0
    assert (day_dir(tmp_path) / f"{DAY}.manifest.json").exists()


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


def test_manifest_field_set_is_unchanged(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["--config", str(path), "--date", DAY]) == 0

    manifest = json.loads(
        (day_dir(tmp_path) / f"{DAY}.manifest.json").read_text(encoding="utf-8")
    )
    assert set(manifest) == MANIFEST_FIELDS
    assert manifest["schema_version"] == "1.3"
    assert manifest["source"] == "home_assistant_rest_history"
    assert manifest["date"] == DAY
    # Read by the internal data-analysis documentation; keys must stay stable.
    assert set(manifest["output_files"]) == {"jsonl", "csv", "parquet"}
    assert set(manifest["history_request_options"]) == {
        "minimal_response",
        "no_attributes",
        "significant_changes_only",
    }


def test_jsonl_and_csv_row_shape_is_unchanged(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["--config", str(path), "--date", DAY]) == 0

    lines = (
        (day_dir(tmp_path) / f"{DAY}.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    row = json.loads(lines[0])
    assert list(row) == JSONL_FIELDS
    assert row["last_changed"].endswith("+00:00")
    assert row["local_offset"] == "+02:00"

    csv_path = day_dir(tmp_path) / f"{DAY}.csv"
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        assert next(reader) == CSV_HEADER
        assert len(next(reader)) == len(CSV_HEADER)


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
