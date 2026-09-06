from __future__ import annotations

import json
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter import planner
from tests.helpers import make_config

BERLIN = ZoneInfo("Europe/Berlin")
DAY = date(2026, 7, 28)


def write_manifest(cfg, *, status="ok", output_files=None):
    path = cfg.day_file(DAY, "manifest.json")
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

    plan = planner.build_plan(DAY, DAY, BERLIN, cfg, force=False, resume=True)

    assert plan.days_to_export == [DAY]
    assert plan.days_skipped_existing == []


def test_build_plan_skips_incomplete_days(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    patch_calendar(monkeypatch)
    today = date(2026, 7, 30)

    plan = planner.build_plan(today, today, BERLIN, cfg, force=False, resume=True)

    assert plan.days_skipped_incomplete == [today]


def test_build_plan_skips_existing_ok_manifest(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    patch_calendar(monkeypatch)
    write_manifest(cfg)

    plan = planner.build_plan(DAY, DAY, BERLIN, cfg, force=False, resume=True)

    assert plan.days_skipped_existing == [DAY]
    assert "status=ok" in plan.decisions[0].reason


def test_force_reexports_existing_day(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    patch_calendar(monkeypatch)
    write_manifest(cfg)
    cfg.day_file(DAY, "jsonl").write_text("{}\n", encoding="utf-8")

    plan = planner.build_plan(DAY, DAY, BERLIN, cfg, force=True, resume=False)

    assert plan.days_to_export == [DAY]


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_non_ok_manifest_does_not_skip(tmp_path, status):
    cfg = make_config(tmp_path)
    write_manifest(cfg, status=status)
    cfg.day_file(DAY, "jsonl").write_text("{}\n", encoding="utf-8")

    assert planner._check_existing(DAY, cfg) is None


def test_invalid_manifest_does_not_skip(tmp_path):
    cfg = make_config(tmp_path)
    path = cfg.day_file(DAY, "manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    cfg.day_file(DAY, "jsonl").write_text("{}\n", encoding="utf-8")

    assert planner._check_existing(DAY, cfg) is None


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
    cfg.day_file(DAY, "jsonl").write_text("{}\n", encoding="utf-8")
    cfg.day_file(DAY, "parquet").write_bytes(b"synthetic")

    plan = planner.build_plan(DAY, DAY, BERLIN, cfg, force=False, resume=True)

    assert plan.decisions[0].action == "derive"
