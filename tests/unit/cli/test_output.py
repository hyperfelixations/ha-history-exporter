from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.app.results import (
    Check,
    CheckStatus,
    DayResult,
    EntitySummary,
    ExportResult,
)
from ha_history_exporter.archive.descriptor import ArchiveZone, ZoneSource
from ha_history_exporter.cli.output import print_checks, print_export_result, print_plan
from ha_history_exporter.domain.manifest import ManifestStatus
from ha_history_exporter.domain.plan import DayAction, DayDecision, ExportPlan
from ha_history_exporter.errors import ExitCode

PLAN = ExportPlan(
    requested_start=date(2026, 7, 28),
    requested_end=date(2026, 7, 30),
    today=date(2026, 7, 30),
    decisions=(
        DayDecision(date(2026, 7, 28), DayAction.EXPORT),
        DayDecision(date(2026, 7, 29), DayAction.SKIP_EXISTING),
        DayDecision(date(2026, 7, 30), DayAction.EXPORT_PARTIAL),
    ),
)
SKIPPING_PLAN = ExportPlan(
    requested_start=date(2026, 7, 16),
    requested_end=date(2026, 7, 28),
    today=date(2026, 7, 30),
    decisions=(
        DayDecision(date(2026, 7, 16), DayAction.SKIP_RETENTION),
        DayDecision(date(2026, 7, 28), DayAction.EXPORT),
    ),
)
ENTITIES = EntitySummary(
    candidates=7,
    requested=("a.b", "c.d", "e.f", "g.h", "i.j"),
    excluded=2,
    unknown=1,
    unavailable=2,
)
ZONE = ArchiveZone(ZoneInfo("Europe/Berlin"), ZoneSource.HOME_ASSISTANT)


def test_the_plan_names_where_it_writes_and_every_category(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_plan(
        PLAN,
        ENTITIES,
        ZONE,
        batch_size=2,
        output_dir=Path("archive"),
        config_source="config.toml",
        log_file=Path("archive/logs/run.log"),
    )
    output = capsys.readouterr().out
    for expected in (
        f"Output          : {Path('archive')}",
        "Configuration   : config.toml",
        f"Log file        : {Path('archive/logs/run.log')}",
        "Time zone       : Europe/Berlin (from Home Assistant)",
        "Latest complete : 2026-07-29",
        "Entities        : 5 requested of 7",
        "Requests / day  : ~3",
        "Will export (2):",
        "2026-07-30  (partial - today is not over yet)",
        "Will skip - already exported (1):",
    ):
        assert expected in output


def test_a_plan_without_log_file_or_days_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    empty = ExportPlan(date(2026, 7, 30), date(2026, 7, 30), date(2026, 7, 30), ())
    nobody = EntitySummary(candidates=0, requested=(), excluded=0, unknown=0, unavailable=0)
    print_plan(
        empty,
        nobody,
        ZONE,
        batch_size=2,
        output_dir=Path("archive"),
        config_source="built-in defaults only",
        log_file=None,
    )
    output = capsys.readouterr().out
    assert "Log file" not in output
    assert "Requests / day  : ~0" in output
    assert "Nothing to export." in output


def test_the_result_lists_every_day_and_where_the_log_is(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = ExportResult(
        ZONE,
        PLAN,
        ENTITIES,
        (
            DayResult(date(2026, 7, 28), ManifestStatus.OK, 12),
            DayResult(date(2026, 7, 30), ManifestStatus.FAILED, 0, "Synthetic failure."),
        ),
        log_file=Path("run.log"),
    )
    print_export_result(result)
    assert capsys.readouterr().out.splitlines() == [
        "2026-07-28  ok       12 state objects",
        "2026-07-30  failed   0 state objects  - Synthetic failure.",
        "Log file: run.log",
    ]


def test_the_plan_lists_the_days_outside_the_retention(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_plan(
        SKIPPING_PLAN,
        ENTITIES,
        ZONE,
        batch_size=2,
        output_dir=Path("archive"),
        config_source="config.toml",
        log_file=None,
    )
    output = capsys.readouterr().out
    assert "Will skip - outside the Recorder retention (1):\n    2026-07-16" in output


def test_the_result_lists_the_days_outside_the_retention_and_exits_with_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = ExportResult(
        ZONE, SKIPPING_PLAN, ENTITIES, (DayResult(date(2026, 7, 28), ManifestStatus.OK, 12),)
    )
    print_export_result(result)
    assert capsys.readouterr().out.splitlines() == [
        "2026-07-28  ok       12 state objects",
        "2026-07-16  skipped  outside the Recorder retention",
    ]
    assert result.exit_code is ExitCode.FAILED


def test_a_result_without_any_saved_day_still_lists_the_skipped_days(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_export_result(ExportResult(ZONE, SKIPPING_PLAN))
    assert capsys.readouterr().out.splitlines() == [
        "2026-07-16  skipped  outside the Recorder retention"
    ]


def test_checks_end_with_a_verdict(capsys: pytest.CaptureFixture[str]) -> None:
    print_checks(
        [
            Check(CheckStatus.OK, "version", "1.4.0"),
            Check(CheckStatus.WARN, "configuration", "no file", "hhe init"),
            Check(CheckStatus.FAIL, "token", "missing", "hhe config set homeassistant.token"),
        ]
    )
    output = capsys.readouterr().out
    assert "[warn] configuration  no file" in output
    assert "-> hhe config set homeassistant.token" in output
    assert output.rstrip().endswith("1 problem(s) need attention, 1 warning(s).")
