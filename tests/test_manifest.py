from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter import manifest
from ha_history_exporter.errors import ExportError
from tests.helpers import make_config

BERLIN = ZoneInfo("Europe/Berlin")
DAY = date(2026, 7, 28)
BOUNDS = {
    "start_local": "2026-07-28T00:00:00+02:00",
    "end_local": "2026-07-29T00:00:00+02:00",
    "start_utc": "2026-07-27T22:00:00+00:00",
    "end_utc": "2026-07-28T22:00:00+00:00",
}


def load(cfg):
    return manifest.load_or_create(
        day=DAY,
        tz_name="Europe/Berlin",
        cfg=cfg,
        **BOUNDS,
    )


def valid_data(schema_version: str = "1.4") -> dict:
    data = {
        "schema_version": schema_version,
        "status": "ok",
        "source": "home_assistant_rest_history",
        "date": str(DAY),
        "timezone": "Europe/Berlin",
        **BOUNDS,
        "export_started_at": "2026-07-28T00:01:00+02:00",
        "export_finished_at": "2026-07-28T00:02:00+02:00",
        "duration_seconds": 60.0,
        "entity_count_current": 2,
        "entity_count_requested": 2,
        "entity_count_with_history": 1,
        "entity_count_zero_history": 1,
        "state_object_count": 1,
        "batch_size_entities": 15,
        "request_count": 1,
        "failed_request_count": 0,
        "retried_request_count": 0,
        "history_request_options": {
            "minimal_response": False,
            "no_attributes": False,
            "significant_changes_only": False,
        },
        "output_files": {
            "jsonl": f"{DAY}.jsonl",
            "csv": None,
            "parquet": None,
        },
        "zero_history_entities": ["sensor.no_history"],
        "failed_batches": [],
        "skipped_reason": None,
        "error": None,
        "script_version": "1.3.2",
    }
    if schema_version == "1.4":
        data["history_request_options"]["skip_initial_state"] = True
        data.update(
            {
                "capture_profile": {
                    "response": "full",
                    "attributes": "full",
                    "state_changes": "all",
                    "initial_state": "omitted",
                },
                "retention": {"status": "known_safe", "purge_keep_days": 14},
                "artifacts": {
                    "jsonl": {
                        "filename": f"{DAY}.jsonl",
                        "size_bytes": 100,
                        "sha256": "a" * 64,
                        "row_count": 1,
                        "logical_sha256": "b" * 64,
                    },
                    "csv": None,
                    "parquet": None,
                },
                "error_code": None,
            }
        )
        data["script_version"] = "1.4.0"
    return data


def write_manifest(cfg, data: dict) -> None:
    path = cfg.layout.day_file(DAY, "manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_load_or_create_builds_fresh_manifest_1_4(tmp_path):
    cfg = make_config(tmp_path)

    item = load(cfg)

    assert item.date == str(DAY)
    assert item.status == "pending"
    assert item.schema_version == "1.4"
    assert item.script_version == "1.4.0"
    assert item.output_files == {"jsonl": None, "csv": None, "parquet": None}
    assert item.history_request_options["skip_initial_state"] is True


def test_save_and_load_round_trip_ignores_private_timer(tmp_path):
    cfg = make_config(tmp_path)
    item = load(cfg)
    item.state_object_count = 12
    item._started_ts = 123.4

    manifest.save(item, cfg, 0, 0)
    raw = json.loads(cfg.layout.day_file(DAY, "manifest.json").read_text(encoding="utf-8"))
    loaded = load(cfg)

    assert "_started_ts" not in raw
    assert loaded.state_object_count == 12
    assert loaded._started_ts is None


@pytest.mark.parametrize("schema_version", ["1.1", "1.2", "1.3"])
def test_valid_legacy_ok_manifests_remain_complete(tmp_path, schema_version):
    cfg = make_config(tmp_path)
    write_manifest(cfg, valid_data(schema_version))

    loaded = load(cfg)

    assert loaded.schema_version == schema_version
    assert manifest.is_complete(loaded)
    assert loaded.output_files == {
        "jsonl": f"{DAY}.jsonl",
        "csv": None,
        "parquet": None,
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data.update(schema_version="9.9"), "schema version"),
        (lambda data: data.update(status="invented"), "status"),
        (lambda data: data.update(date="2026-07-27"), "date"),
        (lambda data: data.update(timezone="UTC"), "timezone"),
        (lambda data: data.update(state_object_count=True), "state_object_count"),
        (
            lambda data: data["output_files"].update(jsonl="../outside.jsonl"),
            "output_files",
        ),
        (
            lambda data: data["history_request_options"].update(
                skip_initial_state="yes"
            ),
            "skip_initial_state",
        ),
        (lambda data: data.update(future_field=42), "unknown field"),
    ],
)
def test_manifest_contract_violations_fail_closed(tmp_path, mutation, message):
    cfg = make_config(tmp_path)
    data = deepcopy(valid_data())
    mutation(data)
    write_manifest(cfg, data)

    with pytest.raises(ExportError, match=message):
        load(cfg)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda data: data["artifacts"].update(jsonl=None),
            "artifacts.jsonl contradicts output_files",
        ),
        (
            lambda data: data["output_files"].update(jsonl=None),
            "artifacts.jsonl contradicts output_files",
        ),
        (
            lambda data: data.update(error="failure on a successful manifest"),
            "successful manifest contains an error",
        ),
        (
            lambda data: data.update(
                failed_batches=[
                    {
                        "batch_index": 1,
                        "entities": ["sensor.private"],
                        "error": "failure",
                    }
                ]
            ),
            "failed_batches entry",
        ),
        (
            lambda data: data.update(
                retention={"status": "known_safe", "purge_keep_days": None}
            ),
            "known_safe retention",
        ),
        (
            lambda data: data.update(
                retention={"status": "unknown", "purge_keep_days": 14}
            ),
            "unknown retention",
        ),
    ],
)
def test_manifest_1_4_cross_field_invariants_fail_closed(
    tmp_path, mutation, message
):
    cfg = make_config(tmp_path)
    data = deepcopy(valid_data())
    mutation(data)
    write_manifest(cfg, data)

    with pytest.raises(ExportError, match=message):
        load(cfg)


def test_corrupt_manifest_fails_closed(tmp_path):
    cfg = make_config(tmp_path)
    path = cfg.layout.day_file(DAY, "manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ExportError, match="valid JSON"):
        load(cfg)


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
        artifacts={"jsonl": {"filename": "old.jsonl"}},
        zero_history_entities=["sensor.old"],
        failed_batches=[{"batch_index": 1}],
        skipped_reason="synthetic previous skip",
        error="synthetic previous failure",
        error_code="synthetic_error",
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
    assert item.artifacts == {"jsonl": None, "csv": None, "parquet": None}
    assert item.zero_history_entities == []
    assert item.failed_batches == []
    assert item.skipped_reason is None
    assert item.error is None
    assert item.error_code is None
