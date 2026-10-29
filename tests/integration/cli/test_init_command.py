"""``hhe init``: asks, checks against Home Assistant, writes both files or neither."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from ha_history_exporter.errors import HAConnectionError
from ha_history_exporter.settings.secrets import read_token
from tests.support.fake_source import FakeHistorySource
from tests.support.terminal import Terminal, states

TOKEN = "synthetic-test-token"
URL = "http://home-assistant.invalid:8123"


@pytest.fixture
def terminal(tmp_path: Path) -> Terminal:
    terminal = Terminal(tmp_path / "t")
    terminal.source = FakeHistorySource(states=states(["sensor.a", "sensor.b"]))
    return terminal


def written(terminal: Terminal) -> dict[str, Any]:
    with (terminal.config_dir / "config.toml").open("rb") as handle:
        return tomllib.load(handle)


def token(terminal: Terminal) -> str | None:
    return read_token(terminal.config_dir / "credentials.toml")


def test_without_questions_the_token_comes_from_hhe_token(terminal: Terminal) -> None:
    terminal.environ["HHE_TOKEN"] = TOKEN
    assert terminal.run("init", "--non-interactive", "--url", URL) == 0
    assert token(terminal) == TOKEN
    document = written(terminal)
    assert document["homeassistant"] == {"url": URL}
    assert "timezone" not in document["export"]
    assert document["recorder"] == {}
    assert terminal.connected[0].token == TOKEN


def test_without_questions_the_token_may_be_piped(terminal: Terminal) -> None:
    terminal.environ["HHE_URL"] = URL
    terminal.stdin = f"{TOKEN}\n"
    assert terminal.run("init", "--non-interactive", "--purge-keep-days", "14") == 0
    assert token(terminal) == TOKEN
    assert written(terminal)["recorder"] == {"purge_keep_days": 14}


@pytest.mark.parametrize(
    ("environ", "missing"),
    [({"HHE_TOKEN": TOKEN}, "--url or HHE_URL"), ({"HHE_URL": URL}, "a token in HHE_TOKEN")],
)
def test_without_questions_every_value_must_be_given(
    terminal: Terminal, environ: dict[str, str], missing: str
) -> None:
    terminal.environ.update(environ)
    assert terminal.run("init", "--non-interactive") == 2
    assert missing in terminal.stderr
    assert not terminal.config_dir.exists()


def test_the_questions_offer_home_assistants_time_zone_and_its_retention(
    terminal: Terminal,
) -> None:
    terminal.tty = True
    terminal.secrets = [TOKEN]
    terminal.answers = [URL, "", "jsonl,csv", "", ""]
    assert terminal.run("init") == 0
    document = written(terminal)
    assert document["homeassistant"]["url"] == URL
    assert document["export"]["formats"] == ["jsonl", "csv"]
    assert "timezone" not in document["export"]
    assert document["recorder"]["purge_keep_days"] == 10
    assert any("Europe/Berlin" in prompt for prompt in terminal.prompts)


def test_another_time_zone_is_stored(terminal: Terminal) -> None:
    terminal.secrets = [TOKEN]
    terminal.answers = [URL, "", "", "America/New_York", "7"]
    assert terminal.run("init") == 0
    document = written(terminal)
    assert document["export"]["timezone"] == "America/New_York"
    assert document["recorder"]["purge_keep_days"] == 7


def test_an_existing_configuration_is_updated_or_replaced_only_on_request(
    terminal: Terminal,
) -> None:
    terminal.environ["HHE_TOKEN"] = TOKEN
    terminal.run("init", "--non-interactive", "--url", URL, "--purge-keep-days", "14")
    assert terminal.run("init", "--non-interactive", "--url", URL) == 2
    assert "hhe init --force" in terminal.stderr
    assert terminal.run("init", "--non-interactive", "--force", "--url", URL) == 0
    assert written(terminal)["recorder"] == {}


def test_a_failed_check_writes_nothing_unless_the_user_insists(terminal: Terminal) -> None:
    terminal.source = FakeHistorySource(api_error=HAConnectionError("Synthetic outage."))
    terminal.secrets = [TOKEN]
    terminal.answers = [URL, "n"]
    assert terminal.run("init") == 4
    assert not terminal.config_dir.exists()
    terminal.secrets = [TOKEN]
    terminal.answers = [URL, "y", "", "", "", ""]
    assert terminal.run("init") == 0
    assert token(terminal) == TOKEN


def test_the_configuration_is_undone_when_the_token_cannot_be_stored(
    terminal: Terminal,
) -> None:
    terminal.environ["HHE_TOKEN"] = TOKEN
    (terminal.config_dir / "credentials.toml").mkdir(parents=True)
    assert terminal.run("init", "--non-interactive", "--url", URL) != 0
    assert not (terminal.config_dir / "config.toml").exists()


def test_an_invalid_answer_is_named(terminal: Terminal) -> None:
    terminal.environ["HHE_TOKEN"] = TOKEN
    assert (
        terminal.run("init", "--non-interactive", "--url", URL, "--timezone", "Mars/Olympus") == 2
    )
    assert "export.timezone from the setup is not a known IANA time zone" in terminal.stderr


def test_plain_http_to_a_public_host_is_stored_with_a_warning(
    terminal: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    terminal.environ["HHE_TOKEN"] = TOKEN
    url = "http://ha.example.net:8123"
    assert terminal.run("init", "--non-interactive", "--url", url, "--purge-keep-days", "14") == 0
    assert "the access token travels unencrypted" in capsys.readouterr().out
    assert written(terminal)["homeassistant"] == {"url": url}
