"""``hhe convert``, ``verify`` and ``status`` in-process, as people and programs read them."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ha_history_exporter.archive.layout import ArchiveLayout
from tests.support.builders import state_row
from tests.support.fake_source import FakeHistorySource
from tests.support.terminal import Terminal, states

DAY = date(2026, 7, 29)


@pytest.fixture
def terminal(tmp_path: Path) -> Terminal:
    """An archive with one exported day in JSONL; NOW is the next morning."""
    terminal = Terminal(
        tmp_path / "t",
        environ={
            "HHE_URL": "http://homeassistant.local:8123",
            "HHE_TOKEN": "synthetic-test-token",
            "HHE_EXPORT_OUTPUT_DIR": str(tmp_path / "t" / "archive"),
            "HHE_RECORDER_PURGE_KEEP_DAYS": "14",
        },
    )
    terminal.source = FakeHistorySource(
        [[[state_row(timestamp="2026-07-29T10:00:00+00:00")]]],
        states=states(["sensor.test_temperature"]),
    )
    assert terminal.run("export") == 0
    terminal.connected.clear()
    return terminal


def layout(terminal: Terminal) -> ArchiveLayout:
    return ArchiveLayout(terminal.root / "archive")


def document(capsys: pytest.CaptureFixture[str], *argv: str, terminal: Terminal) -> Any:
    capsys.readouterr()
    code = terminal.run(*argv, "--json")
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["exit_code"] == code
    return parsed


def test_verify_reports_an_intact_archive(
    terminal: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert terminal.run("verify") == 0
    output = capsys.readouterr().out
    assert "1 day(s) checksummed: 1 ok." in output
    assert "Every stored artifact matches its manifest." in output
    assert terminal.connected == []


def test_verify_names_each_problem_and_fails(
    terminal: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    layout(terminal).day_file(DAY, "csv").write_text("stale", encoding="utf-8")
    capsys.readouterr()
    assert terminal.run("verify", "--deep") == 1
    output = capsys.readouterr().out
    assert "2026-07-29  csv     unexpected - no manifest declares this file" in output
    assert "decoded and checksummed: 1 ok, 1 unexpected." in output
    assert "Problems found." in output


def test_verify_as_json(terminal: Terminal, capsys: pytest.CaptureFixture[str]) -> None:
    parsed = document(capsys, "verify", terminal=terminal)
    assert parsed["command"] == "verify"
    assert parsed["summary"]["ok"] == 1
    assert parsed["archive"]["zone"] == {"timezone": "Europe/Berlin", "source": "recorded"}
    assert parsed["days"][0]["artifacts"][0]["status"] == "ok"


def test_status_shows_the_months(terminal: Terminal, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert terminal.run("status", "--start-date", "2026-07-27", "--end-date", "yesterday") == 0
    output = capsys.readouterr().out
    assert "Time zone  : Europe/Berlin (recorded in hhe-archive.json)" in output
    assert "2026-07  1 ok, 2 missing" in output
    assert "         missing: 27, 28" in output
    parsed = document(capsys, "status", terminal=terminal)
    assert parsed["months"][0]["ok"] == ["2026-07-29"]


def test_convert_adds_a_format(terminal: Terminal, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert terminal.run("convert", "--format", "csv") == 0
    output = capsys.readouterr().out
    assert "2026-07-29  converted  csv from jsonl" in output
    assert "1 day(s): 1 converted." in output
    assert layout(terminal).day_file(DAY, "csv").is_file()
    assert terminal.connected == []
    (log,) = layout(terminal).logs_dir.iterdir()  # the same second as the export's
    assert "Day 2026-07-29: derived csv from jsonl." in log.read_text(encoding="utf-8")
    parsed = document(capsys, "convert", "--format", "csv", terminal=terminal)
    assert parsed["summary"]["present"] == 1


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (("convert", "--format", "xml"), "--format does not know the output format 'xml'."),
        (("convert", "--format", "none"), "Name at least one format to derive."),
        (("verify", "--last-days", "0"), "--last-days needs a whole number of at least 1"),
        (("status", "--end-date", "yesterday"), "A range needs both --start-date and --end-date."),
    ],
)
def test_wrong_options_are_usage_errors(
    terminal: Terminal, argv: tuple[str, ...], message: str
) -> None:
    assert terminal.run(*argv) == 2
    assert message in terminal.stderr


def test_an_error_is_a_json_document_too(
    terminal: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    parsed = document(capsys, "convert", "--format", "xml", terminal=terminal)
    assert parsed["exit_code"] == 2
    assert parsed["error"]["code"] == "usage_error"
    assert parsed["error"]["remedies"][0]["command"] == "hhe convert --format parquet"
    assert "error:" in terminal.stderr


def test_export_as_json(terminal: Terminal, capsys: pytest.CaptureFixture[str]) -> None:
    parsed = document(capsys, "export", "--dry-run", terminal=terminal)
    assert parsed["dry_run"] is True
    assert parsed["plan"]["days"] == [
        {
            "day": "2026-07-29",
            "action": "skip_existing",
            "reason": parsed["plan"]["days"][0]["reason"],
        }
    ]
    assert parsed["entities"]["requested"] == 1
    assert "Will export" not in json.dumps(parsed)
