from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter import planner
from ha_history_exporter.errors import ExportError
from tests.helpers import make_config

BERLIN = ZoneInfo("Europe/Berlin")
DAY = date(2026, 7, 28)


def write_manifest(cfg, *, status="ok", output_files=None):
    path = cfg.layout.day_file(DAY, "manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "date": str(DAY),
                "status": status,
                "state_object_count": 4,
                "output_files": output_files
                or {"jsonl": f"{DAY}.jsonl", "csv": None, "parquet": None},
            }
        ),
        encoding="utf-8",
    )
    return path


def patch_calendar(monkeypatch):
    monkeypatch.setattr(planner, "today_local", lambda tz: date(2026, 7, 30))
    monkeypatch.setattr(
        planner, "latest_complete_day", lambda tz: date(2026, 7, 29)
    )
    monkeypatch.setattr(
        planner,
        "is_day_complete",
        lambda day, tz: day < date(2026, 7, 30),
    )


def test_build_plan_exports_complete_missing_days(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    patch_calendar(monkeypatch)

    plan = planner.build_plan(DAY, DAY, BERLIN, cfg, force=False)

    assert plan.days_to_export == [DAY]
    assert plan.days_skipped_existing == []


def test_build_plan_skips_incomplete_days(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    patch_calendar(monkeypatch)
    today = date(2026, 7, 30)

    plan = planner.build_plan(today, today, BERLIN, cfg, force=False)

    assert plan.days_skipped_incomplete == [today]


def test_build_plan_skips_existing_ok_manifest(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    patch_calendar(monkeypatch)
    write_manifest(cfg)

    plan = planner.build_plan(DAY, DAY, BERLIN, cfg, force=False)

    assert plan.days_skipped_existing == [DAY]
    assert "status=ok" in plan.decisions[0].reason


def test_force_reexports_existing_day(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    patch_calendar(monkeypatch)
    write_manifest(cfg)
    cfg.layout.day_file(DAY, "jsonl").write_text("{}\n", encoding="utf-8")

    plan = planner.build_plan(DAY, DAY, BERLIN, cfg, force=True)

    assert plan.days_to_export == [DAY]


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_non_ok_manifest_does_not_skip(tmp_path, status):
    cfg = make_config(tmp_path)
    write_manifest(cfg, status=status)
    cfg.layout.day_file(DAY, "jsonl").write_text("{}\n", encoding="utf-8")

    assert planner._check_existing(DAY, cfg) is None


def test_invalid_manifest_warns_and_does_not_skip(tmp_path, caplog):
    """A broken manifest is a statement about the run, not about readability."""
    cfg = make_config(tmp_path)
    path = cfg.layout.day_file(DAY, "manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    cfg.layout.day_file(DAY, "jsonl").write_text("{}\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        assert planner._check_existing(DAY, cfg) is None

    assert str(path) in caplog.text


def test_an_unreadable_manifest_stops_the_run_instead_of_re_exporting(
    tmp_path, monkeypatch
):
    """The manifest is the only durable record that a day was captured.

    Treating an unreadable one as "not exported" would silently overwrite a
    finished day.
    """
    cfg = make_config(tmp_path)
    target = write_manifest(cfg)
    real_open = Path.open

    def refuse(self, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "Access is denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse)

    with pytest.raises(ExportError, match="manifest"):
        planner._check_existing(DAY, cfg)


def test_plan_properties_partition_decisions():
    plan = planner.ExportPlan(
        requested_start=DAY,
        requested_end=date(2026, 7, 30),
        today=date(2026, 7, 30),
        latest_complete=date(2026, 7, 29),
        decisions=[
            planner.DayDecision(DAY, "export"),
            planner.DayDecision(date(2026, 7, 29), "skip_existing"),
            planner.DayDecision(date(2026, 7, 30), "skip_incomplete"),
        ],
    )

    assert plan.days_to_export == [DAY]
    assert plan.days_skipped_existing == [date(2026, 7, 29)]
    assert plan.days_skipped_incomplete == [date(2026, 7, 30)]


def test_print_summary_reports_every_decision_category(capsys):
    plan = planner.ExportPlan(
        requested_start=DAY,
        requested_end=date(2026, 7, 30),
        today=date(2026, 7, 30),
        latest_complete=date(2026, 7, 29),
        decisions=[
            planner.DayDecision(DAY, "export"),
            planner.DayDecision(date(2026, 7, 29), "skip_existing"),
            planner.DayDecision(date(2026, 7, 30), "skip_incomplete"),
        ],
    )

    plan.print_summary(
        entity_count=5,
        unknown_count=1,
        unavailable_count=2,
        batch_size=2,
    )

    output = capsys.readouterr().out
    assert "Requests / day  : ~3" in output
    assert "Will export (1)" in output
    assert "already exported (1)" in output
    assert "not yet complete (1)" in output


def make_plan() -> planner.ExportPlan:
    return planner.ExportPlan(
        requested_start=DAY,
        requested_end=DAY,
        today=date(2026, 7, 30),
        latest_complete=date(2026, 7, 29),
        decisions=[planner.DayDecision(DAY, "export")],
    )


def test_print_summary_names_where_the_run_writes_and_what_configured_it(capsys):
    """The two facts a wrong run is recognised by, before anything is fetched."""
    make_plan().print_summary(
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


def test_print_summary_stays_usable_without_the_optional_context(capsys):
    make_plan().print_summary(
        entity_count=5, unknown_count=1, unavailable_count=2, batch_size=2
    )

    output = capsys.readouterr().out
    assert "Output" not in output
    assert "Will export (1)" in output


def test_successful_manifest_skips_when_artifacts_were_archived(tmp_path):
    cfg = make_config(tmp_path)
    write_manifest(cfg)

    assert planner._check_existing(DAY, cfg) is not None


@pytest.mark.known_bug
@pytest.mark.xfail(
    strict=True,
    reason="planner has no derive action for a newly requested missing format",
)
def test_newly_requested_format_plans_local_derivation(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, jsonl=True, csv=True, parquet=True)
    patch_calendar(monkeypatch)
    write_manifest(
        cfg,
        output_files={
            "jsonl": f"{DAY}.jsonl",
            "csv": None,
            "parquet": f"{DAY}.parquet",
        },
    )
    cfg.layout.day_file(DAY, "jsonl").write_text("{}\n", encoding="utf-8")
    cfg.layout.day_file(DAY, "parquet").write_bytes(b"synthetic")

    plan = planner.build_plan(DAY, DAY, BERLIN, cfg, force=False)

    assert plan.decisions[0].action == "derive"
