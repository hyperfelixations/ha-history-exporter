"""The export run against an in-memory Home Assistant and a clock that stands still."""

from __future__ import annotations

import csv
import itertools
import json
import logging
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.app.export import ExportHooks, ExportRequest, run_export
from ha_history_exporter.app.results import ExportResult
from ha_history_exporter.archive.descriptor import ZoneSource
from ha_history_exporter.archive.layout import ArchiveLayout
from ha_history_exporter.archive.lock import probe
from ha_history_exporter.domain.plan import DayAction, DaySelection
from ha_history_exporter.errors import (
    AuthError,
    ConfigError,
    ExitCode,
    HAAPIError,
    HAConnectionError,
    InternalError,
    TLSError,
)
from ha_history_exporter.settings import Config
from tests.support.builders import make_config, state_row
from tests.support.clock import FixedClock
from tests.support.fake_source import Answer, FakeHistorySource, Retried

BERLIN = ZoneInfo("Europe/Berlin")
DAY = date(2026, 7, 28)
YESTERDAY = date(2026, 7, 29)
OLD_AND_SAFE = DaySelection(start=DAY, end=YESTERDAY)
NOW = datetime(2026, 7, 30, 3, 30, 5, tzinfo=BERLIN)
FORMATS = ("jsonl", "csv", "parquet")
TEMPERATURE = "sensor.test_temperature"
IN_YESTERDAY = "2026-07-29T10:00:00+00:00"


def states(*entity_ids: str) -> list[dict[str, Any]]:
    return [{"entity_id": entity_id, "state": "on", "attributes": {}} for entity_id in entity_ids]


class Run:
    """One configuration and one Home Assistant, exported again and again."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.connected = 0
        self.attached: list[Path] = []
        self.lock_held_when_attached: list[bool] = []

    @property
    def layout(self) -> ArchiveLayout:
        return ArchiveLayout(self.cfg.export.output_dir)

    def export(
        self,
        source: FakeHistorySource,
        *,
        selection: DaySelection | None = None,
        force: bool = False,
        dry_run: bool = False,
        now: datetime = NOW,
    ) -> ExportResult:
        def connect() -> FakeHistorySource:
            self.connected += 1
            return source

        def attach(path: Path) -> None:
            self.attached.append(path)
            self.lock_held_when_attached.append(probe(self.layout.lock_file).held)

        return run_export(
            ExportRequest(selection or DaySelection.single(DAY), force=force, dry_run=dry_run),
            self.cfg,
            connect,
            clock=FixedClock(now),
            sleep=lambda _seconds: None,
            hooks=ExportHooks(attach_log=attach),
        )

    def manifest(self, day: date = DAY) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.layout.manifest_file(day).read_bytes())
        return data


def run(tmp_path: Path, formats: tuple[str, ...] = ("jsonl",), **kwargs: Any) -> Run:
    if "parquet" in formats:
        pytest.importorskip("pyarrow")
    cfg = make_config(
        tmp_path,
        jsonl="jsonl" in formats,
        csv="csv" in formats,
        parquet="parquet" in formats,
        **kwargs,
    )
    return Run(replace(cfg, export=replace(cfg.export, timezone=None)))


def source(answers: list[Answer], *entity_ids: str) -> FakeHistorySource:
    return FakeHistorySource(answers, states=states(*(entity_ids or (TEMPERATURE,))))


def test_every_format_is_written_and_counted(tmp_path: Path) -> None:
    pq = pytest.importorskip("pyarrow.parquet")
    export = run(tmp_path, FORMATS)
    first = state_row("sensor.one", "1")
    second = state_row("sensor.two", "unknown", "2026-07-28T11:00:00+00:00")
    entities = ("sensor.no_history", "sensor.one", "sensor.two")
    ha = source([[[first]], [[second]]], *entities)

    result = export.export(ha)

    assert result.exit_code is ExitCode.OK
    assert [call.entity_ids for call in ha.calls] == [entities[:2], entities[2:]]
    layout = export.layout
    jsonl = layout.day_file(DAY, "jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["entity_id"] for line in jsonl] == ["sensor.one", "sensor.two"]
    with layout.day_file(DAY, "csv").open(encoding="utf-8", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 2
    assert pq.read_metadata(layout.day_file(DAY, "parquet")).num_rows == 2
    manifest = export.manifest()
    assert manifest["status"] == "ok"
    assert manifest["zero_history_entities"] == ["sensor.no_history"]
    assert manifest["request_count"] == 2
    assert json.loads(layout.run_log.read_text(encoding="utf-8"))["status"] == "ok"


def test_one_clock_reading_names_the_run_its_log_and_its_snapshot(tmp_path: Path) -> None:
    export = run(tmp_path)
    result = export.export(source([[[state_row()]]]))
    assert result.log_file == export.layout.logs_dir / "ha_history_export_2026-07-30_033005.log"
    assert export.attached == [result.log_file]
    assert export.lock_held_when_attached == [True]
    assert result.snapshot_file is not None
    assert result.snapshot_file.name.startswith("entity_snapshot_20260730T033005_20260730T013005-")


def test_the_clock_reading_names_the_log_in_the_archive_zone(tmp_path: Path) -> None:
    export = run(tmp_path)
    result = export.export(source([[[state_row()]]]), now=NOW.astimezone(UTC))
    assert result.log_file is not None
    assert result.log_file.name == "ha_history_export_2026-07-30_033005.log"


def test_a_partial_day_ends_at_the_start_of_the_run(tmp_path: Path) -> None:
    export = run(tmp_path)
    today = date(2026, 7, 30)
    row = state_row(timestamp="2026-07-30T01:00:00+00:00")
    result = export.export(source([[[row]]]), selection=DaySelection.single(today))
    assert [day.status.value for day in result.days] == ["partial"]
    manifest = export.manifest(today)
    assert (manifest["status"], manifest["end_local"]) == ("partial", "2026-07-30T03:30:05+02:00")


def test_a_new_archive_takes_the_time_zone_of_home_assistant(tmp_path: Path) -> None:
    export = run(tmp_path)
    ha = FakeHistorySource([[[state_row()]]], states=states(TEMPERATURE), time_zone="UTC")
    result = export.export(ha)
    assert (result.archive_zone.zone.key, result.archive_zone.source) == (
        "UTC",
        ZoneSource.HOME_ASSISTANT,
    )
    assert export.manifest()["timezone"] == "UTC"
    assert json.loads(export.layout.descriptor_file.read_bytes())["timezone"] == "UTC"


def test_a_configured_zone_that_is_not_the_archives_stops_the_run(tmp_path: Path) -> None:
    export = run(tmp_path)
    export.export(source([[[state_row()]]]))
    other = replace(export.cfg, export=replace(export.cfg.export, timezone=ZoneInfo("UTC")))
    with pytest.raises(ConfigError, match="this archive's days are in Europe/Berlin"):
        Run(other).export(source([[[state_row()]]]), force=True)


def test_nothing_to_export_asks_home_assistant_nothing(tmp_path: Path) -> None:
    export = run(tmp_path)
    export.export(source([[[state_row()]]]))
    assert export.connected == 1
    result = export.export(source([]))
    assert export.connected == 1
    assert result.entities is None
    assert result.plan is not None
    assert result.plan.days_to_export == []


def test_a_dry_run_plans_and_writes_nothing(tmp_path: Path) -> None:
    export = run(tmp_path)
    planned: list[list[date]] = []
    ha = source([])
    result = run_export(
        ExportRequest(DaySelection(last_days=3), dry_run=True),
        export.cfg,
        lambda: ha,
        clock=FixedClock(NOW),
        hooks=ExportHooks(
            planned=lambda plan, _entities, _zone: planned.append(plan.days_to_export)
        ),
    )
    assert result.dry_run
    assert planned == [[date(2026, 7, 27), date(2026, 7, 28), date(2026, 7, 29)]]
    assert result.entities is not None
    assert result.entities.requested == (TEMPERATURE,)
    assert ha.calls == []
    assert not export.cfg.export.output_dir.exists()
    assert not export.cfg.storage.temp_dir.exists()


def test_a_snapshot_only_run_writes_the_snapshot_and_no_day(tmp_path: Path) -> None:
    export = run(tmp_path, ())
    ha = source([])
    result = export.export(ha)
    assert result.plan is None
    assert result.snapshot_file is not None
    assert result.snapshot_file.is_file()
    assert ha.calls == []
    assert not export.layout.manifest_file(DAY).exists()


def test_a_day_outside_the_retention_is_skipped_and_the_safe_days_are_exported(
    tmp_path: Path,
) -> None:
    export = run(tmp_path, purge_keep_days=2)
    ha = source([[[state_row(timestamp=IN_YESTERDAY)]]])
    result = export.export(ha, selection=OLD_AND_SAFE)
    assert result.plan is not None
    assert result.plan.retention_skipped == [DAY]
    assert [(day.day, day.status.value) for day in result.days] == [(YESTERDAY, "ok")]
    assert result.exit_code is ExitCode.FAILED
    assert [call.window.day for call in ha.calls] == [YESTERDAY]
    assert not export.layout.manifest_file(DAY).exists()
    assert export.manifest(YESTERDAY)["retention"] == {
        "status": "known_safe",
        "purge_keep_days": 2,
    }
    run_log = export.layout.run_log.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["day"] for line in run_log] == [str(YESTERDAY)]


def test_only_days_outside_the_retention_of_a_known_archive_ask_home_assistant_nothing(
    tmp_path: Path,
) -> None:
    export = Run(make_config(tmp_path, purge_keep_days=1))
    result = export.export(source([]))
    assert result.exit_code is ExitCode.FAILED
    assert result.plan is not None
    assert result.plan.retention_skipped == [DAY]
    assert export.connected == 0
    assert not export.cfg.export.output_dir.exists()


def test_only_days_outside_the_retention_of_a_new_archive_leave_no_snapshot(
    tmp_path: Path,
) -> None:
    export = run(tmp_path, purge_keep_days=1)
    result = export.export(source([]))
    assert result.exit_code is ExitCode.FAILED
    assert result.snapshot_file is None
    assert result.log_file is not None
    assert export.attached == [result.log_file]
    assert export.layout.descriptor_file.is_file()
    assert not export.layout.metadata_dir.exists()
    assert not export.layout.daily_root.exists()


def test_a_saved_day_outside_the_retention_cannot_be_forced_and_stays_as_it_is(
    tmp_path: Path,
) -> None:
    export = run(tmp_path, ("jsonl", "csv"))
    assert export.export(source([[[state_row()]]])).exit_code is ExitCode.OK
    directory = export.layout.day_dir(DAY)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    ha = source([])
    result = export.export(ha, force=True, now=NOW + timedelta(days=30))
    assert result.exit_code is ExitCode.FAILED
    assert result.plan is not None
    assert result.plan.retention_skipped == [DAY]
    assert ha.calls == []
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


def test_a_failed_day_and_a_skipped_day_are_both_reported(tmp_path: Path) -> None:
    export = run(tmp_path, purge_keep_days=2)
    result = export.export(source([HAAPIError("Synthetic failure.")]), selection=OLD_AND_SAFE)
    assert result.exit_code is ExitCode.FAILED
    assert [(day.day, day.status.value) for day in result.days] == [(YESTERDAY, "failed")]
    assert result.plan is not None
    assert result.plan.retention_skipped == [DAY]


def test_a_failure_that_ends_the_run_keeps_its_own_exit_code_among_skipped_days(
    tmp_path: Path,
) -> None:
    export = run(tmp_path, purge_keep_days=2)
    ha = source([AuthError("Home Assistant rejected the access token (HTTP 401).")])
    with pytest.raises(AuthError) as caught:
        export.export(ha, selection=OLD_AND_SAFE)
    assert caught.value.exit_code is ExitCode.AUTHENTICATION
    assert export.manifest(YESTERDAY)["status"] == "failed"
    assert not export.layout.manifest_file(DAY).exists()


def test_repeating_a_range_saves_nothing_twice_and_still_reports_the_skipped_day(
    tmp_path: Path,
) -> None:
    export = run(tmp_path, purge_keep_days=2)
    export.export(source([[[state_row(timestamp=IN_YESTERDAY)]]]), selection=OLD_AND_SAFE)
    assert export.connected == 1
    result = export.export(source([]), selection=OLD_AND_SAFE)
    assert export.connected == 1
    assert result.exit_code is ExitCode.FAILED
    assert result.plan is not None
    assert result.plan.days(DayAction.SKIP_EXISTING) == [YESTERDAY]
    assert result.plan.retention_skipped == [DAY]


def test_a_saved_day_outside_the_retention_is_no_reason_to_report_anything(
    tmp_path: Path,
) -> None:
    export = run(tmp_path)
    export.export(source([[[state_row()]]]))
    later = NOW + timedelta(days=30)
    assert export.export(source([]), now=later).exit_code is ExitCode.OK


def test_a_dry_run_predicts_the_exit_code_of_the_skipped_day(tmp_path: Path) -> None:
    export = run(tmp_path, purge_keep_days=2)
    ha = source([])
    result = export.export(ha, selection=OLD_AND_SAFE, dry_run=True)
    assert result.exit_code is ExitCode.FAILED
    assert result.plan is not None
    assert result.plan.days_to_export == [YESTERDAY]
    assert ha.calls == []
    assert not export.cfg.export.output_dir.exists()


def test_the_warning_about_skipped_days_appears_once_and_before_the_first_day(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    export = Run(make_config(tmp_path, purge_keep_days=2))
    with caplog.at_level(logging.INFO):
        export.export(source([[[state_row(timestamp=IN_YESTERDAY)]]]), selection=OLD_AND_SAFE)
    warning = "Skipped 1 day(s) outside the 2-day Recorder retention: 2026-07-28."
    assert caplog.text.count(warning) == 1
    assert caplog.text.index(warning) < caplog.text.index("== Day 1/1")


@pytest.mark.parametrize("dry_run", [False, True], ids=["run", "dry run"])
def test_the_warning_about_an_unknown_retention_appears_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, dry_run: bool
) -> None:
    export = Run(make_config(tmp_path, purge_keep_days=None))
    with caplog.at_level(logging.WARNING):
        export.export(source([[[state_row()]]]), dry_run=dry_run)
    assert caplog.text.count("Recorder retention is not configured.") == 1


def test_a_run_with_a_known_retention_does_not_warn_about_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    export = Run(make_config(tmp_path))
    with caplog.at_level(logging.WARNING):
        result = export.export(source([[[state_row()]]]))
    assert result.exit_code is ExitCode.OK
    assert "Recorder retention" not in caplog.text


def test_an_empty_day_with_known_retention_is_published_empty(tmp_path: Path) -> None:
    export = run(tmp_path, ("jsonl", "csv"))
    assert export.export(source([[]])).exit_code is ExitCode.OK
    assert export.layout.day_file(DAY, "jsonl").read_bytes() == b""
    assert export.manifest()["retention"] == {"status": "known_safe", "purge_keep_days": 14}


def test_an_empty_day_with_unknown_retention_fails_without_artifacts(tmp_path: Path) -> None:
    export = run(tmp_path, ("jsonl", "csv"), purge_keep_days=None)
    assert export.export(source([[]])).exit_code is ExitCode.FAILED
    assert not export.layout.day_file(DAY, "jsonl").exists()
    assert export.manifest()["status"] == "failed"


def test_the_first_failed_batch_fails_the_day_and_the_next_day_still_runs(
    tmp_path: Path,
) -> None:
    export = run(tmp_path, batch_size=1)
    entities = ("sensor.one", "sensor.three", "sensor.two")
    ha = source(
        [
            HAAPIError("Synthetic batch failure."),
            [[state_row("sensor.one", timestamp="2026-07-29T10:00:00+00:00")]],
            [],
            [],
        ],
        *entities,
    )
    result = export.export(ha, selection=DaySelection(start=DAY, end=date(2026, 7, 29)))
    assert [(day.day, day.status.value) for day in result.days] == [
        (DAY, "failed"),
        (date(2026, 7, 29), "ok"),
    ]
    assert result.exit_code is ExitCode.FAILED
    assert len(ha.calls) == 4
    manifest = export.manifest()
    assert manifest["error"] == "History batch 1 of 3 failed for 2026-07-28."
    assert manifest["failed_batches"] == [
        {
            "batch_index": 1,
            "entity_count": 1,
            "error_code": "home_assistant_api_error",
            "error": "Synthetic batch failure.",
        }
    ]


@pytest.mark.parametrize(
    "error",
    [
        HAConnectionError("Home Assistant is not available."),
        TLSError("The HTTPS certificate of Home Assistant could not be verified."),
        AuthError("Home Assistant rejected the access token (HTTP 401)."),
    ],
    ids=["unavailable", "tls", "auth"],
)
def test_a_failure_no_request_can_survive_records_the_day_and_ends_the_run(
    tmp_path: Path, error: Exception
) -> None:
    export = run(tmp_path)
    ha = source([error, [[state_row(timestamp="2026-07-29T10:00:00+00:00")]]])
    with pytest.raises(type(error)):
        export.export(ha, selection=DaySelection(start=DAY, end=date(2026, 7, 29)))
    assert len(ha.calls) == 1
    manifest = export.manifest()
    assert (manifest["status"], manifest["error"]) == ("failed", str(error))
    assert len(manifest["failed_batches"]) == (0 if isinstance(error, AuthError) else 1)
    assert not export.layout.manifest_file(date(2026, 7, 29)).exists()
    run_log = export.layout.run_log.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["day"] for line in run_log] == [str(DAY)]
    assert not probe(export.layout.lock_file).held


def test_a_defect_is_logged_with_its_traceback_and_ends_the_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    export = run(tmp_path)
    with caplog.at_level(logging.ERROR), pytest.raises(InternalError) as caught:
        export.export(source([ZeroDivisionError("synthetic defect")]))
    assert caught.value.exit_code is ExitCode.INTERNAL
    assert [record.exc_info is not None for record in caplog.records].count(True) == 1
    assert "ZeroDivisionError" in caplog.text
    manifest = export.manifest()
    assert (manifest["status"], manifest["error_code"]) == ("failed", "unexpected_error")


def test_an_interruption_is_recorded_and_propagates(tmp_path: Path) -> None:
    export = run(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        export.export(source([KeyboardInterrupt()]))
    manifest = export.manifest()
    assert (manifest["status"], manifest["error_code"]) == ("failed", "interrupted")
    assert not probe(export.layout.lock_file).held


def test_a_new_failure_replaces_an_old_failure_but_never_a_success(tmp_path: Path) -> None:
    export = run(tmp_path)
    export.export(source([HAAPIError("First failure.")]))
    assert export.manifest()["failed_batches"][0]["error"] == "First failure."
    export.export(source([HAAPIError("Second failure.")]))
    assert export.manifest()["failed_batches"][0]["error"] == "Second failure."
    assert export.export(source([[[state_row()]]])).exit_code is ExitCode.OK
    success = export.layout.manifest_file(DAY).read_bytes()
    failed = export.export(source([HAAPIError("Third failure.")]), force=True)
    assert failed.exit_code is ExitCode.FAILED
    assert export.layout.manifest_file(DAY).read_bytes() == success


def test_a_failed_forced_export_keeps_the_previous_generation(tmp_path: Path) -> None:
    export = run(tmp_path, ("jsonl", "csv"))
    assert export.export(source([[[state_row()]]])).exit_code is ExitCode.OK
    directory = export.layout.day_dir(DAY)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    outside_the_day = state_row(timestamp="2026-07-30T00:00:00+00:00")
    assert export.export(source([[[outside_the_day]]]), force=True).exit_code is ExitCode.FAILED
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


def test_retries_of_every_batch_count_also_when_one_fails(tmp_path: Path) -> None:
    export = run(tmp_path, batch_size=1)
    answers: list[Answer] = [
        Retried([[state_row("sensor.one")]], attempts=3),
        Retried(HAAPIError("Synthetic batch failure."), attempts=4),
    ]
    result = export.export(source(answers, "sensor.one", "sensor.two"))
    assert result.exit_code is ExitCode.FAILED
    manifest = export.manifest()
    assert manifest["retried_request_count"] == 5
    assert (manifest["request_count"], manifest["failed_request_count"]) == (1, 1)


def test_a_run_leaves_only_its_output_behind(tmp_path: Path) -> None:
    export = run(tmp_path)
    temp = export.cfg.storage.temp_dir
    temp.mkdir(parents=True)
    (temp / "old.tmp").write_text("belongs to something else", encoding="utf-8")
    export.export(source([[[state_row()]]]))
    assert [path.name for path in temp.iterdir()] == ["old.tmp"]
    assert not export.layout.staging_root.exists()
    assert not export.layout.journal_root.exists()


def _format_sets() -> list[tuple[str, ...]]:
    return [subset for size in (1, 2, 3) for subset in itertools.combinations(FORMATS, size)]


@pytest.mark.parametrize(("old", "new"), list(itertools.product(_format_sets(), repeat=2)))
def test_a_new_generation_never_leaves_files_of_dropped_formats(
    tmp_path: Path, old: tuple[str, ...], new: tuple[str, ...]
) -> None:
    run(tmp_path, old).export(source([[[state_row()]]]))
    export = run(tmp_path, new)
    assert export.export(source([[[state_row()]]]), force=True).exit_code is ExitCode.OK
    assert sorted(path.name for path in export.layout.day_dir(DAY).iterdir()) == sorted(
        [f"{DAY}.manifest.json", *(f"{DAY}.{fmt}" for fmt in new)]
    )
    assert export.manifest()["output_files"] == {
        fmt: f"{DAY}.{fmt}" if fmt in new else None for fmt in FORMATS
    }


def test_parquet_without_pyarrow_stops_before_home_assistant_is_asked(
    tmp_path: Path, without_pyarrow: None
) -> None:
    export = Run(make_config(tmp_path, parquet=True))
    with pytest.raises(ConfigError, match="pyarrow is not installed"):
        export.export(source([]))
    assert export.connected == 0
    assert not export.cfg.export.output_dir.exists()


def test_excluded_entities_are_counted_but_not_requested(tmp_path: Path) -> None:
    base = run(tmp_path)
    cfg = replace(base.cfg, entities=replace(base.cfg.entities, exclude_domains=("update",)))
    ha = FakeHistorySource(
        [[[state_row()]]],
        states=[*states(TEMPERATURE, "update.core"), {"entity_id": "sensor.x", "state": "unknown"}],
    )
    export = Run(cfg)
    result = export.export(ha)
    assert result.entities is not None
    assert (result.entities.candidates, result.entities.excluded) == (3, 1)
    assert result.entities.requested == ("sensor.test_temperature", "sensor.x")
    assert result.entities.unknown == 1
    assert export.manifest()["entity_count_current"] == 3
