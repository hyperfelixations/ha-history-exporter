from __future__ import annotations

import csv
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ha_history_exporter.validators import (
    ValidationError,
    validate_csv,
    validate_jsonl,
    validate_parquet,
)
from ha_history_exporter.writers import _parquet_schema


def test_validate_jsonl_accepts_valid_rows_and_blank_lines(tmp_path):
    path = tmp_path / "valid.jsonl"
    path.write_text('{"a": 1}\n\n{"a": 2}\n', encoding="utf-8")

    assert validate_jsonl(path, 2) == 2


def test_validate_jsonl_accepts_empty_export_when_zero_expected(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert validate_jsonl(path, 0) == 0


def test_validate_jsonl_rejects_missing_invalid_and_wrong_count(tmp_path):
    with pytest.raises(ValidationError, match="does not exist"):
        validate_jsonl(tmp_path / "missing.jsonl", 0)

    invalid = tmp_path / "invalid.jsonl"
    invalid.write_text('{"valid": true}\n{broken\n', encoding="utf-8")
    with pytest.raises(ValidationError, match="Invalid JSON on line 2"):
        validate_jsonl(invalid, 2)

    valid = tmp_path / "count.jsonl"
    valid.write_text('{"valid": true}\n', encoding="utf-8")
    with pytest.raises(ValidationError, match="expected 2, found 1"):
        validate_jsonl(valid, 2)


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["entity_id", "state"])
        writer.writerows(rows)


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


def make_valid_parquet(path, rows=2):
    schema = _parquet_schema()
    values = {
        "entity_id": [f"sensor.{index}" for index in range(rows)],
        "state": [str(index) for index in range(rows)],
        "last_changed": [None] * rows,
        "last_updated": [None] * rows,
        "attributes_json": [json.dumps({})] * rows,
        "local_offset": ["+02:00"] * rows,
    }
    pq.write_table(pa.table(values, schema=schema), path)


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
    with pytest.raises(ValidationError, match="missing required columns"):
        validate_parquet(missing_column, 1)
