from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from ha_history_exporter.errors import ValidationError
from ha_history_exporter.formats.base import TABLE_COLUMNS as COLUMNS
from ha_history_exporter.formats.csv import CsvWriter, read_csv
from tests.support.records import make_record

VALID = [
    "sensor.one",
    "1",
    "2026-07-28T10:00:00+00:00",
    "2026-07-28T10:00:00+00:00",
    "{}",
    "+02:00",
    "",
]


def _csv(*rows: list[str], header: tuple[str, ...] = COLUMNS) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _read(data: bytes) -> list[object]:
    return list(read_csv(io.BytesIO(data), "day.csv"))


def test_writer_quotes_complex_states_and_attributes_with_crlf(tmp_path: Path) -> None:
    path = tmp_path / "day.csv"
    record = make_record(
        state='Alarm, "high"',
        attributes={"description": "line one\nline two, comma"},
        extra=(("context", {"id": "c"}),),
    )
    with CsvWriter(path) as writer:
        writer.write(record)

    raw = path.read_bytes()
    assert raw.startswith(",".join(COLUMNS).encode() + b"\r\n")
    [row] = list(csv.DictReader(io.StringIO(raw.decode("utf-8"), newline="")))
    assert row["state"] == 'Alarm, "high"'
    assert row["attributes_json"] == record.attributes_json
    assert row["extra_json"] == '{"context": {"id": "c"}}'
    assert _read(raw) == [record]


def test_records_without_extras_leave_extra_json_empty(tmp_path: Path) -> None:
    path = tmp_path / "day.csv"
    with CsvWriter(path) as writer:
        writer.write(make_record())
    assert path.read_bytes().endswith(b",+02:00,\r\n")


def test_reader_counts_data_rows_and_accepts_null_attributes() -> None:
    null_attributes = [*VALID[:4], "null", "+02:00", ""]
    records = _read(_csv(VALID, ["sensor.two", *VALID[1:]], null_attributes))
    assert len(records) == 3


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "day.csv: the CSV file is empty"),
        (_csv(header=("entity_id", "state")), "header does not match"),
        (_csv([*VALID, "unexpected"]), "day.csv, line 2: the row has 8 values; the schema has 7"),
        (_csv(VALID[:-1]), "the row has 6 values"),
        (_csv([*VALID[:4], "{broken", "+02:00", ""]), "line 2: attributes_json is not valid JSON"),
        (_csv([*VALID[:4], "[1]", "+02:00", ""]), "attributes_json must contain an object"),
        (_csv([*VALID[:6], "[1]"]), "extra_json must contain an object"),
    ],
)
def test_reader_rejects_rows_outside_the_schema(data: bytes, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _read(data)


def test_writer_refuses_writes_after_close(tmp_path: Path) -> None:
    writer = CsvWriter(tmp_path / "day.csv")
    writer.close()
    writer.close()
    with pytest.raises(ValueError, match="closed"):
        writer.write(make_record())
