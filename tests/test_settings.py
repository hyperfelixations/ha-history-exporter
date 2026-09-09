"""Configuration resolution: paths, registry, precedence, credentials, secrets.

Four sources, highest precedence first: command line, environment, the one
configuration file, built-in defaults. There is no discovery in the working
directory, so nothing here depends on where a test happens to run.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from ha_history_exporter.errors import ConfigError, CredentialsError
from ha_history_exporter.settings import (
    Config,
    Format,
    paths,
    resolve,
    schema,
    secrets,
)
from ha_history_exporter.settings.schema import KeyStatus

SYNTHETIC_ENV = {
    "HA_URL": "http://home-assistant.invalid",
    "HA_TOKEN": "synthetic-test-token",
}


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def user_config(config_dir: Path, text: str) -> Path:
    return write(config_dir / paths.CONFIG_FILENAME, text)


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


#: Keys whose registry default cannot be a plain copy of a model field.
DEFAULTS_WITHOUT_A_MODEL_FIELD = {
    # Resolved from the platform at load time, not a fixed value.
    "export.output_dir",
    "storage.temp_dir",
    # The token never becomes part of the configuration object.
    "homeassistant.token",
}


def test_key_registry_defaults_match_the_configuration_model():
    """One default per key.

    The registry and the dataclasses both state defaults; declaring a value in
    two places invites them to drift apart unnoticed.
    """
    config = Config()

    for key in schema.KEYS:
        if key.path in DEFAULTS_WITHOUT_A_MODEL_FIELD:
            continue
        expected = getattr(getattr(config, key.section), key.name)
        assert key.coerce(key.default) == expected, key.path


def test_request_defaults_follow_the_sizing_proven_in_production():
    requests = Config().requests
    assert requests.batch_size == 15
    assert requests.sleep_between_requests == 0.7


def test_default_output_directory_is_in_the_home_directory():
    assert paths.default_output_dir() == Path.home() / "ha-history-exports"


@pytest.mark.parametrize("lookup_result", ["", ".", "./ha-history-exporter"])
def test_config_dir_rejects_a_relative_platform_answer(
    monkeypatch, tmp_path, lookup_result
):
    """platformdirs does not check the Windows known-folder call.

    An empty answer becomes '.', and the configuration directory would then
    depend on the working directory the tool happened to start in.
    """
    monkeypatch.delenv(paths.ENV_CONFIG_DIR, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setattr(
        paths.platformdirs, "user_config_dir", lambda *a, **k: lookup_result
    )

    resolved = paths.user_config_dir()

    assert resolved.is_absolute()
    assert resolved == tmp_path / "roaming" / paths.APP_NAME


def test_config_dir_falls_back_to_the_home_directory_as_a_last_resort(
    monkeypatch, tmp_path
):
    monkeypatch.delenv(paths.ENV_CONFIG_DIR, raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(paths.platformdirs, "user_config_dir", lambda *a, **k: "")

    resolved = paths.user_config_dir()

    assert resolved.is_absolute()
    assert resolved == Path.home() / ".config" / paths.APP_NAME


def test_no_configuration_is_discovered_in_the_working_directory(
    tmp_path, monkeypatch
):
    """A file lying in the current directory must have no effect at all."""
    for name in ("config.yaml", "ha-history-exporter.yaml", "export_config.yaml"):
        write(tmp_path / name, "requests:\n  batch_size: 99\n")
    monkeypatch.chdir(tmp_path)

    settings = resolve(environ=SYNTHETIC_ENV)

    assert settings.config.requests.batch_size == 15
    assert settings.config_file is None


# ── schema ────────────────────────────────────────────────────────────────────

def test_every_key_has_documentation_and_a_unique_environment_variable():
    seen: set[str] = set()
    for key in schema.KEYS:
        assert key.doc.strip(), f"{key.path} has no documentation"
        assert key.doc.endswith("."), f"{key.path} documentation is not a sentence"
        assert key.env_var not in seen, f"{key.env_var} is used twice"
        seen.add(key.env_var)


def test_key_paths_are_unique_and_two_levels_deep():
    for key in schema.KEYS:
        assert key.path.count(".") == 1, key.path
    assert len({key.path for key in schema.KEYS}) == len(schema.KEYS)


def test_the_credential_keys_use_short_environment_variables():
    """HHE_HOMEASSISTANT_URL would be clumsy; these two are named explicitly."""
    assert schema.BY_PATH["homeassistant.url"].env_var == "HHE_URL"
    assert schema.BY_PATH["homeassistant.token"].env_var == "HHE_TOKEN"


def test_only_the_token_is_a_secret():
    secrets_found = [
        key.path for key in schema.KEYS if key.status is KeyStatus.SECRET
    ]
    assert secrets_found == ["homeassistant.token"]


def test_defaults_match_the_runtime_configuration():
    settings = resolve(environ=SYNTHETIC_ENV, require_credentials=False)
    cfg = settings.config
    assert cfg.export.timezone == "Europe/Berlin"
    assert cfg.export.formats == frozenset({Format.JSONL})
    assert cfg.requests.batch_size == 15
    assert cfg.requests.sleep_between_requests == 0.7
    assert cfg.requests.sleep_between_days == 5.0
    assert cfg.requests.timeout == 120
    assert cfg.requests.max_retries == 3
    assert cfg.requests.backoff == (2.0, 5.0, 15.0)
    assert cfg.entities.include_unknown is True
    assert cfg.entities.include_unavailable is True
    assert cfg.entities.exclude_domains == ()
    assert cfg.recorder.purge_keep_days is None
    assert cfg.storage.locked_file_retries == 5


def test_suggest_finds_a_close_key():
    assert schema.suggest("requests.batch_siz") == "requests.batch_size"
    assert schema.suggest("nothing.like.a.key") is None


def test_the_configuration_object_cannot_be_changed_after_it_is_built():
    from dataclasses import FrozenInstanceError

    settings = resolve(environ=SYNTHETIC_ENV, require_credentials=False)
    with pytest.raises(FrozenInstanceError):
        settings.config.export.output_dir = "elsewhere"  # type: ignore[misc]


# ── output formats ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("jsonl", {Format.JSONL}),
        ("jsonl,parquet", {Format.JSONL, Format.PARQUET}),
        (" JSONL , CSV ", {Format.JSONL, Format.CSV}),
        ("none", set()),
        ([], set()),
        (["csv"], {Format.CSV}),
    ],
)
def test_format_values_are_accepted_in_every_written_form(raw, expected):
    assert schema.format_set(raw, "export.formats") == frozenset(expected)


def test_an_unknown_format_is_rejected_with_the_valid_ones():
    with pytest.raises(ConfigError) as exc:
        schema.format_set("jsonl,xml", "export.formats")
    assert "xml" in exc.value.summary
    assert "jsonl, csv, parquet" in (exc.value.details or "")


def test_none_cannot_be_combined_with_a_format():
    with pytest.raises(ConfigError, match="cannot combine 'none'"):
        schema.format_set("none,csv", "export.formats")


def test_no_format_means_snapshot_only(tmp_path, isolated_user_environment):
    user_config(isolated_user_environment, "export:\n  formats: []\n")
    settings = resolve(environ=SYNTHETIC_ENV)
    assert settings.config.export.formats == frozenset()
    assert settings.config.snapshot_only is True


def test_formats_from_a_file_reach_the_configuration(isolated_user_environment):
    user_config(isolated_user_environment, "export:\n  formats: [jsonl, parquet]\n")
    cfg = resolve(environ=SYNTHETIC_ENV).config
    assert cfg.wants(Format.JSONL)
    assert cfg.wants(Format.PARQUET)
    assert not cfg.wants(Format.CSV)


# ── precedence ────────────────────────────────────────────────────────────────

def test_defaults_apply_without_any_configuration_file(tmp_path):
    settings = resolve(environ=SYNTHETIC_ENV)
    assert settings.config_file is None
    assert settings.config.requests.batch_size == 15
    assert settings.origin("requests.batch_size") == "default"


def test_the_user_file_is_read(isolated_user_environment):
    path = user_config(isolated_user_environment, "requests:\n  batch_size: 11\n")
    settings = resolve(environ=SYNTHETIC_ENV)
    assert settings.config.requests.batch_size == 11
    assert settings.config_file == path
    assert settings.origin("requests.batch_size") == f"file:{path}"


def test_explicit_config_replaces_the_user_file(tmp_path, isolated_user_environment):
    user_config(isolated_user_environment, "requests:\n  batch_size: 11\n")
    explicit = write(tmp_path / "other.yaml", "requests:\n  timeout: 42\n")

    settings = resolve(explicit_config=explicit, environ=SYNTHETIC_ENV)

    assert settings.config.requests.timeout == 42
    assert settings.config.requests.batch_size == 15  # the user file was not read
    assert settings.config_file == explicit


def test_missing_explicit_config_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="Config file not found"):
        resolve(explicit_config=tmp_path / "absent.yaml", environ=SYNTHETIC_ENV)


def test_environment_overrides_the_file(isolated_user_environment):
    user_config(isolated_user_environment, "requests:\n  batch_size: 11\n")
    environ = {**SYNTHETIC_ENV, "HHE_REQUESTS_BATCH_SIZE": "23"}

    settings = resolve(environ=environ)

    assert settings.config.requests.batch_size == 23
    assert settings.origin("requests.batch_size") == "env:HHE_REQUESTS_BATCH_SIZE"


def test_the_command_line_overrides_everything(isolated_user_environment):
    user_config(isolated_user_environment, "requests:\n  batch_size: 11\n")
    environ = {**SYNTHETIC_ENV, "HHE_REQUESTS_BATCH_SIZE": "23"}

    settings = resolve(
        cli_overrides={"requests.batch_size": 7}, environ=environ
    )

    assert settings.config.requests.batch_size == 7
    assert settings.origin("requests.batch_size") == "cli"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("false", False), ("1", True), ("0", False), ("YES", True)],
)
def test_boolean_environment_values(raw, expected):
    environ = {**SYNTHETIC_ENV, "HHE_ENTITIES_INCLUDE_UNKNOWN": raw}
    settings = resolve(environ=environ)
    assert settings.config.entities.include_unknown is expected


def test_invalid_boolean_environment_value_is_rejected():
    environ = {**SYNTHETIC_ENV, "HHE_ENTITIES_INCLUDE_UNKNOWN": "maybe"}
    with pytest.raises(ConfigError, match=r"entities\.include_unknown"):
        resolve(environ=environ)


def test_list_environment_values_are_split():
    environ = {
        **SYNTHETIC_ENV,
        "HHE_ENTITIES_EXCLUDE_DOMAINS": "update, button",
        "HHE_REQUESTS_BACKOFF": "1,2,3",
        "HHE_EXPORT_FORMATS": "jsonl,csv",
    }
    cfg = resolve(environ=environ).config
    assert cfg.entities.exclude_domains == ("update", "button")
    assert cfg.requests.backoff == (1.0, 2.0, 3.0)
    assert cfg.export.formats == frozenset({Format.JSONL, Format.CSV})


def test_optional_integer_environment_value_may_be_null():
    environ = {**SYNTHETIC_ENV, "HHE_RECORDER_PURGE_KEEP_DAYS": "null"}
    assert resolve(environ=environ).config.recorder.purge_keep_days is None


# ── validation ────────────────────────────────────────────────────────────────

def test_unknown_key_names_the_file_the_line_and_a_suggestion(
    isolated_user_environment,
):
    user_config(
        isolated_user_environment,
        "requests:\n  batch_size: 5\n  batch_siz: 7\n",
    )
    with pytest.raises(ConfigError) as exc:
        resolve(environ=SYNTHETIC_ENV)

    assert "requests.batch_siz" in exc.value.summary
    assert "line 3" in exc.value.summary
    assert "requests.batch_size" in (exc.value.details or "")


def test_a_retired_key_is_reported_as_unknown(isolated_user_environment):
    """Old spellings simply no longer exist; HHE was never published with them."""
    user_config(isolated_user_environment, "formats:\n  jsonl: true\n")
    with pytest.raises(ConfigError, match="Unknown configuration key 'formats'"):
        resolve(environ=SYNTHETIC_ENV)


def test_unknown_section_is_rejected(isolated_user_environment):
    user_config(isolated_user_environment, "nonsense:\n  key: 1\n")
    with pytest.raises(ConfigError, match="Unknown configuration key 'nonsense'"):
        resolve(environ=SYNTHETIC_ENV)


def test_section_that_is_not_a_mapping_is_rejected(isolated_user_environment):
    user_config(isolated_user_environment, "requests: 5\n")
    with pytest.raises(ConfigError, match="must be a mapping"):
        resolve(environ=SYNTHETIC_ENV)


def test_empty_section_is_accepted(isolated_user_environment):
    user_config(isolated_user_environment, "requests:\n")
    assert resolve(environ=SYNTHETIC_ENV).config.requests.batch_size == 15


def test_invalid_yaml_is_reported_with_the_file(isolated_user_environment):
    path = user_config(isolated_user_environment, "requests:\n  - [\n")
    with pytest.raises(ConfigError) as exc:
        resolve(environ=SYNTHETIC_ENV)
    assert str(path) in exc.value.summary


def test_top_level_scalar_configuration_is_rejected(isolated_user_environment):
    user_config(isolated_user_environment, "just-a-string\n")
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        resolve(environ=SYNTHETIC_ENV)


def test_a_directory_named_as_the_config_file_is_reported(tmp_path):
    directory = tmp_path / "not-a-file.yaml"
    directory.mkdir()
    with pytest.raises(ConfigError, match="Config file not found"):
        resolve(explicit_config=directory, environ=SYNTHETIC_ENV)


def test_a_user_configuration_that_cannot_be_examined_is_an_error(
    isolated_user_environment, monkeypatch
):
    """"Cannot read it" must never be treated as "it is not there".

    The silent fallback to built-in defaults would send the export to a
    different output directory, where no manifest exists and every day looks
    unexported.
    """
    user_config(isolated_user_environment, "requests:\n  batch_size: 3\n")
    target = paths.user_config_file()
    real_stat = Path.stat

    def refuse(self, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "Access is denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", refuse)

    with pytest.raises(ConfigError, match="Cannot examine configuration file"):
        resolve(environ=SYNTHETIC_ENV)


def test_an_unreadable_file_is_reported_with_its_path(tmp_path):
    from ha_history_exporter.settings import sources

    directory = tmp_path / "unreadable.yaml"
    directory.mkdir()
    with pytest.raises(ConfigError, match="Cannot read configuration file"):
        sources.file_entries(directory)


@pytest.mark.parametrize(
    ("key_path", "value", "message"),
    [
        ("requests.batch_size", 0, "must be greater than 0"),
        ("requests.batch_size", "five", "must be an integer"),
        ("requests.max_retries", -1, "greater than or equal to 0"),
        ("requests.sleep_between_days", "slow", "must be a number"),
        ("requests.backoff", 5, "must be a list of numbers"),
        ("entities.exclude_domains", "update", "must be a list of strings"),
        ("entities.include_unknown", "yes", "must be true or false"),
        ("export.timezone", 5, "must be a string"),
        ("recorder.purge_keep_days", 0, "must be greater than 0"),
    ],
)
def test_invalid_values_are_rejected_per_key(key_path, value, message):
    with pytest.raises(ConfigError, match=message):
        schema.BY_PATH[key_path].coerce(value)


def test_optional_integer_accepts_null():
    assert schema.BY_PATH["recorder.purge_keep_days"].coerce(None) is None


def test_retries_require_a_backoff_schedule(isolated_user_environment):
    """A rule that spans two keys, and therefore lives outside the registry."""
    user_config(
        isolated_user_environment,
        "requests:\n  max_retries: 3\n  backoff: []\n",
    )
    with pytest.raises(ConfigError) as exc:
        resolve(environ=SYNTHETIC_ENV)
    assert "requests.backoff" in exc.value.summary
    assert any("max_retries 0" in (r.command or "") for r in exc.value.remedies)


def test_key_helpers_expose_section_and_name():
    key = schema.BY_PATH["requests.batch_size"]
    assert (key.section, key.name) == ("requests", "batch_size")


# ── credentials ───────────────────────────────────────────────────────────────

def test_url_and_token_come_from_the_legacy_environment_variables():
    settings = resolve(environ=SYNTHETIC_ENV)
    assert settings.config.homeassistant.url == "http://home-assistant.invalid"
    assert settings.token == "synthetic-test-token"
    assert settings.origin("homeassistant.url") == "env:HA_URL"
    assert settings.origin("homeassistant.token") == "env:HA_TOKEN"


def test_the_new_environment_variable_names_work_too():
    environ = {
        "HHE_URL": "http://home-assistant.invalid",
        "HHE_TOKEN": "synthetic-test-token",
    }
    settings = resolve(environ=environ)
    assert settings.config.homeassistant.url == "http://home-assistant.invalid"
    assert settings.token == "synthetic-test-token"
    assert settings.origin("homeassistant.url") == "env:HHE_URL"


def test_the_legacy_names_win_when_both_are_set():
    """Existing machines have HA_URL configured; that setup must not shift."""
    environ = {
        "HHE_URL": "http://new.invalid",
        "HA_URL": "http://home-assistant.invalid",
        "HA_TOKEN": "synthetic-test-token",
    }
    settings = resolve(environ=environ)
    assert settings.config.homeassistant.url == "http://home-assistant.invalid"
    assert settings.origin("homeassistant.url") == "env:HA_URL"


def test_a_trailing_slash_is_stripped_from_the_url():
    environ = {**SYNTHETIC_ENV, "HA_URL": "http://home-assistant.invalid/"}
    assert resolve(environ=environ).config.homeassistant.url.endswith("invalid")


def test_url_falls_back_to_the_configuration_file(isolated_user_environment):
    user_config(
        isolated_user_environment,
        'homeassistant:\n  url: "http://home-assistant.invalid"\n',
    )
    settings = resolve(environ={"HA_TOKEN": "synthetic-test-token"})
    assert settings.config.homeassistant.url == "http://home-assistant.invalid"


def test_token_falls_back_to_the_credentials_file(isolated_user_environment):
    secrets.write_token("stored-synthetic-token")
    settings = resolve(environ={"HA_URL": "http://home-assistant.invalid"})
    assert settings.token == "stored-synthetic-token"
    assert settings.origin("homeassistant.token").startswith("file:")


def test_environment_token_wins_over_the_credentials_file(
    isolated_user_environment,
):
    secrets.write_token("stored-synthetic-token")
    settings = resolve(environ=SYNTHETIC_ENV)
    assert settings.token == "synthetic-test-token"


def test_a_token_in_the_configuration_file_is_refused(isolated_user_environment):
    user_config(
        isolated_user_environment,
        'homeassistant:\n  token: "synthetic-test-token"\n',
    )
    with pytest.raises(ConfigError) as exc:
        resolve(environ=SYNTHETIC_ENV)
    assert "must not be stored" in exc.value.summary
    assert "synthetic-test-token" not in str(exc.value.summary)


def test_missing_url_is_reported_with_remedies():
    with pytest.raises(CredentialsError) as exc:
        resolve(environ={})
    assert exc.value.exit_code == 2
    assert any(r.command == "hhe init" for r in exc.value.remedies)


def test_missing_token_is_reported_with_remedies():
    with pytest.raises(CredentialsError) as exc:
        resolve(environ={"HA_URL": "http://home-assistant.invalid"})
    assert "access token" in exc.value.summary


def test_credentials_may_be_optional_for_read_only_commands():
    settings = resolve(environ={}, require_credentials=False)
    assert settings.config.homeassistant.url == ""
    assert settings.token == ""


def test_resolved_values_never_expose_the_token():
    settings = resolve(environ=SYNTHETIC_ENV)
    assert settings.values["homeassistant.token"] == "<set>"
    assert "synthetic-test-token" not in repr(settings.values)
    assert "synthetic-test-token" not in repr(settings.config)


def test_an_absent_token_is_reported_as_not_set():
    settings = resolve(environ={}, require_credentials=False)
    assert settings.values["homeassistant.token"] == "<not set>"


# ── secrets ───────────────────────────────────────────────────────────────────

def test_token_round_trip(isolated_user_environment):
    path = secrets.write_token("synthetic-round-trip")
    assert path == paths.user_credentials_file()
    assert secrets.read_token() == "synthetic-round-trip"
    assert "synthetic-round-trip" in path.read_text(encoding="utf-8")


def test_token_can_be_replaced_and_cleared(isolated_user_environment):
    secrets.write_token("first-synthetic")
    secrets.write_token("second-synthetic")
    assert secrets.read_token() == "second-synthetic"
    assert secrets.clear_token() is True
    assert secrets.read_token() is None
    assert secrets.clear_token() is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
def test_credentials_file_is_owner_only(isolated_user_environment):
    path = secrets.write_token("synthetic-mode-check")
    assert path.stat().st_mode & 0o077 == 0
    assert secrets.is_owner_only(path) is True


def test_blank_stored_token_counts_as_unset(isolated_user_environment):
    write(paths.user_credentials_file(), 'homeassistant:\n  token: "   "\n')
    assert secrets.read_token() is None


def test_corrupt_credentials_file_is_reported_without_its_content(
    isolated_user_environment,
):
    write(paths.user_credentials_file(), 'homeassistant:\n  token: "abc\n  [\n')
    with pytest.raises(ConfigError) as exc:
        secrets.read_token()
    assert "abc" not in str(exc.value.summary) + str(exc.value.details)


def test_credentials_file_without_a_token_section(isolated_user_environment):
    write(paths.user_credentials_file(), "other:\n  value: 1\n")
    assert secrets.read_token() is None


def test_credentials_file_with_a_non_mapping_is_rejected(isolated_user_environment):
    write(paths.user_credentials_file(), "- a\n- b\n")
    with pytest.raises(ConfigError, match="mapping"):
        secrets.read_token()


def test_credentials_file_with_a_non_string_token_is_rejected(
    isolated_user_environment,
):
    write(paths.user_credentials_file(), "homeassistant:\n  token: 12345\n")
    with pytest.raises(ConfigError, match="string"):
        secrets.read_token()


def test_credentials_path_accepts_an_explicit_directory(tmp_path):
    assert secrets.credentials_path(tmp_path) == tmp_path / "credentials.yaml"


def test_write_token_cleans_up_a_stale_temporary_file(isolated_user_environment):
    stale = paths.user_credentials_file().with_name("credentials.yaml.tmp")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("leftover", encoding="utf-8")
    secrets.write_token("synthetic-after-stale")
    assert secrets.read_token() == "synthetic-after-stale"
    assert not stale.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows behaviour")
def test_owner_only_check_is_not_applicable_on_windows(isolated_user_environment):
    """Windows has no POSIX mode; the file inherits the user-profile ACL."""
    path = secrets.write_token("synthetic-windows")
    assert secrets.is_owner_only(path) is None


def test_no_test_leaves_a_real_looking_token_behind(isolated_user_environment):
    """Every token used here must be obviously synthetic."""
    secrets.write_token("synthetic-final")
    content = paths.user_credentials_file().read_text(encoding="utf-8")
    assert re.search(r"synthetic", content)
