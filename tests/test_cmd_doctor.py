"""Tests for the ``doctor`` command."""

from __future__ import annotations

import json

import pytest

from ha_history_exporter import cli, ha_client
from ha_history_exporter.errors import AuthError, HAConnectionError
from ha_history_exporter.settings import document, secrets
from tests.helpers import FakeCliClient

TOKEN = "synthetic-stored-token"


@pytest.fixture
def configured(tmp_path):
    """A configuration that passes every offline check."""
    document.write_user_values(
        {
            "homeassistant.url": "http://home-assistant.invalid",
            "export.output_dir": str(tmp_path / "out"),
            "storage.temp_dir": str(tmp_path / "temp"),
        }
    )
    secrets.write_token(TOKEN)
    return tmp_path


# ── offline ───────────────────────────────────────────────────────────────────

def test_doctor_passes_a_complete_offline_configuration(configured, capsys):
    assert cli.main(["doctor", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "Everything checks out." in out
    assert "[fail]" not in out
    assert TOKEN not in out


def test_doctor_reports_missing_credentials_with_commands(tmp_path, capsys):
    document.write_user_values({"export.output_dir": str(tmp_path / "out")})

    assert cli.main(["doctor", "--offline"]) == 1

    out = capsys.readouterr().out
    assert "[fail] home assistant url" in out
    assert "hhe config set homeassistant.url" in out
    assert "[fail] access token" in out
    assert "hhe config set homeassistant.token" in out


def test_doctor_reports_an_invalid_timezone(configured, capsys):
    values = document.read_user_values()
    values["export.timezone"] = "Nowhere/Nothing"
    document.write_user_values(values)

    assert cli.main(["doctor", "--offline"]) == 1
    assert "[fail] timezone" in capsys.readouterr().out


def test_doctor_reports_an_unwritable_output_directory(
    configured, monkeypatch, capsys
):
    from pathlib import Path

    original = Path.mkdir

    def refuse(self, *args, **kwargs):
        if "out" in self.name:
            raise PermissionError(13, "Permission denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", refuse)

    assert cli.main(["doctor", "--offline"]) == 1
    out = capsys.readouterr().out
    assert "[fail] output directory" in out
    assert "hhe config set export.output_dir" in out


def test_doctor_warns_when_no_format_is_enabled(configured, capsys):
    values = document.read_user_values()
    values["export.formats"] = []
    document.write_user_values(values)

    assert cli.main(["doctor", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "[warn] output formats" in out
    assert "snapshot only" in out


def test_doctor_fails_when_parquet_lacks_pyarrow(configured, monkeypatch, capsys):
    import builtins

    values = document.read_user_values()
    values["export.formats"] = ["jsonl", "parquet"]
    document.write_user_values(values)

    real_import = builtins.__import__

    def no_pyarrow(name, *args, **kwargs):
        if name == "pyarrow":
            raise ImportError("synthetic missing pyarrow")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyarrow)

    assert cli.main(["doctor", "--offline"]) == 1
    out = capsys.readouterr().out
    assert "[fail] pyarrow" in out
    assert "pipx inject" in out


def test_doctor_reports_existing_exports(configured, capsys):
    day_dir = configured / "out" / "exports" / "daily" / "2026" / "07"
    day_dir.mkdir(parents=True)
    for day in ("2026-07-27", "2026-07-28"):
        (day_dir / f"{day}.manifest.json").write_text(
            json.dumps({"status": "ok"}), encoding="utf-8"
        )

    assert cli.main(["doctor", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "2 day(s), 2026-07-27 to 2026-07-28" in out


def test_doctor_reports_a_broken_configuration_file(tmp_path, capsys):
    broken = tmp_path / "broken.yaml"
    broken.write_text("formats:\n  jsnol: true\n", encoding="utf-8")

    assert cli.main(["doctor", "--offline", "--config", str(broken)]) == 1
    out = capsys.readouterr().out
    assert "[fail] configuration" in out
    assert "Unknown configuration key" in out


# ── online ────────────────────────────────────────────────────────────────────

def test_doctor_contacts_home_assistant(configured, monkeypatch, capsys):
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "[ ok ] home assistant" in out
    assert "1 entities" in out


@pytest.mark.parametrize(
    "error",
    [
        AuthError("Authentication failed: rejected token (HTTP 401)."),
        HAConnectionError("Failed to reach /api/ after 1 attempt(s)."),
    ],
)
def test_doctor_reports_a_failing_home_assistant(
    configured, monkeypatch, capsys, error
):
    FakeCliClient.check_error = error
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["doctor"]) == 1
    assert "[fail] home assistant" in capsys.readouterr().out


def test_doctor_skips_contact_without_credentials(tmp_path, monkeypatch, capsys):
    document.write_user_values({"export.output_dir": str(tmp_path / "out")})

    class ForbiddenClient:
        def __init__(self, **kwargs):
            raise AssertionError("doctor must not contact HA without credentials")

    monkeypatch.setattr(ha_client, "HomeAssistantClient", ForbiddenClient)

    assert cli.main(["doctor"]) == 1
    assert "[warn] home assistant" in capsys.readouterr().out
