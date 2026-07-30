from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from ha_history_exporter import cli, ha_client
from ha_history_exporter.exceptions import AuthError
from tests.helpers import state_row


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


class FakeCliClient:
    instances = []
    check_error = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.total_requests = 0
        self.total_retries = 0
        self.history_calls = []
        self.closed = False
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def check_api(self):
        if self.check_error is not None:
            raise self.check_error

    def get_states(self):
        return [
            {
                "entity_id": "sensor.synthetic",
                "state": "1",
                "attributes": {"friendly_name": "Synthetic"},
            }
        ]

    def get_history(self, **kwargs):
        self.history_calls.append(kwargs)
        self.total_requests += 1
        return [[state_row("sensor.synthetic", "1")]]


@pytest.fixture(autouse=True)
def reset_fake_client():
    FakeCliClient.instances = []
    FakeCliClient.check_error = None


@pytest.fixture(autouse=True)
def restore_root_logger():
    """Keep logging configured by one CLI invocation out of later tests."""
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level

    yield

    for handler in list(root_logger.handlers):
        if handler not in original_handlers:
            root_logger.removeHandler(handler)
            handler.close()
    root_logger.setLevel(original_level)


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


@pytest.mark.known_bug
@pytest.mark.xfail(
    strict=True,
    reason="logging initializes ZoneInfo before main's invalid-timezone handler",
)
def test_main_returns_two_for_invalid_timezone(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    text = path.read_text(encoding="utf-8").replace(
        "timezone: Europe/Berlin", "timezone: Invalid/Timezone"
    )
    path.write_text(text, encoding="utf-8")
    set_synthetic_env(monkeypatch)

    assert cli.main(["--config", str(path), "--date", "2026-07-28"]) == 2


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
    assert "Authentication failed" in capsys.readouterr().err


def test_main_short_circuits_existing_day_before_client(
    tmp_path, monkeypatch
):
    path = write_config(tmp_path)
    set_synthetic_env(monkeypatch)
    export = (
        tmp_path / "output" / "exports" / "daily" / "2026" / "07"
    )
    export.mkdir(parents=True)
    (export / "2026-07-28.jsonl").write_text("{}\n", encoding="utf-8")
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
