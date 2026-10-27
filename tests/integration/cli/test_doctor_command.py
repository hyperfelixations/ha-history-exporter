"""``hhe doctor``: every check reports, nothing is created or changed."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ha_history_exporter.archive.descriptor import establish
from ha_history_exporter.archive.layout import ArchiveLayout
from ha_history_exporter.archive.lock import FileLock
from ha_history_exporter.errors import AuthError
from tests.support.fake_source import FakeHistorySource
from tests.support.manifests import manifest14, write_manifest
from tests.support.terminal import Terminal, states


@pytest.fixture
def terminal(tmp_path: Path) -> Terminal:
    terminal = Terminal(
        tmp_path / "t",
        environ={
            "HHE_URL": "http://home-assistant.invalid:8123",
            "HHE_TOKEN": "synthetic-test-token",
            "HHE_EXPORT_OUTPUT_DIR": str(tmp_path / "t" / "archive" / "new"),
        },
    )
    terminal.source = FakeHistorySource(states=states(["sensor.a"]))
    return terminal


def archive(terminal: Terminal) -> ArchiveLayout:
    return ArchiveLayout(terminal.root / "archive" / "new")


def test_a_complete_setup_checks_out_without_creating_anything(
    terminal: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    assert terminal.run("doctor") == 0
    output = capsys.readouterr().out
    assert "(created by the first export)" in output
    assert "new archive; takes Home Assistant's" in output
    assert "reachable, version 2026.10.0, 1 entities, Europe/Berlin" in output
    assert not (terminal.root / "archive").exists()
    assert not (terminal.root / "temp").exists()


def test_offline_skips_home_assistant(terminal: Terminal) -> None:
    assert terminal.run("doctor", "--offline") == 0
    assert terminal.connected == []


def test_missing_credentials_fail_with_their_command(
    terminal: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    del terminal.environ["HHE_TOKEN"]
    assert terminal.run("doctor") == 1
    output = capsys.readouterr().out
    assert "[fail] access token" in output
    assert "-> hhe config set homeassistant.token" in output
    assert terminal.connected == []


def test_a_broken_configuration_is_one_failure(
    terminal: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    terminal.environ["HHE_REQUESTS_TIMEOUT"] = "soon"
    assert terminal.run("doctor") == 1
    assert "[fail] configuration" in capsys.readouterr().out


def test_a_rejected_token_fails_the_check(
    terminal: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    terminal.source = FakeHistorySource(api_error=AuthError("Rejected (HTTP 401)."))
    assert terminal.run("doctor") == 1
    assert "[fail] home assistant" in capsys.readouterr().out


def test_the_archive_is_reported_as_it_is(
    terminal: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = archive(terminal)
    write_manifest(layout.root, manifest14())
    (layout.root / ".hhe.lock").write_text("old", encoding="utf-8")
    with FileLock(layout.lock_file, "run_id: synthetic\n"):
        assert terminal.run("doctor", "--offline") == 0
    output = capsys.readouterr().out
    assert "Europe/Berlin; the next export records it for 1 existing day(s)" in output
    assert "an export is running (run_id: synthetic)" in output
    assert ".hhe.lock is left over" in output
    assert "1 day(s), 2026-07-28 to 2026-07-28" in output
    assert not layout.descriptor_file.exists()


def test_a_configured_zone_the_archive_does_not_use_fails(
    terminal: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    write_manifest(archive(terminal).root, manifest14())
    terminal.environ["HHE_EXPORT_TIMEZONE"] = "UTC"
    assert terminal.run("doctor", "--offline") == 1
    assert "export.timezone is UTC" in capsys.readouterr().out


def test_the_days_of_the_archive_are_checked_without_checksums(
    terminal: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = archive(terminal)
    write_manifest(layout.root, manifest14())
    establish(layout, None, configured=None, home_assistant=None, now=terminal.now)
    write_manifest(layout.root, manifest14(date(2026, 7, 27))).write_bytes(b"{")
    layout.day_file(date(2026, 7, 28), "csv").write_text("stale", encoding="utf-8")
    journal = layout.journal_root / "run-2026-07-28.json"
    journal.parent.mkdir()
    journal.write_text("{}", encoding="utf-8")

    assert terminal.run("doctor", "--offline") == 1

    output = capsys.readouterr().out
    assert "[fail] manifests" in output
    assert "1 invalid, first 2026-07-27" in output
    assert "1 file(s) that no manifest declares, first 2026-07-28" in output
    assert "1 declared file(s) missing, first 2026-07-28" in output
    assert "1 day(s); the next export or conversion restores them" in output
    assert journal.exists()


def test_plain_http_to_a_public_host_is_a_warning(
    terminal: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    terminal.environ["HHE_URL"] = "http://ha.example.net:8123"
    assert terminal.run("doctor", "--offline") == 0
    output = capsys.readouterr().out
    assert "[warn] plain http" in output
    assert "the access token travels unencrypted" in output


def test_doctor_as_json(terminal: Terminal, capsys: pytest.CaptureFixture[str]) -> None:
    assert terminal.run("doctor", "--offline", "--json") == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["command"] == "doctor"
    assert parsed["exit_code"] == 0
    assert {
        "status": "ok",
        "label": "home assistant",
        "detail": "not contacted (--offline)",
        "remedy": "",
    } in parsed["checks"]
