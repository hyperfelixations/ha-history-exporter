from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from ha_history_exporter.config import (
    AppConfig,
    ConfigError,
    FormatsConfig,
    default_output_dir,
    load_config,
    validate_config,
)

MINIMAL_CONFIG = """
home_assistant:
  url_env: HA_URL
  token_env: HA_TOKEN
  timezone: Europe/Berlin
export:
  output_dir: "./synthetic-output"
requests:
  batch_size_entities: 7
  sleep_between_requests_seconds: 0.25
formats:
  jsonl: true
  csv: false
  parquet: true
storage:
  temp_dir: "./synthetic-temp"
"""


def test_load_config_resolves_synthetic_environment(
    tmp_path, synthetic_ha_environment
):
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_CONFIG, encoding="utf-8")

    cfg = load_config(path)

    assert cfg.ha_url == "http://home-assistant.invalid"
    assert cfg.ha_token == "synthetic-test-token"
    assert cfg.home_assistant.timezone == "Europe/Berlin"
    assert cfg.requests.batch_size_entities == 7
    assert cfg.requests.sleep_between_requests_seconds == 0.25
    assert cfg.formats == FormatsConfig(jsonl=True, csv=False, parquet=True)


def test_load_config_strips_trailing_url_slash(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_CONFIG, encoding="utf-8")
    monkeypatch.setenv("HA_URL", "http://home-assistant.invalid///")
    monkeypatch.setenv("HA_TOKEN", "synthetic-test-token")

    assert load_config(path).ha_url == "http://home-assistant.invalid"


def test_load_config_rejects_missing_file():
    with pytest.raises(ConfigError, match="Config file not found"):
        load_config("does-not-exist.yaml")


def test_load_config_rejects_invalid_yaml(tmp_path, synthetic_ha_environment):
    path = tmp_path / "invalid.yaml"
    path.write_text("formats: [unterminated", encoding="utf-8")

    with pytest.raises(ConfigError, match="Invalid YAML"):
        load_config(path)


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("HA_URL", "No Home Assistant URL configured."),
        ("HA_TOKEN", "No Home Assistant access token configured."),
    ],
)
def test_load_config_requires_credentials(
    tmp_path, monkeypatch: pytest.MonkeyPatch, missing, message
):
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_CONFIG, encoding="utf-8")
    monkeypatch.setenv("HA_URL", "http://home-assistant.invalid")
    monkeypatch.setenv("HA_TOKEN", "synthetic-test-token")
    monkeypatch.delenv(missing)

    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config(path)


def test_app_config_builds_daily_paths(tmp_path):
    cfg = AppConfig()
    cfg.export.output_dir = str(tmp_path)

    day = date(2026, 7, 28)
    expected_dir = tmp_path / "exports" / "daily" / "2026" / "07"
    assert cfg.daily_export_root == tmp_path / "exports" / "daily"
    assert cfg.day_dir(day) == expected_dir
    assert cfg.day_dir("2026-07-28") == expected_dir
    assert cfg.day_file(day, "jsonl") == expected_dir / "2026-07-28.jsonl"
    assert cfg.metadata_dir == tmp_path / "metadata"
    assert cfg.logs_dir == tmp_path / "logs"


def test_default_output_directory_lives_in_the_user_home():
    """An installed tool must not write next to the current directory."""
    expected = Path.home() / "ha-history-exports"
    assert default_output_dir() == expected
    assert AppConfig().export.output_dir == str(expected)


def test_example_config_is_generic_and_loadable(
    monkeypatch: pytest.MonkeyPatch,
):
    example = Path(__file__).parents[1] / "export_config.example.yaml"
    text = example.read_text(encoding="utf-8")
    monkeypatch.setenv("HA_URL", "http://home-assistant.invalid")
    monkeypatch.setenv("HA_TOKEN", "synthetic-test-token")

    cfg = load_config(example)

    assert cfg.export.output_dir == str(default_output_dir())
    assert cfg.formats == FormatsConfig(jsonl=True, csv=False, parquet=False)
    assert cfg.ha_url.endswith(".invalid")
    assert "synthetic-test-token" not in text
    assert not re.search(r"[A-Za-z]:\\\\Users\\\\", text)
    assert "private-data" not in text


def test_missing_format_section_uses_dataclass_defaults(
    tmp_path, synthetic_ha_environment
):
    path = tmp_path / "config.yaml"
    path.write_text("home_assistant: {}\n", encoding="utf-8")

    cfg = load_config(path)

    assert cfg.formats == FormatsConfig()


def test_zero_batch_size_is_rejected(tmp_path, synthetic_ha_environment):
    path = tmp_path / "config.yaml"
    path.write_text(
        MINIMAL_CONFIG.replace("batch_size_entities: 7", "batch_size_entities: 0"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="batch_size_entities"):
        load_config(path)


def test_disabling_every_output_format_enables_snapshot_only_mode(
    tmp_path, synthetic_ha_environment
):
    path = tmp_path / "config.yaml"
    path.write_text(
        MINIMAL_CONFIG.replace(
            "jsonl: true\n  csv: false\n  parquet: true",
            "jsonl: false\n  csv: false\n  parquet: false",
        ),
        encoding="utf-8",
    )

    cfg = load_config(path)

    assert cfg.snapshot_only


@pytest.mark.parametrize(
    ("old", "new", "field_name"),
    [
        ("batch_size_entities: 7", "batch_size_entities: -1", "batch_size_entities"),
        (
            "sleep_between_requests_seconds: 0.25",
            "sleep_between_requests_seconds: -0.1",
            "sleep_between_requests_seconds",
        ),
        ("batch_size_entities: 7", "batch_size_entities: true", "batch_size_entities"),
    ],
)
def test_invalid_numeric_config_is_rejected(
    tmp_path, synthetic_ha_environment, old, new, field_name
):
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_CONFIG.replace(old, new), encoding="utf-8")

    with pytest.raises(ConfigError, match=field_name):
        load_config(path)


def test_retries_require_a_backoff_value(tmp_path, synthetic_ha_environment):
    path = tmp_path / "config.yaml"
    path.write_text(
        MINIMAL_CONFIG.replace(
            "sleep_between_requests_seconds: 0.25",
            "sleep_between_requests_seconds: 0.25\n"
            "  max_retries: 1\n"
            "  backoff_seconds: []",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="backoff_seconds"):
        load_config(path)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("requests", "sleep_between_requests_seconds", -1),
        ("requests", "sleep_between_days_seconds", float("nan")),
        ("requests", "request_timeout_seconds", 0),
        ("requests", "max_retries", -1),
        ("requests", "backoff_seconds", [-1]),
        ("storage", "cloud_storage_retry_count", -1),
        ("storage", "cloud_storage_retry_sleep_seconds", -1),
        ("recorder", "expected_purge_keep_days", 0),
    ],
)
def test_validate_config_rejects_invalid_numeric_ranges(
    section, field, value
):
    cfg = AppConfig()
    setattr(getattr(cfg, section), field, value)

    with pytest.raises(ConfigError, match=field):
        validate_config(cfg)
