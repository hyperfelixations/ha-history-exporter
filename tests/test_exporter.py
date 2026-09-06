from __future__ import annotations

import csv
import json
from datetime import date
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
import pytest

from ha_history_exporter import exporter
from ha_history_exporter.exceptions import AuthError
from ha_history_exporter.planner import ExportPlan
from tests.helpers import (
    FakeHomeAssistantClient,
    make_config,
    make_export_plan,
    state_row,
)


BERLIN = ZoneInfo("Europe/Berlin")
DAY = date(2026, 7, 28)


def test_run_export_writes_and_validates_all_formats(tmp_path):
    cfg = make_config(tmp_path, jsonl=True, csv=True, parquet=True, batch_size=2)
    plan = make_export_plan(DAY)
    first = state_row("sensor.one", "1")
    second = state_row(
        "sensor.two",
        "unknown",
        "2026-07-28T11:00:00+00:00",
        attributes={"friendly_name": "Synthetic two"},
    )
    client = FakeHomeAssistantClient([[[first]], [[second]]])

    result = exporter.run_export(
        cfg,
        plan,
        ["sensor.one", "sensor.two", "sensor.no_history"],
        4,
        client,
        BERLIN,
    )

    assert result == 0
    assert len(client.calls) == 2
    assert client.calls[0]["entity_ids"] == ["sensor.one", "sensor.two"]
    assert client.calls[1]["entity_ids"] == ["sensor.no_history"]

    jsonl_path = cfg.day_file(DAY, "jsonl")
    csv_path = cfg.day_file(DAY, "csv")
    parquet_path = cfg.day_file(DAY, "parquet")
    manifest_path = cfg.day_file(DAY, "manifest.json")
    assert jsonl_path.exists()
    assert csv_path.exists()
    assert parquet_path.exists()
    assert manifest_path.exists()

    jsonl_rows = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["entity_id"] for row in jsonl_rows] == [
        "sensor.one",
        "sensor.two",
    ]
    assert all(row["local_offset"] == "+02:00" for row in jsonl_rows)

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    assert len(csv_rows) == 2

    table = pq.read_table(parquet_path)
    assert table.num_rows == 2
    assert table.column_names == [
        "entity_id",
        "state",
        "last_changed",
        "last_updated",
        "attributes_json",
        "local_offset",
    ]

    day_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert day_manifest["status"] == "ok"
    assert day_manifest["entity_count_current"] == 4
    assert day_manifest["entity_count_requested"] == 3
    assert day_manifest["entity_count_with_history"] == 2
    assert day_manifest["entity_count_zero_history"] == 1
    assert day_manifest["zero_history_entities"] == ["sensor.no_history"]
    assert day_manifest["state_object_count"] == 2
    assert day_manifest["request_count"] == 2
    assert day_manifest["output_files"] == {
        "jsonl": f"{DAY}.jsonl",
        "csv": f"{DAY}.csv",
        "parquet": f"{DAY}.parquet",
    }

    run_log = cfg.metadata_dir / "export_runs.jsonl"
    entries = [
        json.loads(line)
        for line in run_log.read_text(encoding="utf-8").splitlines()
    ]
    assert entries[-1]["day"] == str(DAY)
    assert entries[-1]["status"] == "ok"


def test_run_export_handles_empty_history(tmp_path):
    cfg = make_config(tmp_path, jsonl=True, csv=True, parquet=True, batch_size=2)
    client = FakeHomeAssistantClient([[[], []]])

    result = exporter.run_export(
        cfg,
        make_export_plan(DAY),
        ["sensor.one", "sensor.two"],
        2,
        client,
        BERLIN,
    )

    assert result == 0
    assert cfg.day_file(DAY, "jsonl").read_text(encoding="utf-8") == ""
    with cfg.day_file(DAY, "csv").open("r", encoding="utf-8", newline="") as handle:
        assert len(list(csv.reader(handle))) == 1
    assert pq.read_metadata(cfg.day_file(DAY, "parquet")).num_rows == 0
    day_manifest = json.loads(
        cfg.day_file(DAY, "manifest.json").read_text(encoding="utf-8")
    )
    assert day_manifest["state_object_count"] == 0
    assert day_manifest["zero_history_entities"] == [
        "sensor.one",
        "sensor.two",
    ]


def test_run_export_dry_run_never_calls_client_or_writes_files(tmp_path):
    cfg = make_config(tmp_path)
    client = FakeHomeAssistantClient([])

    result = exporter.run_export(
        cfg,
        make_export_plan(DAY),
        ["sensor.one"],
        1,
        client,
        BERLIN,
        dry_run=True,
    )

    assert result == 0
    assert client.calls == []
    assert not cfg.daily_export_root.exists()


def test_run_export_with_empty_plan_is_noop(tmp_path):
    cfg = make_config(tmp_path)
    client = FakeHomeAssistantClient([])
    plan = ExportPlan(
        requested_start=DAY,
        requested_end=DAY,
        today=date(2026, 7, 30),
        latest_complete=date(2026, 7, 29),
        decisions=[],
    )

    assert exporter.run_export(cfg, plan, [], 0, client, BERLIN) == 0
    assert client.calls == []


def test_run_export_snapshot_only_never_calls_history_or_writes_manifest(tmp_path):
    cfg = make_config(tmp_path, jsonl=False, csv=False, parquet=False)
    client = FakeHomeAssistantClient([])

    assert (
        exporter.run_export(
            cfg,
            make_export_plan(DAY),
            ["sensor.one"],
            1,
            client,
            BERLIN,
        )
        == 0
    )
    assert client.calls == []
    assert not cfg.day_file(DAY, "manifest.json").exists()


def test_failed_batch_marks_manifest_failed_and_keeps_final_files_absent(tmp_path):
    cfg = make_config(tmp_path, jsonl=True, csv=False, parquet=False, batch_size=1)
    client = FakeHomeAssistantClient(
        [RuntimeError("synthetic batch failure"), [[state_row("sensor.two")]]]
    )

    result = exporter.run_export(
        cfg,
        make_export_plan(DAY),
        ["sensor.one", "sensor.two"],
        2,
        client,
        BERLIN,
    )

    assert result == 1
    assert len(client.calls) == 2
    assert not cfg.day_file(DAY, "jsonl").exists()
    day_manifest = json.loads(
        cfg.day_file(DAY, "manifest.json").read_text(encoding="utf-8")
    )
    assert day_manifest["status"] == "failed"
    assert day_manifest["failed_request_count"] == 1
    assert day_manifest["failed_batches"][0]["entities"] == ["sensor.one"]
    assert "1 batch(es) failed" in day_manifest["error"]


def test_run_export_leaves_foreign_temp_files_alone(tmp_path):
    """Cleanup is run-isolated: a parallel run's files must survive."""
    cfg = make_config(tmp_path, parquet=False)
    cfg.resolved_temp_dir.mkdir(parents=True)
    foreign = cfg.resolved_temp_dir / "old.tmp"
    foreign.write_text("belongs to something else", encoding="utf-8")
    client = FakeHomeAssistantClient([[[state_row()]]])

    assert (
        exporter.run_export(
            cfg,
            make_export_plan(DAY),
            ["sensor.test_temperature"],
            1,
            client,
            BERLIN,
        )
        == 0
    )
    assert foreign.read_text(encoding="utf-8") == "belongs to something else"


def test_run_export_removes_its_own_working_directory(tmp_path):
    cfg = make_config(tmp_path, parquet=False)
    client = FakeHomeAssistantClient([[[state_row()]]])

    assert (
        exporter.run_export(
            cfg,
            make_export_plan(DAY),
            ["sensor.test_temperature"],
            1,
            client,
            BERLIN,
        )
        == 0
    )

    assert list(cfg.resolved_temp_dir.glob("run-*")) == []
    assert not (cfg.day_dir(DAY).parents[3] / ".hhe.lock").exists()


def test_chunks_preserve_order_and_handle_empty_list():
    assert list(exporter._chunks([1, 2, 3, 4, 5], 2)) == [
        [1, 2],
        [3, 4],
        [5],
    ]
    assert list(exporter._chunks([], 2)) == []


def test_auth_error_aborts_day_and_propagates_immediately(tmp_path):
    cfg = make_config(tmp_path, batch_size=1)
    client = FakeHomeAssistantClient(
        [AuthError("synthetic authentication failure"), [[state_row("sensor.two")]]]
    )

    with pytest.raises(AuthError):
        exporter.run_export(
            cfg,
            make_export_plan(DAY),
            ["sensor.one", "sensor.two"],
            2,
            client,
            BERLIN,
        )

    assert len(client.calls) == 1
    day_manifest = json.loads(
        cfg.day_file(DAY, "manifest.json").read_text(encoding="utf-8")
    )
    assert day_manifest["status"] == "failed"
    assert day_manifest["failed_request_count"] == 1
    assert "synthetic authentication failure" in day_manifest["error"]


@pytest.mark.parametrize(
    ("csv_enabled", "parquet_enabled"),
    [(True, False), (False, True)],
    ids=["csv-only", "parquet-only"],
)
def test_non_jsonl_exports_do_not_leave_final_jsonl(
    tmp_path, csv_enabled, parquet_enabled
):
    cfg = make_config(
        tmp_path,
        jsonl=False,
        csv=csv_enabled,
        parquet=parquet_enabled,
    )
    client = FakeHomeAssistantClient([[[state_row()]]])

    result = exporter.run_export(
        cfg,
        make_export_plan(DAY),
        ["sensor.test_temperature"],
        1,
        client,
        BERLIN,
    )

    assert result == 0
    if csv_enabled:
        assert cfg.day_file(DAY, "csv").exists()
    if parquet_enabled:
        assert cfg.day_file(DAY, "parquet").exists()
    assert not cfg.day_file(DAY, "jsonl").exists()
    assert not (cfg.resolved_temp_dir / f"{DAY}.jsonl.tmp").exists()

    day_manifest = json.loads(
        cfg.day_file(DAY, "manifest.json").read_text(encoding="utf-8")
    )
    assert day_manifest["status"] == "ok"
    assert day_manifest["output_files"]["jsonl"] is None


@pytest.mark.parametrize(
    ("jsonl_enabled", "csv_enabled", "parquet_enabled"),
    [
        (True, False, False),
        (False, True, False),
        (False, False, True),
        (True, True, False),
        (True, False, True),
        (False, True, True),
        (True, True, True),
    ],
)
def test_export_respects_every_valid_format_combination(
    tmp_path,
    jsonl_enabled,
    csv_enabled,
    parquet_enabled,
):
    cfg = make_config(
        tmp_path,
        jsonl=jsonl_enabled,
        csv=csv_enabled,
        parquet=parquet_enabled,
    )
    client = FakeHomeAssistantClient([[[state_row()]]])

    assert (
        exporter.run_export(
            cfg,
            make_export_plan(DAY),
            ["sensor.test_temperature"],
            1,
            client,
            BERLIN,
        )
        == 0
    )

    enabled = {
        "jsonl": jsonl_enabled,
        "csv": csv_enabled,
        "parquet": parquet_enabled,
    }
    for suffix, is_enabled in enabled.items():
        assert cfg.day_file(DAY, suffix).exists() is is_enabled

    day_manifest = json.loads(
        cfg.day_file(DAY, "manifest.json").read_text(encoding="utf-8")
    )
    assert day_manifest["output_files"] == {
        suffix: f"{DAY}.{suffix}" if is_enabled else None
        for suffix, is_enabled in enabled.items()
    }
