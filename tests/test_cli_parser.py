"""Tests for the command grammar, the legacy shim, and range selection."""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter import __version__, cli
from ha_history_exporter.cli import parser
from ha_history_exporter.cli.commands import export as export_command
from ha_history_exporter.errors import UsageError
from ha_history_exporter.settings import Format
from ha_history_exporter.time_utils import last_n_complete_days, today_local

BERLIN = ZoneInfo("Europe/Berlin")


# ── the command is part of every invocation ───────────────────────────────────

def test_top_level_help_names_the_invoked_program(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ha-history-exporter", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("usage: ha-history-exporter ")


def test_version_flag_reports_the_package_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_an_invocation_without_a_command_exits_two_and_lists_the_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
    assert "COMMAND" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--date", "2026-07-28"],
        ["--last-days", "7"],
        ["--dry-run"],
    ],
)
def test_export_flags_without_the_command_are_rejected(argv, capsys):
    """The implicit command is gone: one spelling, stated in every invocation."""
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert "COMMAND" in capsys.readouterr().err


def test_an_unknown_command_lists_the_known_ones(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["exprot", "--date", "2026-07-28"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err
    assert "export" in err


@pytest.mark.parametrize("command", ["export", "init", "config", "doctor"])
def test_every_command_is_reachable(command):
    assert command in parser.COMMANDS


def test_export_without_a_day_selection_takes_the_latest_complete_day():
    """The daily case needs no argument to say what it obviously means."""
    tz = ZoneInfo("Europe/Berlin")
    args = cli.parse_args(["export"])
    assert args.command == "export"

    start, end = export_command.resolve_date_range(args, tz)
    expected = today_local(tz) - timedelta(days=1)
    assert (start, end) == (expected, expected)


def test_end_date_without_start_date_is_a_usage_error():
    tz = ZoneInfo("Europe/Berlin")
    args = cli.parse_args(["export", "--end-date", "2026-07-28"])
    with pytest.raises(ValueError, match="--start-date is required"):
        export_command.resolve_date_range(args, tz)


@pytest.mark.parametrize(
    "argv",
    [
        ["export", "--date", "2026-07-28", "--last-days", "3"],
        ["export", "--start-date", "2026-07-01", "--last-days", "3"],
        ["export", "--date", "2026-07-28", "--start-date", "2026-07-01"],
    ],
)
def test_day_selections_stay_mutually_exclusive(argv):
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("word", ["yesterday", "today"])
def test_date_words_work_in_every_date_option(word):
    """One translation of 'yesterday' and 'today', used by all three options."""
    tz = ZoneInfo("Europe/Berlin")
    single = export_command.resolve_date_range(
        cli.parse_args(["export", "--date", word]), tz
    )
    ranged = export_command.resolve_date_range(
        cli.parse_args(["export", "--start-date", word, "--end-date", word]), tz
    )
    assert single == ranged


def test_a_range_may_end_at_yesterday():
    tz = ZoneInfo("Europe/Berlin")
    args = cli.parse_args(
        ["export", "--start-date", "2026-07-01", "--end-date", "yesterday"]
    )
    start, end = export_command.resolve_date_range(args, tz)
    assert start == date(2026, 7, 1)
    assert end == today_local(tz) - timedelta(days=1)


# ── --last-days ───────────────────────────────────────────────────────────────

def test_last_n_complete_days_ends_yesterday():
    today = today_local(BERLIN)
    assert last_n_complete_days(1, BERLIN) == (
        today - timedelta(days=1),
        today - timedelta(days=1),
    )
    assert last_n_complete_days(10, BERLIN) == (
        today - timedelta(days=10),
        today - timedelta(days=1),
    )


def test_last_n_complete_days_covers_exactly_n_days():
    start, end = last_n_complete_days(10, BERLIN)
    assert (end - start).days + 1 == 10


@pytest.mark.parametrize("value", [0, -1])
def test_last_n_complete_days_rejects_non_positive_counts(value):
    with pytest.raises(ValueError, match="must be 1 or greater"):
        last_n_complete_days(value, BERLIN)


def test_last_days_matches_the_legacy_batch_file_window():
    """The .bat asked for today-10 .. today; today is skipped as incomplete."""
    today = today_local(BERLIN)
    start, end = last_n_complete_days(10, BERLIN)
    assert start == today - timedelta(days=10)
    assert end == today - timedelta(days=1)


def test_resolve_date_range_uses_last_days():
    args = cli.parse_args(["export", "--last-days", "3"])
    start, end = export_command.resolve_date_range(args, BERLIN)
    assert (start, end) == last_n_complete_days(3, BERLIN)


def test_resolve_date_range_still_supports_single_days_and_ranges():
    single = cli.parse_args(["export", "--date", "2026-07-28"])
    assert export_command.resolve_date_range(single, BERLIN) == (
        date(2026, 7, 28),
        date(2026, 7, 28),
    )

    ranged = cli.parse_args(
        ["export", "--start-date", "2026-07-20", "--end-date", "2026-07-28"]
    )
    assert export_command.resolve_date_range(ranged, BERLIN) == (
        date(2026, 7, 20),
        date(2026, 7, 28),
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--last-days", "7", "--date", "2026-07-28"],
        ["--last-days", "7", "--start-date", "2026-07-20"],
    ],
)
def test_last_days_is_mutually_exclusive_with_other_selectors(argv):
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("value", ["0", "-1"])
def test_invalid_last_days_exits_with_code_two(tmp_path, monkeypatch, capsys, value):
    monkeypatch.setenv("HA_URL", "http://home-assistant.invalid")
    monkeypatch.setenv("HA_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("HHE_EXPORT_OUTPUT_DIR", str(tmp_path / "output"))

    assert cli.main(["export", "--last-days", value]) == 2
    assert "--last-days must be 1 or greater" in capsys.readouterr().err


def test_invalid_date_argument_is_echoed_for_diagnosis(tmp_path, monkeypatch, capsys):
    marker = "not-a-date"
    monkeypatch.setenv("HA_URL", "http://home-assistant.invalid")
    monkeypatch.setenv("HA_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("HHE_EXPORT_OUTPUT_DIR", str(tmp_path / "output"))

    assert cli.main(["export", "--date", marker]) == 2
    captured = capsys.readouterr()
    assert marker not in captured.out
    assert marker in captured.err
    assert "Unrecognised date argument" in captured.err


# ── --format ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("jsonl", {Format.JSONL}),
        ("parquet", {Format.PARQUET}),
        ("jsonl,parquet", {Format.JSONL, Format.PARQUET}),
        ("JSONL, CSV", {Format.JSONL, Format.CSV}),
        ("none", set()),
    ],
)
def test_format_list_states_the_complete_set(raw, expected):
    assert export_command.parse_format_list(raw) == frozenset(expected)


@pytest.mark.parametrize("raw", ["", " , "])
def test_an_empty_format_list_means_snapshot_only(raw):
    """An empty selection is honest about what it does; it is not an error."""
    assert export_command.parse_format_list(raw) == frozenset()


def test_unknown_format_lists_the_valid_values():
    with pytest.raises(UsageError) as exc:
        export_command.parse_format_list("arrow")
    assert "arrow" in exc.value.summary
    assert "jsonl" in (exc.value.details or "")
    assert exc.value.exit_code == 2


def test_none_cannot_be_combined_with_another_format():
    with pytest.raises(UsageError, match="cannot combine 'none'"):
        export_command.parse_format_list("none,jsonl")


@pytest.mark.parametrize(
    ("option", "reason"),
    [
        ("--jsonl", "--format states the whole set, not a nudge to one member"),
        ("--parquet", "--format states the whole set, not a nudge to one member"),
        ("--no-csv", "--format states the whole set, not a nudge to one member"),
        ("--resume", "skipping finished days is the behaviour; --force overrides it"),
        ("--outdir", "the option is spelled --output-dir, like the key it sets"),
    ],
)
def test_options_with_a_second_spelling_are_gone(option, reason):
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(["export", "--date", "2026-07-28", option])
    assert exc.value.code == 2, reason


def test_format_none_selects_snapshot_only(tmp_path, monkeypatch):
    from ha_history_exporter import ha_client
    from tests.helpers import FakeCliClient

    monkeypatch.setenv("HA_URL", "http://home-assistant.invalid")
    monkeypatch.setenv("HA_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("HHE_EXPORT_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["export", "--date", "2026-07-28", "--format", "none"]) == 0

    assert FakeCliClient.instances[0].history_calls == []
    assert not (tmp_path / "output" / "exports").exists()


def test_format_switch_reaches_the_configuration(tmp_path, monkeypatch):
    from ha_history_exporter import ha_client
    from tests.helpers import FakeCliClient

    monkeypatch.setenv("HA_URL", "http://home-assistant.invalid")
    monkeypatch.setenv("HA_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("HHE_EXPORT_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("HHE_STORAGE_TEMP_DIR", str(tmp_path / "temp"))
    monkeypatch.setenv("HHE_REQUESTS_SLEEP_BETWEEN_REQUESTS", "0")
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    captured = {}

    def fake_run_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(export_command, "run_export", fake_run_export)

    assert (
        cli.main(["export", "--date", "2026-07-28", "--format", "csv,parquet"]) == 0
    )

    assert captured["cfg"].export.formats == frozenset({Format.CSV, Format.PARQUET})


# ── logging ───────────────────────────────────────────────────────────────────

def test_repeated_runs_do_not_stack_log_handlers(tmp_path, monkeypatch):
    import logging

    from ha_history_exporter import ha_client
    from tests.helpers import FakeCliClient

    monkeypatch.setenv("HA_URL", "http://home-assistant.invalid")
    monkeypatch.setenv("HA_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("HHE_EXPORT_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("HHE_STORAGE_TEMP_DIR", str(tmp_path / "temp"))
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    before = len(logging.getLogger().handlers)
    for _ in range(3):
        cli.main(["export", "--date", "2026-07-28", "--dry-run"])
    after = len(logging.getLogger().handlers)

    assert after == before + 2  # exactly one stderr and one file handler
