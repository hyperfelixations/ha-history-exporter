"""``hhe export`` end to end in-process: options, output, exit codes, logs."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ha_history_exporter.archive.layout import ArchiveLayout
from ha_history_exporter.errors import AuthError, HAConnectionError
from tests.helpers import state_row
from tests.support.fake_source import FakeHistorySource
from tests.support.terminal import Terminal, states

TOKEN = "synthetic-test-token"
YESTERDAY = "2026-07-29"


@pytest.fixture
def terminal(tmp_path: Path) -> Terminal:
    return Terminal(
        tmp_path / "t",
        environ={
            "HHE_URL": "http://home-assistant.invalid:8123",
            "HHE_TOKEN": TOKEN,
            "HHE_EXPORT_OUTPUT_DIR": str(tmp_path / "t" / "archive"),
            "HHE_RECORDER_PURGE_KEEP_DAYS": "14",
        },
    )


def layout(terminal: Terminal) -> ArchiveLayout:
    return ArchiveLayout(terminal.root / "archive")


def answer(terminal: Terminal, *answers: object) -> None:
    terminal.source = FakeHistorySource(
        answers,  # type: ignore[arg-type]
        states=states(["sensor.test_temperature"]),
    )


def test_without_a_selection_the_latest_complete_day_is_exported(
    terminal: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    answer(terminal, [[state_row(timestamp="2026-07-29T10:00:00+00:00")]])
    assert terminal.run("export") == 0
    output = capsys.readouterr().out
    assert "Will export (1):" in output
    assert f"{YESTERDAY}  ok       1 state objects" in output
    manifest = json.loads(layout(terminal).manifest_file(date(2026, 7, 29)).read_bytes())
    assert manifest["status"] == "ok"
    logs = list(layout(terminal).logs_dir.iterdir())
    assert [path.name for path in logs] == ["ha_history_export_2026-07-30_033005.log"]
    assert "Run 20260730T013005-" in logs[0].read_text(encoding="utf-8")
    assert TOKEN not in logs[0].read_text(encoding="utf-8")


def test_a_dry_run_shows_the_plan_and_leaves_nothing(
    terminal: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    answer(terminal)
    assert terminal.run("export", "--last-days", "3", "--dry-run") == 0
    output = capsys.readouterr().out
    assert "Will export (3):" in output
    assert "Dry run: nothing was fetched or written." in output
    assert not (terminal.root / "archive").exists()
    assert not (terminal.root / "temp").exists()


def test_today_by_name_is_exported_partially(terminal: Terminal) -> None:
    answer(terminal, [[state_row(timestamp="2026-07-30T00:30:00+00:00")]])
    assert terminal.run("export", "--date", "today") == 0
    path = layout(terminal).manifest_file(date(2026, 7, 30))
    assert json.loads(path.read_bytes())["status"] == "partial"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--last-days", "0"], "--last-days needs a whole number of at least 1, not '0'"),
        (["--last-days", "two"], "not 'two'"),
        (["--start-date", "2026-07-20"], "needs both --start-date and --end-date"),
        (["--end-date", "2026-07-20"], "needs both --start-date and --end-date"),
        (["--date", "2026/07/20"], "is not a day"),
        (["--start-date", "today", "--end-date", "yesterday"], "starts after it ends"),
        (["--batch-size", "0"], "requests.batch_size from --batch-size"),
        (["--format", "xml"], "does not know the output format 'xml'"),
        (["--timezone", "Mars/Olympus"], "not a known IANA time zone"),
    ],
)
def test_wrong_options_exit_with_two_before_home_assistant_is_asked(
    terminal: Terminal, argv: list[str], message: str
) -> None:
    answer(terminal)
    assert terminal.run("export", *argv) == 2
    assert message in terminal.stderr
    assert terminal.connected == []


def test_a_missing_url_says_how_to_set_one(terminal: Terminal) -> None:
    del terminal.environ["HHE_URL"]
    assert terminal.run("export") == 2
    assert "No Home Assistant URL is configured." in terminal.stderr
    assert "hhe init" in terminal.stderr


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (AuthError("Home Assistant rejected the access token (HTTP 401)."), 3),
        (HAConnectionError("Home Assistant is not available."), 4),
    ],
)
def test_failures_of_home_assistant_have_their_exit_codes(
    terminal: Terminal, failure: Exception, code: int
) -> None:
    answer(terminal, failure)
    assert terminal.run("export") == code
    assert str(failure) in terminal.stderr


def test_a_failed_day_exits_with_one(terminal: Terminal) -> None:
    answer(terminal, [[state_row(timestamp="2026-07-31T10:00:00+00:00")]])
    assert terminal.run("export") == 1


def test_a_defect_exits_with_seventy_and_leaves_its_traceback_in_the_log_only(
    terminal: Terminal,
) -> None:
    answer(terminal, ZeroDivisionError("synthetic defect"))
    assert terminal.run("export") == 70
    assert "Traceback" not in terminal.stderr
    assert "unexpected internal error" in terminal.stderr
    log = next(layout(terminal).logs_dir.iterdir()).read_text(encoding="utf-8")
    assert "Traceback" in log
    assert "ZeroDivisionError" in log


def test_an_interruption_exits_with_130(terminal: Terminal) -> None:
    answer(terminal, KeyboardInterrupt())
    assert terminal.run("export") == 130


def test_a_day_already_exported_is_not_asked_for_again(terminal: Terminal) -> None:
    answer(terminal, [[state_row(timestamp="2026-07-29T10:00:00+00:00")]])
    assert terminal.run("export") == 0
    assert len(terminal.connected) == 1
    assert terminal.run("export") == 0
    assert len(terminal.connected) == 1


def test_a_misspelt_variable_is_named(terminal: Terminal) -> None:
    terminal.environ["HHE_REQUEST_TIMEOUT"] = "30"
    answer(terminal, [[state_row(timestamp="2026-07-29T10:00:00+00:00")]])
    assert terminal.run("export") == 0
    assert "HHE_REQUEST_TIMEOUT is not a variable HHE knows" in terminal.stderr


@pytest.mark.parametrize("prog", ["hhe", "ha-history-exporter", "python -m ha_history_exporter"])
def test_usage_errors_name_the_program(
    terminal: Terminal, prog: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert terminal.run("export", "--no-such-option", prog=prog) == 2
    assert f"usage: {prog} " in capsys.readouterr().err
