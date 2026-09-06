from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

from ha_history_exporter import cli, ha_client
from ha_history_exporter.exceptions import AuthError
from tests.helpers import FakeCliClient


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    output = (tmp_path / "output").as_posix()
    temp = (tmp_path / "temp").as_posix()
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
  jsonl: true
  csv: false
  parquet: false
storage:
  temp_dir: "{temp}"
  cloud_storage_retry_count: 0
  cloud_storage_retry_sleep_seconds: 0
""",
        encoding="utf-8",
    )
    return path


def set_synthetic_env(monkeypatch):
    monkeypatch.setenv("HA_URL", "http://home-assistant.invalid")
    monkeypatch.setenv("HA_TOKEN", "synthetic-test-token")


def test_parse_args_requires_date_selector():
    with pytest.raises(SystemExit) as exc:
        cli._parse_args([])
    assert exc.value.code == 2


def test_parse_args_accepts_tuning_options():
    args = cli._parse_args(
        [
            "--date",
            "2026-07-28",
            "--batch-size",
            "12",
            "--timeout",
            "10",
            "--max-retries",
            "0",
            "--parquet",
            "--no-csv",
        ]
    )
    assert args.date == "2026-07-28"
    assert args.batch_size == 12
    assert args.timeout == 10
    assert args.max_retries == 0
    assert args.parquet
    assert args.no_csv


def test_parse_args_prog_follows_invocation_name(monkeypatch, capsys):
    """`prog` must not be hardcoded to the legacy launcher filename.

    Before packaging, argparse always advertised `ha_history_batch_export.py`
    in --help usage, even when invoked as the `ha-history-exporter` console
    script. argparse derives the default prog from sys.argv[0] at parser
    construction time, independent of the argv list passed to parse_args().
    """
    monkeypatch.setattr(sys, "argv", ["ha-history-exporter", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli._parse_args(["--help"])
    assert exc.value.code == 0
    usage_line = capsys.readouterr().out.splitlines()[0]
    assert usage_line.startswith("usage: ha-history-exporter ")


def test_resolve_date_range_requires_end_date():
    args = cli._parse_args(["--start-date", "2026-07-27"])
    with pytest.raises(ValueError, match="--end-date is required"):
        cli._resolve_date_range(args, cli.ZoneInfo("Europe/Berlin"))


def test_resolve_date_range_rejects_reversed_range():
    args = cli._parse_args(
        [
            "--start-date",
            "2026-07-29",
            "--end-date",
            "2026-07-28",
        ]
    )
    with pytest.raises(ValueError, match="must not be after"):
        cli._resolve_date_range(args, cli.ZoneInfo("Europe/Berlin"))


def test_main_returns_two_for_missing_config(capsys):
    result = cli.main(
        ["--config", "does-not-exist.yaml", "--date", "2026-07-28"]
    )
    assert result == 2
    assert "Config file not found" in capsys.readouterr().err


def test_main_returns_two_for_invalid_timezone(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    text = path.read_text(encoding="utf-8").replace(
        "timezone: Europe/Berlin", "timezone: Invalid/Timezone"
    )
    path.write_text(text, encoding="utf-8")
    set_synthetic_env(monkeypatch)

    assert cli.main(["--config", str(path), "--date", "2026-07-28"]) == 2
    assert not (tmp_path / "output" / "logs").exists()


def test_main_rejects_invalid_numeric_override_before_client(
    tmp_path, monkeypatch, capsys
):
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)

    class ForbiddenClient:
        def __init__(self, **kwargs):
            raise AssertionError("client must not be constructed")

    monkeypatch.setattr(ha_client, "HomeAssistantClient", ForbiddenClient)

    assert (
        cli.main(
            [
                "--config",
                str(path),
                "--date",
                "2026-07-28",
                "--batch-size",
                "0",
            ]
        )
        == 2
    )
    assert "batch_size_entities" in capsys.readouterr().err


def test_main_snapshot_only_saves_entities_without_history_or_day_manifest(
    tmp_path, monkeypatch
):
    path = write_config(tmp_path)
    text = path.read_text(encoding="utf-8").replace(
        "jsonl: true\n  csv: false\n  parquet: false",
        "jsonl: false\n  csv: false\n  parquet: false",
    )
    path.write_text(text, encoding="utf-8")
    set_synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["--config", str(path), "--date", "2026-07-28"]) == 0

    instance = FakeCliClient.instances[0]
    assert instance.history_calls == []
    assert instance.closed
    snapshots = list((tmp_path / "output" / "metadata").glob("entity_snapshot_*.json"))
    assert len(snapshots) == 1
    snapshot = json.loads(snapshots[0].read_text(encoding="utf-8"))
    assert snapshot["entity_count"] == 1
    assert snapshot["entities"][0]["state_at_snapshot"] == "1"
    assert not (tmp_path / "output" / "exports").exists()


def test_main_dry_run_uses_only_synthetic_client(
    tmp_path, monkeypatch, capsys
):
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    result = cli.main(
        [
            "--config",
            str(path),
            "--date",
            "2026-07-28",
            "--dry-run",
        ]
    )

    assert result == 0
    assert len(FakeCliClient.instances) == 1
    instance = FakeCliClient.instances[0]
    assert instance.kwargs["url"] == "http://home-assistant.invalid"
    assert instance.kwargs["token"] == "synthetic-test-token"
    assert instance.history_calls == []
    assert instance.closed
    assert "Dry run complete" in capsys.readouterr().err


def test_main_full_export_with_fake_client(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    result = cli.main(
        ["--config", str(path), "--date", "2026-07-28"]
    )

    assert result == 0
    instance = FakeCliClient.instances[0]
    assert len(instance.history_calls) == 1
    output = tmp_path / "output"
    manifest_path = (
        output
        / "exports"
        / "daily"
        / "2026"
        / "07"
        / "2026-07-28.manifest.json"
    )
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "ok"


def test_main_handles_authentication_failure(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    FakeCliClient.check_error = AuthError("synthetic auth failure")
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    result = cli.main(
        ["--config", str(path), "--date", "2026-07-28", "--dry-run"]
    )

    assert result == 1
    assert "error: synthetic auth failure" in capsys.readouterr().err


def test_main_short_circuits_existing_day_before_client(
    tmp_path, monkeypatch
):
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    export = (
        tmp_path / "output" / "exports" / "daily" / "2026" / "07"
    )
    export.mkdir(parents=True)
    (export / "2026-07-28.manifest.json").write_text(
        json.dumps({"status": "ok", "state_object_count": 1}),
        encoding="utf-8",
    )

    class ForbiddenClient:
        def __init__(self, **kwargs):
            raise AssertionError("client must not be constructed")

    monkeypatch.setattr(ha_client, "HomeAssistantClient", ForbiddenClient)

    assert cli.main(["--config", str(path), "--date", "2026-07-28"]) == 0


def test_main_applies_all_cli_overrides(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)
    captured = {}

    def fake_run_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_export", fake_run_export)
    outdir = tmp_path / "override-output"

    result = cli.main(
        [
            "--config",
            str(path),
            "--date",
            "2026-07-28",
            "--outdir",
            str(outdir),
            "--timezone",
            "Europe/Berlin",
            "--batch-size",
            "9",
            "--sleep-between-requests",
            "0.2",
            "--sleep-between-days",
            "0.3",
            "--timeout",
            "17",
            "--max-retries",
            "4",
            "--no-csv",
            "--jsonl",
            "--parquet",
            "--log-level",
            "DEBUG",
        ]
    )

    assert result == 0
    cfg = captured["cfg"]
    assert cfg.export.output_dir == str(outdir)
    assert cfg.home_assistant.timezone == "Europe/Berlin"
    assert cfg.requests.batch_size_entities == 9
    assert cfg.requests.sleep_between_requests_seconds == 0.2
    assert cfg.requests.sleep_between_days_seconds == 0.3
    assert cfg.requests.request_timeout_seconds == 17
    assert cfg.requests.max_retries == 4
    assert cfg.formats.jsonl
    assert not cfg.formats.csv
    assert cfg.formats.parquet


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (KeyboardInterrupt(), 130),
        (RuntimeError("synthetic unexpected failure"), 1),
    ],
)
def test_main_handles_top_level_failures(
    tmp_path, monkeypatch, error, expected_code
):
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    FakeCliClient.check_error = error
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert (
        cli.main(
            [
                "--config",
                str(path),
                "--date",
                "2026-07-28",
                "--dry-run",
            ]
        )
        == expected_code
    )


def test_main_runs_without_any_configuration_file(tmp_path, monkeypatch):
    """A fresh install must work from environment variables alone."""
    set_synthetic_env(monkeypatch)
    monkeypatch.setenv("HHE_EXPORT_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("HHE_STORAGE_TEMP_DIR", str(tmp_path / "temp"))
    monkeypatch.setenv("HHE_REQUESTS_SLEEP_BETWEEN_REQUESTS_SECONDS", "0")
    monkeypatch.setenv("HHE_REQUESTS_SLEEP_BETWEEN_DAYS_SECONDS", "0")
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["--date", "2026-07-28"]) == 0
    assert (
        tmp_path
        / "output"
        / "exports"
        / "daily"
        / "2026"
        / "07"
        / "2026-07-28.manifest.json"
    ).is_file()


def test_main_reports_the_resolved_output_directory(tmp_path, monkeypatch, caplog):
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    with caplog.at_level(logging.INFO):
        assert cli.main(["--config", str(path), "--date", "2026-07-28"]) == 0

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "Output directory:" in messages
    assert str(path) in messages


def test_cli_overrides_are_translated_into_configuration_keys():
    args = cli._parse_args(
        [
            "--date",
            "2026-07-28",
            "--outdir",
            "out",
            "--batch-size",
            "9",
            "--timeout",
            "17",
            "--no-csv",
            "--parquet",
        ]
    )

    assert cli.cli_overrides(args) == {
        "export.output_dir": "out",
        "requests.batch_size_entities": 9,
        "requests.request_timeout_seconds": 17,
        "formats.csv": False,
        "formats.parquet": True,
    }


def test_cli_overrides_stay_empty_when_no_option_is_given():
    args = cli._parse_args(["--date", "2026-07-28"])
    assert cli.cli_overrides(args) == {}


def test_entity_selection_narrows_the_requested_entities(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8")
        + "entity_selection:\n  include_unknown: false\n",
        encoding="utf-8",
    )
    set_synthetic_env(monkeypatch)
    FakeCliClient.states = [
        {"entity_id": "sensor.known", "state": "1", "attributes": {}},
        {"entity_id": "sensor.mystery", "state": "unknown", "attributes": {}},
    ]
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["--config", str(path), "--date", "2026-07-28"]) == 0

    instance = FakeCliClient.instances[0]
    requested = [
        entity
        for call in instance.history_calls
        for entity in call["entity_ids"]
    ]
    assert requested == ["sensor.known"]

    snapshots = list((tmp_path / "output" / "metadata").glob("entity_snapshot_*.json"))
    snapshot = json.loads(snapshots[0].read_text(encoding="utf-8"))
    assert snapshot["entity_count"] == 2
