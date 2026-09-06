"""Tests for the guided ``init`` command."""

from __future__ import annotations

import pytest

from ha_history_exporter import cli, ha_client
from ha_history_exporter.errors import AuthError
from ha_history_exporter.settings import document, paths, secrets
from tests.helpers import FakeCliClient

SYNTHETIC_TOKEN = "synthetic-entered-token"


@pytest.fixture
def fake_client(monkeypatch):
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)
    return FakeCliClient


def answers(monkeypatch, *values: str) -> list[str]:
    """Feed *values* to input() in order and record the prompts."""
    prompts: list[str] = []
    pending = list(values)

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return pending.pop(0) if pending else ""

    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


def token(monkeypatch, value: str = SYNTHETIC_TOKEN) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt="": value)


# ── interactive ───────────────────────────────────────────────────────────────

def test_init_writes_both_files(monkeypatch, tmp_path, capsys, fake_client):
    output = tmp_path / "exports"
    prompts = answers(
        monkeypatch,
        "http://home-assistant.invalid:8123",  # url
        str(output),                           # output directory
        "jsonl,csv",                           # formats
        "UTC",                                 # timezone
    )
    token(monkeypatch)

    assert cli.main(["init"]) == 0

    out = capsys.readouterr().out
    assert "1 entities" in out or "entities" in out
    assert SYNTHETIC_TOKEN not in out

    values = document.read_user_values()
    assert values["homeassistant.url"] == "http://home-assistant.invalid:8123"
    assert values["export.output_dir"] == str(output)
    assert values["home_assistant.timezone"] == "UTC"
    assert values["formats.jsonl"] is True
    assert values["formats.csv"] is True
    assert values["formats.parquet"] is False

    assert secrets.read_token() == SYNTHETIC_TOKEN
    assert SYNTHETIC_TOKEN not in paths.user_config_file().read_text(encoding="utf-8")
    assert any("Home Assistant URL" in prompt for prompt in prompts)


def test_init_accepts_the_offered_defaults(monkeypatch, capsys, fake_client):
    answers(monkeypatch, "", "", "", "")
    token(monkeypatch)

    assert cli.main(["init"]) == 0

    values = document.read_user_values()
    assert values["homeassistant.url"] == "http://homeassistant.local:8123"
    assert values["home_assistant.timezone"] == "Europe/Berlin"
    assert values["formats.jsonl"] is True


def test_init_reports_a_failed_connection_and_can_abort(
    monkeypatch, capsys, fake_client
):
    FakeCliClient.check_error = AuthError("synthetic auth failure")
    answers(monkeypatch, "http://home-assistant.invalid", "n")
    token(monkeypatch)

    assert cli.main(["init"]) == 1

    assert "synthetic auth failure" in capsys.readouterr().err
    assert not paths.user_config_file().exists()
    assert secrets.read_token() is None


def test_init_can_save_despite_a_failed_connection(monkeypatch, capsys, fake_client):
    FakeCliClient.check_error = AuthError("synthetic auth failure")
    answers(
        monkeypatch,
        "http://home-assistant.invalid",  # url
        "y",                              # save anyway
        "",                               # output dir
        "",                               # formats
        "",                               # timezone
    )
    token(monkeypatch)

    assert cli.main(["init"]) == 0
    assert secrets.read_token() == SYNTHETIC_TOKEN


def test_init_refuses_to_overwrite_without_confirmation(
    monkeypatch, capsys, fake_client
):
    document.write_user_values({"formats.csv": True})
    answers(monkeypatch, "n")

    assert cli.main(["init"]) == 0
    assert "Nothing was changed" in capsys.readouterr().out
    assert document.read_user_values() == {"formats.csv": True}


def test_init_force_overwrites_without_asking(monkeypatch, capsys, fake_client):
    document.write_user_values({"formats.csv": True})
    answers(monkeypatch, "http://home-assistant.invalid", "", "jsonl", "")
    token(monkeypatch)

    assert cli.main(["init", "--force"]) == 0
    assert document.read_user_values()["formats.csv"] is False


def test_init_keeps_an_existing_token_on_empty_input(
    monkeypatch, capsys, fake_client
):
    secrets.write_token("synthetic-previous-token")
    answers(monkeypatch, "http://home-assistant.invalid", "", "", "")
    token(monkeypatch, "")

    assert cli.main(["init"]) == 0
    assert secrets.read_token() == "synthetic-previous-token"


def test_init_without_a_token_fails_clearly(monkeypatch, capsys, fake_client):
    answers(monkeypatch, "http://home-assistant.invalid")
    token(monkeypatch, "")

    assert cli.main(["init"]) == 2
    assert "No access token was entered" in capsys.readouterr().err


def test_init_rejects_an_invalid_timezone(monkeypatch, capsys, fake_client):
    answers(monkeypatch, "http://home-assistant.invalid", "", "jsonl", "Nowhere/Nothing")
    token(monkeypatch)

    assert cli.main(["init"]) == 2
    assert "not a known IANA time zone" in capsys.readouterr().err


def test_init_warns_when_parquet_lacks_pyarrow(monkeypatch, capsys, fake_client):
    import builtins

    real_import = builtins.__import__

    def no_pyarrow(name, *args, **kwargs):
        if name == "pyarrow":
            raise ImportError("synthetic missing pyarrow")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyarrow)
    answers(monkeypatch, "http://home-assistant.invalid", "", "parquet", "")
    token(monkeypatch)

    assert cli.main(["init"]) == 0
    assert "pipx inject" in capsys.readouterr().out


# ── non-interactive ───────────────────────────────────────────────────────────

def test_init_non_interactive_uses_options_and_the_environment(
    monkeypatch, tmp_path, capsys, fake_client
):
    monkeypatch.setenv("HA_TOKEN", "synthetic-env-token")

    class PipedStdin:
        @staticmethod
        def isatty() -> bool:
            return False

        @staticmethod
        def read() -> str:
            return "synthetic-piped-token\n"

    monkeypatch.setattr("sys.stdin", PipedStdin)

    assert (
        cli.main(
            [
                "init",
                "--non-interactive",
                "--url",
                "http://home-assistant.invalid",
                "--output-dir",
                str(tmp_path / "out"),
                "--format",
                "jsonl,parquet",
                "--timezone",
                "UTC",
            ]
        )
        == 0
    )

    assert secrets.read_token() == "synthetic-piped-token"
    values = document.read_user_values()
    assert values["formats.parquet"] is True
    assert values["home_assistant.timezone"] == "UTC"


def test_init_non_interactive_requires_a_url(monkeypatch, capsys, fake_client):
    assert cli.main(["init", "--non-interactive"]) == 2
    assert "--url" in capsys.readouterr().err


def test_init_non_interactive_refuses_to_overwrite(monkeypatch, capsys, fake_client):
    document.write_user_values({"formats.csv": True})

    assert (
        cli.main(
            ["init", "--non-interactive", "--url", "http://home-assistant.invalid"]
        )
        == 2
    )
    assert "already exists" in capsys.readouterr().err
