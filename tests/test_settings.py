"""Tests for the layered configuration resolution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ha_history_exporter.errors import ConfigError, CredentialsError
from ha_history_exporter.settings import paths, resolve, schema, secrets
from ha_history_exporter.settings.schema import KeyStatus

SYNTHETIC_ENV = {
    "HA_URL": "http://home-assistant.invalid",
    "HA_TOKEN": "synthetic-test-token",
}


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ── paths ─────────────────────────────────────────────────────────────────────

def test_config_dir_follows_the_override(isolated_user_environment):
    assert paths.user_config_dir() == isolated_user_environment
    assert paths.user_config_file().name == "config.yaml"
    assert paths.user_credentials_file().name == "credentials.yaml"


def test_config_dir_without_override_is_platform_specific(monkeypatch):
    monkeypatch.delenv(paths.ENV_CONFIG_DIR, raising=False)
    resolved = paths.user_config_dir()
    assert resolved.name == "ha-history-exporter"
    assert resolved.is_absolute()


def test_default_output_directory_is_in_the_home_directory():
    assert paths.default_output_dir() == Path.home() / "ha-history-exports"


def test_project_candidates_prefer_the_new_name_over_the_legacy_one(tmp_path):
    candidates = paths.project_config_candidates(tmp_path)
    assert [c.name for c in candidates] == [
        "ha-history-exporter.yaml",
        "export_config.yaml",
    ]


# ── schema ────────────────────────────────────────────────────────────────────

def test_every_key_has_documentation_and_a_unique_environment_variable():
    env_vars = [key.env_var for key in schema.KEYS]
    assert len(env_vars) == len(set(env_vars))
    for key in schema.KEYS:
        assert key.doc.endswith("."), key.path
        assert key.env_var.startswith("HHE_")


def test_defaults_match_the_runtime_dataclasses():
    from ha_history_exporter.settings.model import AppConfig

    cfg = AppConfig()
    defaults = schema.defaults()
    assert defaults["requests.batch_size_entities"] == cfg.requests.batch_size_entities
    assert defaults["formats.jsonl"] == cfg.formats.jsonl
    assert defaults["formats.csv"] == cfg.formats.csv
    assert defaults["formats.parquet"] == cfg.formats.parquet
    assert defaults["export.output_dir"] == cfg.export.output_dir
    assert defaults["storage.temp_dir"] == cfg.storage.temp_dir


@pytest.mark.parametrize(
    "key_path",
    [
        "export.mode",
        "export.include_current_day",
        "storage.use_temp_dir",
        "recorder.export_long_term_statistics",
        "entity_selection.source",
        "entity_selection.include_deleted_from_previous_runs",
    ],
)
def test_default_only_keys_accept_the_default_and_reject_anything_else(key_path):
    key = schema.BY_PATH[key_path]
    assert key.status is KeyStatus.DEFAULT_ONLY
    assert key.coerce(key.default) == key.default

    other = "something-else" if isinstance(key.default, str) else not key.default
    with pytest.raises(ConfigError, match="does not support the value"):
        key.coerce(other)


def test_suggest_finds_a_close_key():
    assert schema.suggest("export.output_dri") == "export.output_dir"
    assert schema.suggest("completely-unrelated") is None


# ── file discovery and precedence ─────────────────────────────────────────────

def test_defaults_apply_without_any_configuration_file(tmp_path):
    settings = resolve(cwd=tmp_path, environ=SYNTHETIC_ENV)

    assert settings.config_files == ()
    assert settings.config.export.output_dir == str(paths.default_output_dir())
    assert settings.origin("requests.batch_size_entities") == "default"


def test_user_file_is_read_when_no_project_file_exists(
    tmp_path, isolated_user_environment
):
    write(
        paths.user_config_file(),
        "requests:\n  batch_size_entities: 11\n",
    )

    settings = resolve(cwd=tmp_path, environ=SYNTHETIC_ENV)

    assert settings.config.requests.batch_size_entities == 11
    assert settings.origin("requests.batch_size_entities").startswith("file:")


def test_project_file_overrides_the_user_file(tmp_path, isolated_user_environment):
    write(paths.user_config_file(), "requests:\n  batch_size_entities: 11\n")
    write(tmp_path / "ha-history-exporter.yaml", "requests:\n  batch_size_entities: 22\n")

    settings = resolve(cwd=tmp_path, environ=SYNTHETIC_ENV)

    assert settings.config.requests.batch_size_entities == 22
    assert len(settings.config_files) == 2


def test_legacy_project_filename_is_still_discovered(tmp_path):
    write(tmp_path / "export_config.yaml", "requests:\n  batch_size_entities: 33\n")

    settings = resolve(cwd=tmp_path, environ=SYNTHETIC_ENV)

    assert settings.config.requests.batch_size_entities == 33
    assert settings.config_files[0].name == "export_config.yaml"


def test_new_project_filename_wins_over_the_legacy_one(tmp_path):
    write(tmp_path / "ha-history-exporter.yaml", "requests:\n  batch_size_entities: 1\n")
    write(tmp_path / "export_config.yaml", "requests:\n  batch_size_entities: 2\n")

    settings = resolve(cwd=tmp_path, environ=SYNTHETIC_ENV)

    assert settings.config.requests.batch_size_entities == 1


def test_explicit_config_suppresses_discovery(tmp_path, isolated_user_environment):
    write(paths.user_config_file(), "requests:\n  batch_size_entities: 11\n")
    write(tmp_path / "export_config.yaml", "requests:\n  max_retries: 9\n")
    explicit = write(tmp_path / "explicit.yaml", "requests:\n  batch_size_entities: 44\n")

    settings = resolve(
        explicit_config=explicit, cwd=tmp_path, environ=SYNTHETIC_ENV
    )

    assert settings.config_files == (explicit,)
    assert settings.config.requests.batch_size_entities == 44
    assert settings.config.requests.max_retries == 3  # default, not the project file


def test_missing_explicit_config_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="Config file not found"):
        resolve(explicit_config=tmp_path / "absent.yaml", environ=SYNTHETIC_ENV)


def test_environment_overrides_files(tmp_path):
    write(tmp_path / "export_config.yaml", "requests:\n  batch_size_entities: 33\n")

    settings = resolve(
        cwd=tmp_path,
        environ={**SYNTHETIC_ENV, "HHE_REQUESTS_BATCH_SIZE_ENTITIES": "55"},
    )

    assert settings.config.requests.batch_size_entities == 55
    assert settings.origin("requests.batch_size_entities") == (
        "env:HHE_REQUESTS_BATCH_SIZE_ENTITIES"
    )


def test_command_line_overrides_everything(tmp_path):
    write(tmp_path / "export_config.yaml", "requests:\n  batch_size_entities: 33\n")

    settings = resolve(
        cwd=tmp_path,
        environ={**SYNTHETIC_ENV, "HHE_REQUESTS_BATCH_SIZE_ENTITIES": "55"},
        cli_overrides={"requests.batch_size_entities": 77},
    )

    assert settings.config.requests.batch_size_entities == 77
    assert settings.origin("requests.batch_size_entities") == "cli"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("false", False), ("1", True), ("0", False), ("YES", True)],
)
def test_boolean_environment_values(tmp_path, raw, expected):
    settings = resolve(
        cwd=tmp_path, environ={**SYNTHETIC_ENV, "HHE_FORMATS_PARQUET": raw}
    )
    assert settings.config.formats.parquet is expected


def test_invalid_boolean_environment_value_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="HHE_FORMATS_PARQUET"):
        resolve(cwd=tmp_path, environ={**SYNTHETIC_ENV, "HHE_FORMATS_PARQUET": "maybe"})


def test_list_environment_values_are_split(tmp_path):
    settings = resolve(
        cwd=tmp_path,
        environ={
            **SYNTHETIC_ENV,
            "HHE_ENTITY_SELECTION_OPTIONAL_EXCLUDE_DOMAINS": "update, button",
            "HHE_REQUESTS_BACKOFF_SECONDS": "1,2,3",
        },
    )
    assert settings.config.entity_selection.optional_exclude_domains == [
        "update",
        "button",
    ]
    assert settings.config.requests.backoff_seconds == [1.0, 2.0, 3.0]


# ── validation ────────────────────────────────────────────────────────────────

def test_unknown_key_names_the_file_the_line_and_a_suggestion(tmp_path):
    write(
        tmp_path / "export_config.yaml",
        "export:\n  output_dri: /tmp/x\n",
    )

    with pytest.raises(ConfigError) as exc:
        resolve(cwd=tmp_path, environ=SYNTHETIC_ENV)

    assert "Unknown configuration key 'export.output_dri'" in exc.value.summary
    assert "line 2" in exc.value.summary
    assert "export.output_dir" in (exc.value.details or "")


def test_unknown_section_is_rejected(tmp_path):
    write(tmp_path / "export_config.yaml", "exprot:\n  output_dir: /tmp/x\n")

    with pytest.raises(ConfigError, match="Unknown configuration key 'exprot'"):
        resolve(cwd=tmp_path, environ=SYNTHETIC_ENV)


def test_section_that_is_not_a_mapping_is_rejected(tmp_path):
    write(tmp_path / "export_config.yaml", "export: 5\n")

    with pytest.raises(ConfigError, match="must be a mapping"):
        resolve(cwd=tmp_path, environ=SYNTHETIC_ENV)


def test_empty_section_is_accepted(tmp_path):
    write(tmp_path / "export_config.yaml", "export:\nformats:\n")

    settings = resolve(cwd=tmp_path, environ=SYNTHETIC_ENV)

    assert settings.config.formats.jsonl is True


def test_invalid_yaml_is_reported_with_the_file(tmp_path):
    write(tmp_path / "export_config.yaml", "formats: [unterminated\n")

    with pytest.raises(ConfigError, match="Invalid YAML"):
        resolve(cwd=tmp_path, environ=SYNTHETIC_ENV)


def test_unsupported_value_for_a_default_only_key_is_rejected(tmp_path):
    write(tmp_path / "export_config.yaml", "export:\n  include_current_day: true\n")

    with pytest.raises(ConfigError, match="export.include_current_day"):
        resolve(cwd=tmp_path, environ=SYNTHETIC_ENV)


def test_default_only_keys_at_their_default_still_load(tmp_path):
    """The production configuration lists these keys at their defaults."""
    write(
        tmp_path / "export_config.yaml",
        "export:\n"
        "  mode: all_current_entities\n"
        "  include_current_day: false\n"
        "storage:\n"
        "  use_temp_dir: true\n"
        "recorder:\n"
        "  export_long_term_statistics: false\n"
        "entity_selection:\n"
        "  source: api_states_runtime\n"
        "  include_deleted_from_previous_runs: false\n",
    )

    settings = resolve(cwd=tmp_path, environ=SYNTHETIC_ENV)

    assert settings.config.export.mode == "all_current_entities"


# ── credentials ───────────────────────────────────────────────────────────────

def test_url_and_token_come_from_the_environment(tmp_path):
    settings = resolve(cwd=tmp_path, environ=SYNTHETIC_ENV)

    assert settings.config.ha_url == "http://home-assistant.invalid"
    assert settings.config.ha_token == "synthetic-test-token"
    assert settings.origin("homeassistant.url") == "env:HA_URL"
    assert settings.origin("homeassistant.token") == "env:HA_TOKEN"


def test_url_falls_back_to_the_configuration_file(tmp_path):
    write(
        tmp_path / "export_config.yaml",
        "homeassistant:\n  url: http://home-assistant.invalid:8123/\n",
    )

    settings = resolve(cwd=tmp_path, environ={"HA_TOKEN": "synthetic-test-token"})

    assert settings.config.ha_url == "http://home-assistant.invalid:8123"


def test_token_falls_back_to_the_credentials_file(tmp_path, isolated_user_environment):
    secrets.write_token("synthetic-stored-token")

    settings = resolve(
        cwd=tmp_path, environ={"HA_URL": "http://home-assistant.invalid"}
    )

    assert settings.config.ha_token == "synthetic-stored-token"
    assert settings.origin("homeassistant.token").startswith("file:")


def test_environment_token_wins_over_the_credentials_file(
    tmp_path, isolated_user_environment
):
    secrets.write_token("synthetic-stored-token")

    settings = resolve(cwd=tmp_path, environ=SYNTHETIC_ENV)

    assert settings.config.ha_token == "synthetic-test-token"


def test_missing_url_is_reported_with_remedies(tmp_path):
    with pytest.raises(CredentialsError) as exc:
        resolve(cwd=tmp_path, environ={"HA_TOKEN": "synthetic-test-token"})

    assert exc.value.summary == "No Home Assistant URL configured."
    assert any(remedy.command for remedy in exc.value.remedies)
    assert exc.value.exit_code == 2


def test_missing_token_is_reported_with_remedies(tmp_path):
    with pytest.raises(CredentialsError) as exc:
        resolve(cwd=tmp_path, environ={"HA_URL": "http://home-assistant.invalid"})

    assert exc.value.summary == "No Home Assistant access token configured."
    assert any(remedy.command for remedy in exc.value.remedies)


def test_credentials_may_be_optional_for_read_only_commands(tmp_path):
    settings = resolve(cwd=tmp_path, environ={}, require_credentials=False)

    assert settings.config.ha_url == ""
    assert settings.values["homeassistant.token"] == "<not set>"


def test_resolved_values_never_expose_the_token(tmp_path):
    settings = resolve(cwd=tmp_path, environ=SYNTHETIC_ENV)

    assert "synthetic-test-token" not in repr(settings.values)
    assert settings.values["homeassistant.token"] == "<set>"


# ── secrets ───────────────────────────────────────────────────────────────────

def test_token_round_trip(isolated_user_environment):
    assert secrets.read_token() is None

    path = secrets.write_token("synthetic-stored-token")

    assert path == paths.user_credentials_file()
    assert secrets.read_token() == "synthetic-stored-token"
    assert "synthetic-stored-token" in path.read_text(encoding="utf-8")


def test_token_can_be_replaced_and_cleared(isolated_user_environment):
    secrets.write_token("first-synthetic")
    secrets.write_token("second-synthetic")
    assert secrets.read_token() == "second-synthetic"

    assert secrets.clear_token() is True
    assert secrets.read_token() is None
    assert secrets.clear_token() is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
def test_credentials_file_is_owner_only(isolated_user_environment):
    path = secrets.write_token("synthetic-stored-token")
    assert secrets.is_owner_only(path) is True


def test_blank_stored_token_counts_as_unset(isolated_user_environment):
    secrets.write_token("   ")
    assert secrets.read_token() is None


def test_corrupt_credentials_file_is_reported_without_its_content(
    isolated_user_environment,
):
    path = paths.user_credentials_file()
    path.write_text("homeassistant: [unterminated\n", encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        secrets.read_token()

    assert "unterminated" not in exc.value.summary
    assert str(path) in (exc.value.context or {}).values()


# ── rejection paths ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("key_path", "value", "message"),
    [
        ("requests.batch_size_entities", "many", "must be an integer"),
        ("requests.max_retries", -1, "greater than or equal to 0"),
        ("requests.sleep_between_days_seconds", "slow", "must be a number"),
        ("requests.backoff_seconds", 5, "must be a list of numbers"),
        ("entity_selection.optional_exclude_domains", "update", "list of strings"),
        ("entity_selection.optional_exclude_patterns", [1], "list of strings"),
        ("formats.jsonl", "yes", "must be true or false"),
        ("home_assistant.timezone", 42, "must be a string"),
        ("recorder.expected_purge_keep_days", 0, "greater than 0"),
    ],
)
def test_invalid_values_are_rejected_per_key(key_path, value, message):
    with pytest.raises(ConfigError, match=message):
        schema.BY_PATH[key_path].coerce(value)


def test_optional_integer_accepts_null():
    key = schema.BY_PATH["recorder.expected_purge_keep_days"]
    assert key.coerce(None) is None
    assert key.coerce(14) == 14


def test_optional_integer_environment_value_may_be_null(tmp_path):
    settings = resolve(
        cwd=tmp_path,
        environ={**SYNTHETIC_ENV, "HHE_RECORDER_EXPECTED_PURGE_KEEP_DAYS": "null"},
    )
    assert settings.config.recorder.expected_purge_keep_days is None


def test_key_helpers_expose_section_and_name():
    key = schema.BY_PATH["requests.batch_size_entities"]
    assert key.section == "requests"
    assert key.name == "batch_size_entities"
    assert key.env_var == "HHE_REQUESTS_BATCH_SIZE_ENTITIES"


def test_unreadable_configuration_file_is_reported(tmp_path):
    from ha_history_exporter.settings import sources

    directory = tmp_path / "not-a-file.yaml"
    directory.mkdir()

    with pytest.raises(ConfigError, match="Cannot read configuration file"):
        sources.file_entries(directory)


def test_top_level_scalar_configuration_is_rejected(tmp_path):
    write(tmp_path / "export_config.yaml", "just-a-string\n")

    with pytest.raises(ConfigError, match="top level must be a mapping"):
        resolve(cwd=tmp_path, environ=SYNTHETIC_ENV)


def test_credentials_file_without_a_token_section(isolated_user_environment):
    paths.user_credentials_file().write_text("other: value\n", encoding="utf-8")
    assert secrets.read_token() is None


def test_credentials_file_with_a_non_mapping_is_rejected(isolated_user_environment):
    paths.user_credentials_file().write_text("- a\n- b\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="does not contain a mapping"):
        secrets.read_token()


def test_credentials_file_with_a_non_string_token_is_rejected(
    isolated_user_environment,
):
    paths.user_credentials_file().write_text(
        "homeassistant:\n  token: 12345\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="must be a string"):
        secrets.read_token()


def test_credentials_path_accepts_an_explicit_directory(tmp_path):
    assert secrets.credentials_path(tmp_path) == tmp_path / "credentials.yaml"


def test_write_token_cleans_up_a_stale_temporary_file(isolated_user_environment):
    stale = isolated_user_environment / "credentials.yaml.tmp"
    stale.write_text("leftover", encoding="utf-8")

    secrets.write_token("synthetic-stored-token")

    assert not stale.exists()
    assert secrets.read_token() == "synthetic-stored-token"


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL behaviour")
def test_owner_only_check_is_not_applicable_on_windows(isolated_user_environment):
    path = secrets.write_token("synthetic-stored-token")
    assert secrets.is_owner_only(path) is None
