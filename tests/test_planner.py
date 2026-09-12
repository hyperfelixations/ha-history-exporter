from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter import manifest, planner
from ha_history_exporter.errors import ConfigError, ExportError
from ha_history_exporter.settings import RecorderSettings
from tests.helpers import make_config

BERLIN = ZoneInfo("Europe/Berlin")
DAY = date(2026, 7, 28)


def write_manifest(cfg, *, status="ok", output_files=None):
    item = manifest.create(
        DAY,
        "Europe/Berlin",
        "2026-07-28T00:00:00+02:00",
        "2026-07-29T00:00:00+02:00",
        "2026-07-27T22:00:00+00:00",
        "2026-07-28T22:00:00+00:00",
        cfg,
    )
    item.status = status
    item.state_object_count = 4
    item.entity_count_requested = 1
    item.entity_count_with_history = 1
    selected = output_files or {
        "jsonl": f"{DAY}.jsonl",
        "csv": None,
        "parquet": None,
    }
    if status == "ok":
        item.output_files = selected
        item.artifacts = {
            name: (
                {
                    "filename": filename,
                    "size_bytes": 100,
                    "sha256": "a" * 64,
                    "row_count": 4,
                    "logical_sha256": "b" * 64,
                }
                if filename is not None
                else None
            )
            for name, filename in selected.items()
        }
    elif status == "failed":
        item.error = "Synthetic export failure."
        item.error_code = "export_failed"
    manifest.save(item, cfg, 0, 0)
    return cfg.layout.day_file(DAY, "manifest.json")


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


def test_invalid_manifest_stops_instead_of_re_exporting(tmp_path):
    cfg = make_config(tmp_path)
    path = cfg.layout.day_file(DAY, "manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    cfg.layout.day_file(DAY, "jsonl").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ExportError, match="valid JSON"):
        planner._check_existing(DAY, cfg)


def test_force_does_not_bypass_an_invalid_manifest(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    patch_calendar(monkeypatch)
    path = cfg.layout.day_file(DAY, "manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ExportError, match="valid JSON"):
        planner.build_plan(DAY, DAY, BERLIN, cfg, force=True)


def test_known_retention_blocks_days_that_touch_or_exceed_the_boundary(tmp_path):
    cfg = make_config(tmp_path, purge_keep_days=14)

    inside = planner.build_plan(
        date(2026, 7, 17),
        date(2026, 7, 17),
        BERLIN,
        cfg,
        now=date(2026, 7, 30),
    )
    assert inside.days_to_export == [date(2026, 7, 17)]

    with pytest.raises(ConfigError, match="Recorder retention"):
        planner.build_plan(
            date(2026, 7, 16),
            date(2026, 7, 16),
            BERLIN,
            cfg,
            now=date(2026, 7, 30),
        )


def test_existing_ok_manifest_outside_retention_still_skips_without_force(
    tmp_path,
):
    cfg = make_config(tmp_path, purge_keep_days=14)
    write_manifest(cfg)

    plan = planner.build_plan(
        DAY,
        DAY,
        BERLIN,
        cfg,
        now=date(2026, 8, 20),
    )

    assert plan.days_skipped_existing == [DAY]
    with pytest.raises(ConfigError, match="Recorder retention"):
        planner.build_plan(
            DAY,
            DAY,
            BERLIN,
            cfg,
            force=True,
            now=date(2026, 8, 20),
        )


def test_unknown_retention_warns_but_allows_a_new_export(tmp_path, caplog):
    cfg = replace(make_config(tmp_path), recorder=RecorderSettings())

    with caplog.at_level("WARNING"):
        plan = planner.build_plan(
            DAY,
            DAY,
            BERLIN,
            cfg,
            now=date(2026, 8, 20),
        )

    assert plan.days_to_export == [DAY]
    assert "retention is not configured" in caplog.text


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
