"""Planning against manifests on disk: the manifest is the record of a day."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.app.export import ExportRequest, plan_export
from ha_history_exporter.archive.layout import ArchiveLayout
from ha_history_exporter.domain.formats import Format
from ha_history_exporter.domain.manifest import ManifestStatus
from ha_history_exporter.domain.plan import DayAction, DaySelection, ExportPlan
from ha_history_exporter.errors import ConfigError, ExportError
from ha_history_exporter.settings import Config, RecorderSettings
from tests.support.builders import make_config
from tests.support.manifests import DAY, manifest14, write_manifest

BERLIN = ZoneInfo("Europe/Berlin")
TODAY = date(2026, 7, 30)


def plan(cfg: Config, *, today: date = TODAY, force: bool = False) -> ExportPlan:
    return plan_export(
        cfg,
        ArchiveLayout(cfg.export.output_dir),
        ExportRequest(DaySelection.single(DAY), force=force),
        zone=BERLIN,
        today=today,
    )


def output(cfg: Config) -> Path:
    return cfg.export.output_dir


def test_a_successful_manifest_skips_the_day_even_without_its_files(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    write_manifest(output(cfg), manifest14())
    decision = plan(cfg).decisions[0]
    assert decision.action is DayAction.SKIP_EXISTING
    assert decision.reason == "manifest.json status=ok, 4 state objects"


def test_force_exports_a_successful_day_again(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    write_manifest(output(cfg), manifest14())
    assert plan(cfg, force=True).days_to_export == [DAY]


def test_a_failed_manifest_does_not_skip(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    write_manifest(output(cfg), manifest14(status=ManifestStatus.FAILED))
    assert plan(cfg).days_to_export == [DAY]


def test_a_newly_requested_format_never_recaptures_a_successful_day(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, jsonl=True, csv=True, parquet=True)
    write_manifest(output(cfg), manifest14(formats=[Format.JSONL, Format.PARQUET]))
    assert plan(cfg).days(DayAction.SKIP_EXISTING) == [DAY]


@pytest.mark.parametrize("force", [False, True])
def test_an_invalid_manifest_stops_the_plan(tmp_path: Path, force: bool) -> None:
    cfg = make_config(tmp_path)
    write_manifest(output(cfg), manifest14()).write_bytes(b"{broken")
    with pytest.raises(ExportError, match="valid JSON"):
        plan(cfg, force=force)


def test_an_unreadable_manifest_stops_the_plan(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    path = write_manifest(output(cfg), manifest14())
    path.unlink()
    path.mkdir()
    with pytest.raises(ExportError, match="Cannot read the manifest"):
        plan(cfg)


def test_known_retention_blocks_days_at_or_beyond_the_boundary(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, purge_keep_days=14)
    assert plan(cfg, today=date(2026, 8, 10)).days_to_export == [DAY]
    with pytest.raises(ConfigError, match="Recorder retention blocks") as caught:
        plan(cfg, today=date(2026, 8, 11))
    assert str(DAY) in (caught.value.details or "")


def test_a_successful_day_beyond_retention_is_skipped_but_cannot_be_forced(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, purge_keep_days=14)
    write_manifest(output(cfg), manifest14())
    assert plan(cfg, today=date(2026, 8, 20)).days(DayAction.SKIP_EXISTING) == [DAY]
    with pytest.raises(ConfigError, match="Recorder retention"):
        plan(cfg, today=date(2026, 8, 20), force=True)


def test_unknown_retention_warns_but_allows_a_new_export(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = replace(make_config(tmp_path), recorder=RecorderSettings())
    with caplog.at_level("WARNING"):
        assert plan(cfg, today=date(2026, 8, 20)).days_to_export == [DAY]
    assert "retention is not configured" in caplog.text
