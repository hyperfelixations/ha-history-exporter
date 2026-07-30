from __future__ import annotations

import json
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter import manifest
from tests.helpers import make_config


BERLIN = ZoneInfo("Europe/Berlin")
DAY = date(2026, 7, 28)
BOUNDS = {
    "start_local": "2026-07-28T00:00:00+02:00",
    "end_local": "2026-07-29T00:00:00+02:00",
    "start_utc": "2026-07-27T22:00:00+00:00",
    "end_utc": "2026-07-28T22:00:00+00:00",
}


def load_fresh(cfg):
    return manifest.load_or_create(
        day=DAY,
        tz_name="Europe/Berlin",
        cfg=cfg,
        **BOUNDS,
    )


def test_load_or_create_builds_fresh_manifest(tmp_path):
    cfg = make_config(tmp_path)

    item = load_fresh(cfg)

    assert item.date == str(DAY)
    assert item.status == "pending"
    assert item.schema_version == "1.3"
    assert item.script_version == "1.3.2"
    assert item.output_files == {"jsonl": None, "csv": None, "parquet": None}


def test_save_and_load_round_trip_ignores_private_timer(tmp_path):
    cfg = make_config(tmp_path)
    item = load_fresh(cfg)
    item.state_object_count = 12
    item._started_ts = 123.4

    manifest.save(item, cfg, 0, 0)
    raw = json.loads(cfg.day_file(DAY, "manifest.json").read_text(encoding="utf-8"))
    loaded = load_fresh(cfg)

    assert "_started_ts" not in raw
    assert loaded.state_object_count == 12
    assert loaded._started_ts is None


def test_load_ignores_unknown_future_fields(tmp_path):
    cfg = make_config(tmp_path)
    path = cfg.day_file(DAY, "manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"date": str(DAY), "status": "ok", "future_field": 42}),
        encoding="utf-8",
    )

    loaded = load_fresh(cfg)

    assert loaded.status == "ok"
    assert not hasattr(loaded, "future_field")


def test_corrupt_manifest_falls_back_to_fresh_manifest(tmp_path, caplog):
    cfg = make_config(tmp_path)
    path = cfg.day_file(DAY, "manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")

    loaded = load_fresh(cfg)

    assert loaded.status == "pending"
    assert loaded.date == str(DAY)
    assert "Could not parse manifest" in caplog.text


def test_mark_started_and_finished_records_duration(monkeypatch):
    item = manifest.DayManifest(date=str(DAY))
    ticks = iter([100.0, 103.26])
    monkeypatch.setattr(manifest.time, "monotonic", lambda: next(ticks))

    item.mark_started(BERLIN)
    item.mark_finished(BERLIN)

    assert item.status == "ok"
    assert item.export_started_at is not None
    assert item.export_finished_at is not None
    assert item.duration_seconds == 3.3
    assert manifest.is_complete(item)


def test_is_complete_only_accepts_ok():
    assert manifest.is_complete(manifest.DayManifest(status="ok"))
    assert not manifest.is_complete(manifest.DayManifest(status="pending"))
    assert not manifest.is_complete(manifest.DayManifest(status="failed"))


def test_mark_started_resets_previous_attempt_state():
    item = manifest.DayManifest(
        status="failed",
        export_finished_at="2026-07-28T12:00:00+02:00",
        duration_seconds=12.3,
        entity_count_with_history=8,
        entity_count_zero_history=2,
        request_count=5,
        failed_request_count=1,
        retried_request_count=2,
        state_object_count=99,
        output_files={
            "jsonl": "old.jsonl",
            "csv": "old.csv",
            "parquet": "old.parquet",
        },
        zero_history_entities=["sensor.old"],
        failed_batches=[{"batch_index": 1}],
        skipped_reason="synthetic previous skip",
        error="synthetic previous failure",
    )

    item.mark_started(BERLIN)

    assert item.status == "pending"
    assert item.export_finished_at is None
    assert item.duration_seconds is None
    assert item.entity_count_with_history == 0
    assert item.entity_count_zero_history == 0
    assert item.request_count == 0
    assert item.failed_request_count == 0
    assert item.retried_request_count == 0
    assert item.state_object_count == 0
    assert item.output_files == {"jsonl": None, "csv": None, "parquet": None}
    assert item.zero_history_entities == []
    assert item.failed_batches == []
    assert item.skipped_reason is None
    assert item.error is None
