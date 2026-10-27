from __future__ import annotations

from datetime import date

import pytest

from ha_history_exporter.cli.commands.export import print_plan
from ha_history_exporter.domain.plan import DayAction, DayDecision, ExportPlan

PLAN = ExportPlan(
    requested_start=date(2026, 7, 28),
    requested_end=date(2026, 7, 30),
    today=date(2026, 7, 30),
    decisions=(
        DayDecision(date(2026, 7, 28), DayAction.EXPORT),
        DayDecision(date(2026, 7, 29), DayAction.SKIP_EXISTING),
        DayDecision(date(2026, 7, 30), DayAction.SKIP_INCOMPLETE),
    ),
)


def test_the_plan_names_output_configuration_and_every_category(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_plan(
        PLAN,
        entity_count=5,
        unknown_count=1,
        unavailable_count=2,
        batch_size=2,
        output_dir="/archive/history",
        config_source="/home/you/.config/ha-history-exporter/config.yaml",
        log_file="/archive/history/logs/run.log",
    )
    output = capsys.readouterr().out
    assert "Output          : /archive/history" in output
    assert "Configuration   : /home/you/.config/ha-history-exporter/config.yaml" in output
    assert "Log file        : /archive/history/logs/run.log" in output
    assert "Latest complete : 2026-07-29" in output
    assert "Requests / day  : ~3" in output
    assert "Will export (1)" in output
    assert "already exported (1)" in output
    assert "not yet complete (1)" in output


def test_a_missing_log_file_is_left_out(capsys: pytest.CaptureFixture[str]) -> None:
    print_plan(
        PLAN,
        entity_count=0,
        unknown_count=0,
        unavailable_count=0,
        batch_size=2,
        output_dir="/archive",
        config_source="built-in defaults only",
        log_file=None,
    )
    output = capsys.readouterr().out
    assert "Log file" not in output
    assert "Requests / day  : ~0" in output
