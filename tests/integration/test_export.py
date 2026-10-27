"""Exporting days through the app layer with an in-memory Home Assistant."""

from __future__ import annotations

import csv
import itertools
import json
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.app.export import run_export
from ha_history_exporter.archive.lock import probe
from ha_history_exporter.domain.plan import ExportPlan
from ha_history_exporter.errors import AuthError, ConfigError
from ha_history_exporter.settings import Config
from tests.helpers import FakeHomeAssistantClient, make_config, make_export_plan, state_row

BERLIN = ZoneInfo("Europe/Berlin")
DAY = date(2026, 7, 28)
FORMATS = ("jsonl", "csv", "parquet")


def export(
    cfg: Config,
    responses: list[Any],
    entity_ids: list[str],
    *,
    plan: ExportPlan | None = None,
) -> tuple[int, FakeHomeAssistantClient]:
    client = FakeHomeAssistantClient(responses)
    result = run_export(
        cfg,
        plan or make_export_plan(DAY),
        entity_ids,
        len(entity_ids),
        client,
        BERLIN,
        sleep=lambda _seconds: None,
    )
    return result, client


def manifest_of(cfg: Config) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(cfg.layout.manifest_file(DAY).read_bytes())
    return data


def config(tmp_path: Path, formats: tuple[str, ...], **kwargs: Any) -> Config:
    if "parquet" in formats:
        pytest.importorskip("pyarrow")
    return make_config(
        tmp_path,
        jsonl="jsonl" in formats,
        csv="csv" in formats,
        parquet="parquet" in formats,
        **kwargs,
    )


def test_every_format_is_written_and_counted(tmp_path: Path) -> None:
    pq = pytest.importorskip("pyarrow.parquet")
    cfg = config(tmp_path, FORMATS)
    first = state_row("sensor.one", "1")
    second = state_row("sensor.two", "unknown", "2026-07-28T11:00:00+00:00")
    entities = ["sensor.one", "sensor.two", "sensor.no_history"]

    result, client = export(cfg, [[[first], [second]], []], entities)

    assert result == 0
    assert [call["entity_ids"] for call in client.calls] == [entities[:2], entities[2:]]
    jsonl = cfg.layout.day_file(DAY, "jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["entity_id"] for line in jsonl] == entities[:2]
    with cfg.layout.day_file(DAY, "csv").open(encoding="utf-8", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 2
    assert pq.read_metadata(cfg.layout.day_file(DAY, "parquet")).num_rows == 2
    manifest = manifest_of(cfg)
    assert manifest["status"] == "ok"
    assert manifest["entity_count_with_history"] == 2
    assert manifest["zero_history_entities"] == ["sensor.no_history"]
    assert manifest["request_count"] == 2
    log = cfg.layout.run_log.read_text(encoding="utf-8").splitlines()
    assert json.loads(log[-1])["status"] == "ok"


def test_an_empty_day_with_known_retention_is_published_empty(tmp_path: Path) -> None:
    cfg = config(tmp_path, ("jsonl", "csv"))

    result, _ = export(cfg, [[]], ["sensor.one"])

    assert result == 0
    assert cfg.layout.day_file(DAY, "jsonl").read_bytes() == b""
    assert manifest_of(cfg)["retention"] == {"status": "known_safe", "purge_keep_days": 14}


def test_an_empty_day_with_unknown_retention_fails_without_artifacts(tmp_path: Path) -> None:
    cfg = config(tmp_path, ("jsonl", "csv"), purge_keep_days=None)

    result, _ = export(cfg, [[]], ["sensor.one"])

    assert result == 1
    assert not cfg.layout.day_file(DAY, "jsonl").exists()
    assert not cfg.layout.day_file(DAY, "csv").exists()
    assert manifest_of(cfg)["status"] == "failed"
    assert manifest_of(cfg)["retention"]["status"] == "unknown"


def test_an_empty_plan_touches_nothing(tmp_path: Path) -> None:
    cfg = config(tmp_path, ("jsonl",))
    plan = ExportPlan(DAY, DAY, date(2026, 7, 30), ())

    result, client = export(cfg, [], [], plan=plan)

    assert (result, client.calls) == (0, [])
    assert not cfg.layout.root.exists()


def test_a_snapshot_only_configuration_asks_for_no_history(tmp_path: Path) -> None:
    cfg = config(tmp_path, ())

    result, client = export(cfg, [], ["sensor.one"])

    assert (result, client.calls) == (0, [])
    assert not cfg.layout.manifest_file(DAY).exists()


def test_a_failed_batch_fails_the_day_without_artifacts(tmp_path: Path) -> None:
    cfg = config(tmp_path, ("jsonl",), batch_size=1)

    result, client = export(
        cfg,
        [RuntimeError("synthetic batch failure"), [[state_row("sensor.two")]]],
        ["sensor.one", "sensor.two"],
    )

    assert result == 1
    assert len(client.calls) == 2
    assert not cfg.layout.day_file(DAY, "jsonl").exists()
    manifest = manifest_of(cfg)
    assert manifest["status"] == "failed"
    assert manifest["failed_batches"] == [
        {
            "batch_index": 1,
            "entity_count": 1,
            "error_code": "unexpected_error",
            "error": "Unexpected error while fetching this batch.",
        }
    ]


def test_a_failed_forced_export_keeps_the_previous_generation(tmp_path: Path) -> None:
    cfg = config(tmp_path, ("jsonl", "csv"))
    assert export(cfg, [[[state_row("sensor.one")]]], ["sensor.one"])[0] == 0
    directory = cfg.layout.manifest_file(DAY).parent
    before = {path.name: path.read_bytes() for path in directory.iterdir()}

    outside_the_day = state_row("sensor.one", timestamp="2026-07-30T00:00:00+00:00")
    assert export(cfg, [[[outside_the_day]]], ["sensor.one"])[0] == 1

    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


def test_an_authentication_failure_records_the_day_and_ends_the_run(tmp_path: Path) -> None:
    cfg = config(tmp_path, ("jsonl",), batch_size=1)
    client = FakeHomeAssistantClient([AuthError("synthetic authentication failure"), [[]]])

    with pytest.raises(AuthError):
        run_export(cfg, make_export_plan(DAY), ["sensor.one", "sensor.two"], 2, client, BERLIN)

    assert len(client.calls) == 1
    assert manifest_of(cfg)["status"] == "failed"
    assert "synthetic authentication failure" in manifest_of(cfg)["error"]
    assert json.loads(cfg.layout.run_log.read_text(encoding="utf-8"))["status"] == "failed"


def test_a_run_leaves_only_its_output_behind(tmp_path: Path) -> None:
    cfg = config(tmp_path, ("jsonl",))
    cfg.resolved_temp_dir.mkdir(parents=True)
    foreign = cfg.resolved_temp_dir / "old.tmp"
    foreign.write_text("belongs to something else", encoding="utf-8")

    assert export(cfg, [[[state_row()]]], ["sensor.test_temperature"])[0] == 0

    assert [path.name for path in cfg.resolved_temp_dir.iterdir()] == ["old.tmp"]
    assert not probe(cfg.layout.lock_file).held
    assert not cfg.layout.staging_root.exists()
    assert not cfg.layout.journal_root.exists()


def _format_sets() -> list[tuple[str, ...]]:
    return [subset for size in (1, 2, 3) for subset in itertools.combinations(FORMATS, size)]


@pytest.mark.parametrize("formats", _format_sets())
def test_exactly_the_requested_formats_are_published(
    tmp_path: Path, formats: tuple[str, ...]
) -> None:
    cfg = config(tmp_path, formats)

    assert export(cfg, [[[state_row()]]], ["sensor.test_temperature"])[0] == 0

    for fmt in FORMATS:
        assert cfg.layout.day_file(DAY, fmt).exists() is (fmt in formats)
    assert manifest_of(cfg)["output_files"] == {
        fmt: f"{DAY}.{fmt}" if fmt in formats else None for fmt in FORMATS
    }


@pytest.mark.parametrize(("old", "new"), list(itertools.product(_format_sets(), repeat=2)))
def test_a_new_generation_never_leaves_files_of_dropped_formats(
    tmp_path: Path, old: tuple[str, ...], new: tuple[str, ...]
) -> None:
    assert export(config(tmp_path, old), [[[state_row()]]], ["sensor.test_temperature"])[0] == 0
    cfg = config(tmp_path, new)

    assert export(cfg, [[[state_row()]]], ["sensor.test_temperature"])[0] == 0

    directory = cfg.layout.manifest_file(DAY).parent
    assert sorted(path.name for path in directory.iterdir()) == sorted(
        [f"{DAY}.manifest.json", *(f"{DAY}.{fmt}" for fmt in new)]
    )


def test_parquet_without_pyarrow_stops_before_home_assistant_is_asked(
    tmp_path: Path, without_pyarrow: None
) -> None:
    cfg = make_config(tmp_path, parquet=True)

    with pytest.raises(ConfigError, match="pyarrow is not installed"):
        export(cfg, [], ["sensor.a"])

    assert not cfg.layout.root.exists()
