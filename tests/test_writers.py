from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from ha_history_exporter import writers
from ha_history_exporter.validators import ValidationError
from tests.helpers import state_row


def test_jsonl_writer_streams_unicode_rows(tmp_path):
    path = tmp_path / "states.jsonl"
    row = state_row(attributes={"friendly_name": "Küche", "unit": "°C"})

    with writers.JsonlWriter(path) as writer:
        writer.write(row)
        writer.write({**row, "state": "22.0"})

    assert writer.row_count == 2
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["attributes"]["friendly_name"] == "Küche"


def test_csv_writer_quotes_complex_attributes_and_state(tmp_path):
    path = tmp_path / "states.csv"
    row = state_row(
        state="value,with,commas\nand newline",
        attributes={"description": 'quoted "value"', "nested": {"answer": 42}},
    )
    row["local_offset"] = "+02:00"

    with writers.CsvWriter(path) as writer:
        writer.write(row)

    with path.open("r", encoding="utf-8", newline="") as handle:
        records = list(csv.DictReader(handle))
    assert writer.row_count == 1
    assert records[0]["state"] == row["state"]
    assert json.loads(records[0]["attributes_json"]) == row["attributes"]
    assert records[0]["local_offset"] == "+02:00"


def test_flatten_payload_ignores_non_list_entries():
    first = state_row("sensor.first")
    second = state_row("sensor.second")
    payload = [[first], None, "invalid", [second]]

    assert writers.flatten_payload(payload) == [first, second]


def test_cleanup_tmp_removes_only_top_level_tmp_files(tmp_path):
    first = tmp_path / "first.tmp"
    keep = tmp_path / "keep.jsonl"
    nested = tmp_path / "nested"
    nested.mkdir()
    nested_tmp = nested / "nested.tmp"
    first.write_text("temporary", encoding="utf-8")
    keep.write_text("keep", encoding="utf-8")
    nested_tmp.write_text("nested", encoding="utf-8")

    assert writers.cleanup_tmp(tmp_path) == 1
    assert not first.exists()
    assert keep.exists()
    assert nested_tmp.exists()


def test_atomic_replace_creates_parent_and_replaces_destination(tmp_path):
    src = tmp_path / "source.tmp"
    dst = tmp_path / "nested" / "final.txt"
    src.write_text("new", encoding="utf-8")

    writers.atomic_replace(src, dst, 0, 0)

    assert not src.exists()
    assert dst.read_text(encoding="utf-8") == "new"


def test_atomic_replace_retries_permission_error(tmp_path, monkeypatch):
    src = tmp_path / "source.tmp"
    dst = tmp_path / "final.txt"
    src.write_text("new", encoding="utf-8")
    original = Path.replace
    attempts = []
    sleeps = []

    def flaky_replace(self, target):
        attempts.append((self, target))
        if len(attempts) == 1:
            raise PermissionError("synthetic lock")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    monkeypatch.setattr(writers.time, "sleep", sleeps.append)

    writers.atomic_replace(src, dst, 1, 0.25)

    assert len(attempts) == 2
    assert sleeps == [0.25]
    assert dst.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "2026-07-28T10:00:00.123456+02:00",
            datetime(2026, 7, 28, 8, 0, 0, 123456, tzinfo=timezone.utc),
        ),
        ("not-a-time", None),
        (None, None),
    ],
)
def test_parse_ts_utc(value, expected):
    assert writers._parse_ts_utc(value) == expected


def test_parquet_schema_is_stable():
    schema = writers._parquet_schema()

    assert schema.names == [
        "entity_id",
        "state",
        "last_changed",
        "last_updated",
        "attributes_json",
        "local_offset",
    ]
    assert not schema.field("entity_id").nullable
    assert str(schema.field("last_changed").type) == "timestamp[us, tz=UTC]"


def test_convert_jsonl_to_parquet_preserves_logical_values(tmp_path):
    src = tmp_path / "source.jsonl"
    dst = tmp_path / "output.parquet"
    rows = [
        {
            **state_row("sensor.one", "1"),
            "local_offset": "+02:00",
        },
        {
            **state_row(
                "sensor.two",
                "unknown",
                "2026-07-28T10:01:00+00:00",
                attributes={"friendly_name": "Zwei"},
            ),
            "local_offset": "+02:00",
        },
    ]
    src.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )

    count = writers.convert_jsonl_to_parquet(src, dst, row_group_size=1)
    table = pq.read_table(dst)
    records = table.to_pylist()

    assert count == 2
    assert table.num_rows == 2
    assert pq.read_metadata(dst).num_row_groups == 2
    assert records[0]["entity_id"] == "sensor.one"
    assert records[0]["state"] == "1"
    assert json.loads(records[1]["attributes_json"]) == {
        "friendly_name": "Zwei"
    }
    assert records[0]["last_changed"].tzinfo is not None


def test_convert_jsonl_to_parquet_turns_invalid_timestamp_into_null(tmp_path):
    src = tmp_path / "source.jsonl"
    dst = tmp_path / "output.parquet"
    row = {
        **state_row(timestamp="invalid"),
        "local_offset": None,
    }
    src.write_text(json.dumps(row) + "\n", encoding="utf-8")

    writers.convert_jsonl_to_parquet(src, dst)

    record = pq.read_table(dst).to_pylist()[0]
    assert record["last_changed"] is None
    assert record["last_updated"] is None


def test_convert_jsonl_to_parquet_requires_source(tmp_path):
    with pytest.raises(ValidationError, match="Source JSONL file does not exist"):
        writers.convert_jsonl_to_parquet(
            tmp_path / "missing.jsonl", tmp_path / "output.parquet"
        )
