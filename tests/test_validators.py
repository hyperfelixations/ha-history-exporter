from __future__ import annotations

import csv
import json
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ha_history_exporter.readers import CSV_FIELDS
from ha_history_exporter.settings import Format, HistoryRequestSettings
from ha_history_exporter.validators import (
    ValidationError,
    validate_csv,
    validate_export_artifacts,
    validate_jsonl,
    validate_parquet,
)
from ha_history_exporter.writers import _parquet_schema
from tests.helpers import state_row


def test_validate_jsonl_accepts_valid_rows_and_blank_lines(tmp_path):
    path = tmp_path / "valid.jsonl"
    first = {**state_row("sensor.one"), "local_offset": "+02:00"}
    second = {**state_row("sensor.two"), "local_offset": "+02:00"}
    path.write_text(
        json.dumps(first) + "\n\n" + json.dumps(second) + "\n",
        encoding="utf-8",
    )

    assert validate_jsonl(path, 2) == 2


def test_validate_jsonl_accepts_empty_export_when_zero_expected(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert validate_jsonl(path, 0) == 0


def test_validate_jsonl_rejects_missing_invalid_and_wrong_count(tmp_path):
    with pytest.raises(ValidationError, match="does not exist"):
        validate_jsonl(tmp_path / "missing.jsonl", 0)

    invalid = tmp_path / "invalid.jsonl"
    invalid.write_text(
        json.dumps({**state_row(), "local_offset": "+02:00"}) + "\n{broken\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="Invalid JSON on line 2"):
        validate_jsonl(invalid, 2)

    valid = tmp_path / "count.jsonl"
    valid.write_text(
        json.dumps({**state_row(), "local_offset": "+02:00"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="expected 2, found 1"):
        validate_jsonl(valid, 2)


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_FIELDS)
        for entity_id, state in rows:
            writer.writerow(
                [
                    entity_id,
                    state,
                    "2026-07-28T10:00:00+00:00",
                    "2026-07-28T10:00:00+00:00",
                    "{}",
                    "+02:00",
                    "",
                ]
            )


def test_validate_csv_counts_data_rows(tmp_path):
    path = tmp_path / "valid.csv"
    write_csv(path, [["sensor.one", "1"], ["sensor.two", "2"]])
    assert validate_csv(path, 2) == 2


def test_validate_csv_rejects_missing_empty_and_wrong_count(tmp_path):
    with pytest.raises(ValidationError, match="does not exist"):
        validate_csv(tmp_path / "missing.csv", 0)

    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValidationError, match="no header"):
        validate_csv(empty, 0)

    path = tmp_path / "count.csv"
    write_csv(path, [["sensor.one", "1"]])
    with pytest.raises(ValidationError, match="expected 2, found 1"):
        validate_csv(path, 2)


def test_validate_csv_rejects_extra_values_beyond_the_fixed_schema(tmp_path):
    path = tmp_path / "extra-value.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_FIELDS)
        writer.writerow(
            [
                "sensor.one",
                "1",
                "2026-07-28T10:00:00+00:00",
                "2026-07-28T10:00:00+00:00",
                "{}",
                "+02:00",
                "",
                "unexpected",
            ]
        )

    with pytest.raises(ValidationError, match="extra value"):
        validate_csv(path, 1)


def make_valid_parquet(path, rows=2):
    schema = _parquet_schema()
    values = {
        "entity_id": [f"sensor.{index}" for index in range(rows)],
        "state": [str(index) for index in range(rows)],
        "last_changed": [datetime(2026, 7, 28, 10, tzinfo=timezone.utc)] * rows,
        "last_updated": [datetime(2026, 7, 28, 10, tzinfo=timezone.utc)] * rows,
        "attributes_json": [json.dumps({})] * rows,
        "local_offset": ["+02:00"] * rows,
        "extra_json": [None] * rows,
    }
    pq.write_table(pa.table(values, schema=schema), path)


def test_cross_format_validation_requires_identical_logical_records(tmp_path):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from ha_history_exporter.writers import CsvWriter, JsonlWriter, convert_jsonl_to_parquet

    row = {
        **state_row("sensor.one"),
        "local_offset": "+02:00",
        "context": {"id": "synthetic-context"},
    }
    jsonl = tmp_path / "day.jsonl"
    csv_path = tmp_path / "day.csv"
    parquet = tmp_path / "day.parquet"
    with JsonlWriter(jsonl) as writer:
        writer.write(row)
    with CsvWriter(csv_path) as writer:
        writer.write(row)
    convert_jsonl_to_parquet(jsonl, parquet)

    artifacts = validate_export_artifacts(
        {Format.JSONL: jsonl, Format.CSV: csv_path, Format.PARQUET: parquet},
        expected_rows=1,
        requested_entity_ids=["sensor.one"],
        start=datetime(2026, 7, 28, tzinfo=timezone.utc),
        end=datetime(2026, 7, 29, tzinfo=timezone.utc),
        settings=HistoryRequestSettings(),
        local_timezone=ZoneInfo("Europe/Berlin"),
    )

    assert len({artifact.logical_sha256 for artifact in artifacts.values()}) == 1
    assert all(artifact.sha256 for artifact in artifacts.values())

    rows = list(csv.DictReader(csv_path.open("r", encoding="utf-8", newline="")))
    rows[0]["state"] = "different"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(ValidationError, match="logical records differ"):
        validate_export_artifacts(
            {Format.JSONL: jsonl, Format.CSV: csv_path},
            expected_rows=1,
            requested_entity_ids=["sensor.one"],
            start=datetime(2026, 7, 28, tzinfo=timezone.utc),
            end=datetime(2026, 7, 29, tzinfo=timezone.utc),
            settings=HistoryRequestSettings(),
            local_timezone=ZoneInfo("Europe/Berlin"),
        )


def test_validate_parquet_checks_rows_and_schema(tmp_path):
    path = tmp_path / "valid.parquet"
    make_valid_parquet(path, 2)
    assert validate_parquet(path, 2) == 2


def test_validate_parquet_rejects_missing_corrupt_count_and_schema(tmp_path):
    with pytest.raises(ValidationError, match="does not exist"):
        validate_parquet(tmp_path / "missing.parquet", 0)

    corrupt = tmp_path / "corrupt.parquet"
    corrupt.write_bytes(b"not parquet")
    with pytest.raises(ValidationError, match="Cannot read Parquet metadata"):
        validate_parquet(corrupt, 0)

    count = tmp_path / "count.parquet"
    make_valid_parquet(count, 1)
    with pytest.raises(ValidationError, match="expected 2, found 1"):
        validate_parquet(count, 2)

    missing_column = tmp_path / "missing-column.parquet"
    pq.write_table(pa.table({"entity_id": ["sensor.one"]}), missing_column)
    with pytest.raises(ValidationError, match="schema does not match"):
        validate_parquet(missing_column, 1)
