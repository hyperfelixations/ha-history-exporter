"""Resolving the configuration: precedence, origins, documents and the token."""

from __future__ import annotations

import itertools
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.errors import ConfigError, CredentialsError
from ha_history_exporter.settings import ResolvedSettings, resolve
from ha_history_exporter.settings.paths import ENV_CONFIG_DIR
from ha_history_exporter.settings.secrets import write_token

TOKEN = "synthetic-test-token"


class Setup:
    """A user configuration directory, a working directory and an environment."""

    def __init__(self, tmp_path: Path) -> None:
        self.config_dir = tmp_path / "config"
        self.cwd = tmp_path / "cwd"
        self.config_dir.mkdir(parents=True)
        self.cwd.mkdir(parents=True)
        self.environ: dict[str, str] = {ENV_CONFIG_DIR: str(self.config_dir)}

    @property
    def user_file(self) -> Path:
        return self.config_dir / "config.toml"

    def write(self, text: str, path: Path | None = None) -> Path:
        target = path or self.user_file
        target.write_text(text, encoding="utf-8")
        return target

    def resolve(
        self, *, explicit: Path | None = None, cli: dict[str, str] | None = None
    ) -> ResolvedSettings:
        return resolve(environ=self.environ, cwd=self.cwd, explicit_config=explicit, cli=cli)


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path / "setup")


def test_without_any_source_the_defaults_apply(setup: Setup) -> None:
    settings = setup.resolve()
    assert settings.config_file is None
    assert settings.config.requests.batch_size == 15
    assert str(settings.origin("requests.batch_size")) == "default"
    assert settings.token is None


@pytest.mark.parametrize(
    ("in_file", "in_env", "on_cli"), list(itertools.product((False, True), repeat=3))
)
def test_the_command_line_beats_the_environment_beats_the_file(
    setup: Setup, in_file: bool, in_env: bool, on_cli: bool
) -> None:
    expected, origin = 15, "default"
    if in_file:
        setup.write("[requests]\nbatch_size = 10\n")
        expected, origin = 10, f"file:{setup.user_file}:2"
    if in_env:
        setup.environ["HHE_REQUESTS_BATCH_SIZE"] = "11"
        expected, origin = 11, "env:HHE_REQUESTS_BATCH_SIZE"
    cli = {"requests.batch_size": "12"} if on_cli else None
    if on_cli:
        expected, origin = 12, "cli:--batch-size"
    settings = setup.resolve(cli=cli)
    assert settings.config.requests.batch_size == expected
    assert str(settings.origin("requests.batch_size")) == origin


def test_a_named_file_replaces_the_user_file(setup: Setup) -> None:
    setup.write("[requests]\nbatch_size = 10\ntimeout = 30\n")
    named = setup.write("[requests]\nbatch_size = 20\n", setup.cwd / "other.toml")
    settings = setup.resolve(explicit=Path("other.toml"))
    assert settings.config_file == named
    assert settings.config.requests.batch_size == 20
    assert settings.config.requests.timeout == 120


def test_a_missing_named_file_is_an_error(setup: Setup) -> None:
    with pytest.raises(ConfigError, match="Configuration file not found"):
        setup.resolve(explicit=Path("absent.toml"))


def test_a_directory_in_place_of_the_user_file_is_an_error(setup: Setup) -> None:
    setup.user_file.mkdir()
    with pytest.raises(ConfigError, match="not a regular file"):
        setup.resolve()


def test_no_configuration_is_read_from_the_working_directory(setup: Setup) -> None:
    setup.write("[requests]\nbatch_size = 10\n", setup.cwd / "config.toml")
    assert setup.resolve().config.requests.batch_size == 15


def test_relative_paths_follow_their_source(setup: Setup) -> None:
    setup.write('[export]\noutput_dir = "archive"\n[storage]\ntemp_dir = "work"\n')
    setup.environ["HHE_STORAGE_TEMP_DIR"] = "scratch"
    config = setup.resolve().config
    assert config.export.output_dir == setup.config_dir / "archive"
    assert config.storage.temp_dir == setup.cwd / "scratch"
    cli = setup.resolve(cli={"export.output_dir": "here"}).config
    assert cli.export.output_dir == setup.cwd / "here"


def test_every_section_reaches_the_configuration(setup: Setup) -> None:
    setup.write(
        "[homeassistant]\n"
        'url = "http://home-assistant.invalid:8123/"\n'
        "[export]\n"
        'timezone = "America/New_York"\n'
        'formats = ["parquet", "csv"]\n'
        "[requests]\n"
        "backoff = [1, 2.5]\n"
        "[history_request]\n"
        "minimal_response = true\n"
        "[entities]\n"
        'exclude_patterns = ["sensor.*_debug"]\n'
        "[recorder]\n"
        "purge_keep_days = 10\n"
        "[storage]\n"
        "locked_file_retry_sleep = 0.5\n"
    )
    config = setup.resolve().config
    assert config.homeassistant.url == "http://home-assistant.invalid:8123"
    assert config.export.timezone == ZoneInfo("America/New_York")
    assert {fmt.value for fmt in config.export.formats} == {"parquet", "csv"}
    assert config.requests.backoff == (1.0, 2.5)
    assert config.history_request.minimal_response
    assert config.entities.exclude_patterns == ("sensor.*_debug",)
    assert config.recorder.purge_keep_days == 10
    assert config.storage.locked_file_retry_sleep == 0.5


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("[requests]\nbatch_size = 10\nbatch_size = 11\n", r"not valid TOML \(line 3"),
        ("[requests]\nbatch_size = 1\n[requests]\n", r"not valid TOML \(line 3, column"),
        ("[requests\n", r"not valid TOML \(line 1"),
        ("[requests]\nbatchsize = 10\n", r"Unknown configuration key 'requests.batchsize'"),
        ("[request]\nbatch_size = 10\n", r"Unknown configuration key 'request'"),
        ("requests = 3\n", "must be a table"),
        ("[homeassistant]\ntoken = 'synthetic-test-token'\n", "token must not be in"),
    ],
)
def test_document_errors_name_where_they_are(setup: Setup, document: str, message: str) -> None:
    setup.write(document)
    with pytest.raises(ConfigError, match=message):
        setup.resolve()


def test_an_unknown_key_is_located_and_corrected(setup: Setup) -> None:
    setup.write("[export]\nformats = []\n\n[requests]\nbatchsize = 10\n")
    with pytest.raises(ConfigError) as caught:
        setup.resolve()
    assert "config.toml, line 5" in caught.value.summary
    assert "'requests.batch_size'" in (caught.value.details or "")


def test_a_document_that_is_not_utf8_is_reported(setup: Setup) -> None:
    setup.user_file.write_bytes(b"[export]\nformats = ['\xff']\n")
    with pytest.raises(ConfigError, match="not UTF-8"):
        setup.resolve()


@pytest.mark.parametrize(
    ("document", "env", "message"),
    [
        ("[requests]\nbatch_size = 0\n", {}, r"requests.batch_size from config.toml, line 2"),
        ('[export]\ntimezone = "Mars/Olympus"\n', {}, r"\(got 'Mars/Olympus'\)"),
        ("", {"HHE_REQUESTS_TIMEOUT": "soon"}, "requests.timeout from HHE_REQUESTS_TIMEOUT"),
        ("", {"HHE_ENTITIES_INCLUDE_UNKNOWN": "maybe"}, "must be true or false"),
    ],
)
def test_invalid_values_name_the_key_and_the_source(
    setup: Setup, document: str, env: dict[str, str], message: str
) -> None:
    setup.write(document)
    setup.environ.update(env)
    with pytest.raises(ConfigError, match=message):
        setup.resolve()


def test_an_invalid_url_is_never_quoted(setup: Setup) -> None:
    setup.environ["HHE_URL"] = "http://" + "user:secret-value@" + "home-assistant.invalid"
    with pytest.raises(ConfigError) as caught:
        setup.resolve()
    assert "secret-value" not in f"{caught.value.summary} {caught.value.details}"


def test_retries_need_a_backoff_schedule(setup: Setup) -> None:
    setup.write("[requests]\nbackoff = []\n")
    with pytest.raises(ConfigError, match=r"requests.backoff needs at least one wait time"):
        setup.resolve()
    setup.write("[requests]\nbackoff = []\nmax_retries = 0\n")
    assert setup.resolve().config.requests.backoff == ()


def test_the_token_comes_from_the_environment_before_the_credentials_file(setup: Setup) -> None:
    write_token(setup.config_dir / "credentials.toml", "stored-synthetic-token")
    assert setup.resolve().token == "stored-synthetic-token"
    assert str(setup.resolve().origin("homeassistant.token")).startswith("file:")
    setup.environ["HHE_TOKEN"] = TOKEN
    assert setup.resolve().token == TOKEN
    assert str(setup.resolve().origin("homeassistant.token")) == "env:HHE_TOKEN"


def test_a_connection_needs_the_url_and_the_token(setup: Setup) -> None:
    with pytest.raises(CredentialsError, match="No Home Assistant URL"):
        setup.resolve().connection()
    setup.environ["HHE_URL"] = "http://home-assistant.invalid:8123"
    with pytest.raises(CredentialsError, match="No Home Assistant access token") as caught:
        setup.resolve().connection()
    assert any("HHE_TOKEN" in (remedy.command or "") for remedy in caught.value.remedies)
    setup.environ["HHE_TOKEN"] = TOKEN
    connection = setup.resolve().connection()
    assert connection.url == "http://home-assistant.invalid:8123"
    assert TOKEN not in repr(connection)
    assert TOKEN not in repr(setup.resolve())


def test_misspelt_variables_are_reported(setup: Setup) -> None:
    setup.environ["HHE_REQUEST_TIMEOUT"] = "30"
    assert setup.resolve().unknown_environment == ("HHE_REQUEST_TIMEOUT",)
