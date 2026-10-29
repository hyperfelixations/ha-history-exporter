from __future__ import annotations

import re
from pathlib import Path

import pytest

from ha_history_exporter.errors import ValidationError
from ha_history_exporter.formats import parquet
from tests.support.records import make_record

pq = pytest.importorskip("pyarrow.parquet")
pa = pytest.importorskip("pyarrow")


def _read(path: Path) -> list[object]:
    with path.open("rb") as stream:
        return list(parquet.read_parquet(stream, path.name))


def test_records_round_trip_with_typed_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "day.parquet"
    first = make_record(extra=(("context", {"id": "c"}),))
    second = make_record("sensor.two", attributes=None)
    with parquet.ParquetWriter(path) as writer:
        writer.write(first)
        writer.write(second)

    table = pq.read_table(path)
    assert table.schema.equals(parquet.schema())
    assert table.column("extra_json").to_pylist() == ['{"context": {"id": "c"}}', None]
    assert table.column("attributes_json").to_pylist()[1] == "null"
    assert _read(path) == [first, second]


def test_a_day_without_records_is_a_valid_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "day.parquet"
    with parquet.ParquetWriter(path):
        pass
    assert _read(path) == []
    assert pq.ParquetFile(path).metadata.num_rows == 0


def test_row_groups_have_the_contracted_size_and_codec(tmp_path: Path) -> None:
    path = tmp_path / "day.parquet"
    record = make_record(attributes={})
    with parquet.ParquetWriter(path) as writer:
        for _ in range(parquet.ROW_GROUP_SIZE + 1):
            writer.write(record)

    metadata = pq.ParquetFile(path).metadata
    assert metadata.num_row_groups == 2
    assert metadata.row_group(0).num_rows == 200_000
    assert metadata.row_group(1).num_rows == 1
    assert metadata.row_group(0).column(0).compression.lower() == "snappy"


def test_a_corrupt_file_is_reported_by_name(tmp_path: Path) -> None:
    path = tmp_path / "day.parquet"
    path.write_bytes(b"not parquet")
    with pytest.raises(
        ValidationError, match=re.escape("day.parquet: cannot read the Parquet metadata")
    ):
        _read(path)


def test_a_foreign_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "day.parquet"
    pq.write_table(pa.table({"entity_id": ["sensor.one"]}), path)
    with pytest.raises(ValidationError, match="schema does not match"):
        _read(path)


def test_invalid_attribute_json_names_the_row(tmp_path: Path) -> None:
    path = tmp_path / "day.parquet"
    record = make_record()
    columns = {
        "entity_id": [record.entity_id],
        "state": [record.state],
        "last_changed": [record.last_changed],
        "last_updated": [record.last_updated],
        "attributes_json": ["{broken"],
        "local_offset": [record.local_offset],
        "extra_json": [None],
    }
    pq.write_table(pa.table(columns, schema=parquet.schema()), path)
    with pytest.raises(ValidationError, match=re.escape("day.parquet, row 1: attributes_json")):
        _read(path)


def test_writer_refuses_writes_after_close(tmp_path: Path) -> None:
    writer = parquet.ParquetWriter(tmp_path / "day.parquet")
    writer.close()
    writer.close()
    with pytest.raises(ValueError, match="closed"):
        writer.write(make_record())


def test_extra_json_must_hold_an_object(tmp_path: Path) -> None:
    path = tmp_path / "day.parquet"
    record = make_record()
    columns = {
        "entity_id": [record.entity_id],
        "state": [record.state],
        "last_changed": [record.last_changed],
        "last_updated": [record.last_updated],
        "attributes_json": [record.attributes_json],
        "local_offset": [record.local_offset],
        "extra_json": ["[1]"],
    }
    pq.write_table(pa.table(columns, schema=parquet.schema()), path)
    with pytest.raises(ValidationError, match="extra_json must contain an object"):
        _read(path)


def test_damaged_row_data_is_reported_by_name(tmp_path: Path) -> None:
    path = tmp_path / "day.parquet"
    with parquet.ParquetWriter(path) as writer:
        for index in range(50):
            writer.write(make_record(f"sensor.n{index}", attributes={"value": "x" * 40}))
    data = bytearray(path.read_bytes())
    data[4:200] = b"\xff" * 196
    path.write_bytes(bytes(data))
    with pytest.raises(
        ValidationError, match=re.escape("day.parquet: cannot read the Parquet rows")
    ):
        _read(path)
