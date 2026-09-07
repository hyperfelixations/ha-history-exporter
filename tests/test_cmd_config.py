"""Tests for the ``config`` command."""

from __future__ import annotations

import pytest

from ha_history_exporter import cli
from ha_history_exporter.settings import document, paths, secrets

SYNTHETIC_TOKEN = "synthetic-stored-token"


def read_user_config() -> str:
    return paths.user_config_file().read_text(encoding="utf-8")


# ── get and list ──────────────────────────────────────────────────────────────

def test_get_prints_only_the_value(capsys):
    assert cli.main(["config", "get", "formats.jsonl"]) == 0
    assert capsys.readouterr().out == "True\n"


def test_get_reflects_the_environment(monkeypatch, capsys):
    monkeypatch.setenv("HHE_REQUESTS_BATCH_SIZE_ENTITIES", "17")
    assert cli.main(["config", "get", "requests.batch_size_entities"]) == 0
    assert capsys.readouterr().out == "17\n"


def test_get_rejects_an_unknown_key_with_a_suggestion(capsys):
    assert cli.main(["config", "get", "formats.jsnol"]) == 2
    err = capsys.readouterr().err
    assert "Unknown configuration key 'formats.jsnol'" in err
    assert "formats.jsonl" in err


def test_list_shows_every_key(capsys):
    from ha_history_exporter.settings import schema

    assert cli.main(["config", "list"]) == 0
    out = capsys.readouterr().out
    for key in schema.KEYS:
        assert key.path in out


def test_list_with_origin_names_the_source(monkeypatch, capsys):
    monkeypatch.setenv("HHE_FORMATS_PARQUET", "true")
    assert cli.main(["config", "list", "--origin"]) == 0
    out = capsys.readouterr().out
    assert "[env:HHE_FORMATS_PARQUET]" in out
    assert "[default]" in out


def test_list_never_prints_the_stored_token(capsys):
    secrets.write_token(SYNTHETIC_TOKEN)
    assert cli.main(["config", "list", "--origin"]) == 0
    out = capsys.readouterr().out
    assert SYNTHETIC_TOKEN not in out
    assert "<set>" in out


def test_path_reports_every_location(capsys):
    assert cli.main(["config", "path"]) == 0
    out = capsys.readouterr().out
    for label in (
        "configuration file",
        "credentials file",
        "output directory",
        "log directory",
        "temporary directory",
    ):
        assert label in out


# ── set and unset ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("formats.parquet", "true", "True"),
        ("requests.batch_size_entities", "12", "12"),
        ("requests.sleep_between_days_seconds", "2.5", "2.5"),
        ("home_assistant.timezone", "UTC", "UTC"),
        ("entity_selection.optional_exclude_domains", "update,button", "update"),
    ],
)
def test_set_writes_the_user_file_and_get_reads_it_back(key, raw, expected, capsys):
    assert cli.main(["config", "set", key, raw]) == 0
    capsys.readouterr()

    assert cli.main(["config", "get", key]) == 0
    assert expected in capsys.readouterr().out
    assert key.split(".", 1)[1] in read_user_config()


def test_set_rejects_a_value_of_the_wrong_type(capsys):
    assert cli.main(["config", "set", "requests.batch_size_entities", "many"]) == 2
    err = capsys.readouterr().err
    assert "requests.batch_size_entities" in err
    assert not paths.user_config_file().exists()


def test_set_rejects_an_unsupported_option(capsys):
    assert cli.main(["config", "set", "export.include_current_day", "true"]) == 2
    assert "does not support the value" in capsys.readouterr().err


def test_set_requires_a_value_for_normal_keys(capsys):
    assert cli.main(["config", "set", "formats.csv"]) == 2
    assert "A value is required" in capsys.readouterr().err


def test_set_does_not_bake_environment_values_into_the_user_file(
    monkeypatch, capsys
):
    """Only the key being set may land in the user file."""
    monkeypatch.setenv("HHE_REQUESTS_MAX_RETRIES", "9")

    assert cli.main(["config", "set", "formats.csv", "true"]) == 0
    capsys.readouterr()

    text = read_user_config()
    assert "csv: true" in text
    assert "max_retries" not in text


def test_set_stores_the_token_in_the_credentials_file_only(capsys):
    assert cli.main(["config", "set", "homeassistant.token", SYNTHETIC_TOKEN]) == 0
    out = capsys.readouterr().out

    assert SYNTHETIC_TOKEN not in out
    assert secrets.read_token() == SYNTHETIC_TOKEN
    assert not paths.user_config_file().exists()


def test_set_reads_a_piped_token_without_echoing_it(monkeypatch, capsys):
    class PipedStdin:
        @staticmethod
        def isatty() -> bool:
            return False

        @staticmethod
        def read() -> str:
            return f"{SYNTHETIC_TOKEN}\n"

    monkeypatch.setattr("sys.stdin", PipedStdin)

    assert cli.main(["config", "set", "homeassistant.token"]) == 0
    assert SYNTHETIC_TOKEN not in capsys.readouterr().out
    assert secrets.read_token() == SYNTHETIC_TOKEN


def test_set_prompts_for_the_token_without_echo(monkeypatch, capsys):
    prompts: list[str] = []

    class Tty:
        @staticmethod
        def isatty() -> bool:
            return True

    def fake_getpass(prompt: str) -> str:
        prompts.append(prompt)
        return SYNTHETIC_TOKEN

    monkeypatch.setattr("sys.stdin", Tty)
    monkeypatch.setattr("getpass.getpass", fake_getpass)

    assert cli.main(["config", "set", "homeassistant.token"]) == 0
    assert prompts and "hidden" in prompts[0]
    assert secrets.read_token() == SYNTHETIC_TOKEN


def test_set_rejects_an_empty_token(monkeypatch, capsys):
    class Tty:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr("sys.stdin", Tty)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "   ")

    assert cli.main(["config", "set", "homeassistant.token"]) == 2
    assert secrets.read_token() is None


def test_unset_removes_a_value_and_reports_the_fallback(capsys):
    cli.main(["config", "set", "formats.csv", "true"])
    capsys.readouterr()

    assert cli.main(["config", "unset", "formats.csv"]) == 0
    out = capsys.readouterr().out
    assert "False" in out
    assert "[default]" in out


def test_unset_on_an_unset_key_is_harmless(capsys):
    assert cli.main(["config", "unset", "formats.csv"]) == 0
    assert "not set in the user configuration" in capsys.readouterr().out


def test_unset_clears_the_stored_token(capsys):
    secrets.write_token(SYNTHETIC_TOKEN)

    assert cli.main(["config", "unset", "homeassistant.token"]) == 0
    assert "Removed" in capsys.readouterr().out
    assert secrets.read_token() is None


def test_writes_refuse_an_explicit_config_file(tmp_path, capsys):
    other = tmp_path / "other.yaml"
    other.write_text("formats:\n  csv: true\n", encoding="utf-8")

    assert cli.main(["config", "--config", str(other), "set", "formats.csv", "false"]) == 2
    assert "always write the user configuration" in capsys.readouterr().err
    assert "csv: true" in other.read_text(encoding="utf-8")


# ── edit ──────────────────────────────────────────────────────────────────────

def test_edit_creates_validates_and_reports_the_file(monkeypatch, tmp_path, capsys):
    marker = tmp_path / "editor-ran"
    script = tmp_path / "editor.py"
    script.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path(r'{marker}').write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EDITOR", f"{__import__('sys').executable} {script}")

    assert cli.main(["config", "edit"]) == 0
    assert marker.read_text(encoding="utf-8") == "ran"
    assert "is valid" in capsys.readouterr().out


def test_edit_reports_an_invalid_document_and_keeps_it(monkeypatch, tmp_path, capsys):
    script = tmp_path / "editor.py"
    script.write_text(
        "import sys, pathlib\n"
        "pathlib.Path(sys.argv[1]).write_text("
        "'formats:\\n  jsonl: not-a-boolean\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EDITOR", f"{__import__('sys').executable} {script}")

    assert cli.main(["config", "edit"]) == 2
    assert "formats.jsonl" in capsys.readouterr().err
    assert "not-a-boolean" in read_user_config()


def test_edit_without_an_editor_explains_how_to_set_one(monkeypatch, capsys):
    import os

    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delattr(os, "startfile", raising=False)  # emulate a POSIX host

    assert cli.main(["config", "edit"]) == 2
    assert "No editor configured" in capsys.readouterr().err


@pytest.mark.skipif(
    not hasattr(__import__("os"), "startfile"), reason="Windows only"
)
def test_edit_falls_back_to_the_windows_default_editor(monkeypatch, capsys):
    import os

    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    opened: list[str] = []
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)

    assert cli.main(["config", "edit"]) == 0
    assert opened == [str(paths.user_config_file())]
    assert "default editor" in capsys.readouterr().out


# ── document rendering ────────────────────────────────────────────────────────

def test_rendered_document_round_trips():
    values = {
        "formats.parquet": True,
        "export.output_dir": r"D:\ha-archive",
        "requests.backoff_seconds": [1.0, 2.0],
    }
    document.write_user_values(values)

    assert document.read_user_values() == values


def test_rendered_document_never_contains_the_token_key():
    document.write_user_values({"homeassistant.url": "http://home-assistant.invalid"})
    text = read_user_config()

    assert "token" in text  # only as the explanatory header comment
    assert "  token:" not in text
