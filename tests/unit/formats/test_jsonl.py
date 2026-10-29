from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from hypothesis import given

from ha_history_exporter.domain.records import HistoryRecord
from ha_history_exporter.errors import ValidationError
from ha_history_exporter.formats.jsonl import JsonlWriter, jsonl_line, read_jsonl
from tests.support.records import make_record, records


def _ordered_mapping(record: HistoryRecord) -> dict[str, object]:
    return {
        "entity_id": record.entity_id,
        "state": record.state,
        "last_changed": record.last_changed.isoformat(),
        "last_updated": record.last_updated.isoformat(),
        "attributes": record.attributes,
        **dict(record.extra),
        "local_offset": record.local_offset,
    }


@given(records())
def test_a_line_equals_json_dumps_of_the_ordered_mapping(record: HistoryRecord) -> None:
    assert jsonl_line(record) == json.dumps(_ordered_mapping(record), ensure_ascii=False) + "\n"


def test_writer_streams_unicode_rows_with_lf(tmp_path: Path) -> None:
    path = tmp_path / "day.jsonl"
    with JsonlWriter(path) as writer:
        writer.write(make_record(attributes={"friendly_name": "Küche 🌡"}))
        writer.write(make_record("sensor.two", extra=(("context", {"id": "c"}),)))

    raw = path.read_bytes()
    assert b"\r" not in raw
    lines = raw.decode("utf-8").splitlines()
    assert json.loads(lines[0])["attributes"]["friendly_name"] == "Küche 🌡"
    assert list(json.loads(lines[1])) == [
        "entity_id",
        "state",
        "last_changed",
        "last_updated",
        "attributes",
        "context",
        "local_offset",
    ]


def test_writer_refuses_writes_after_close(tmp_path: Path) -> None:
    writer = JsonlWriter(tmp_path / "day.jsonl")
    writer.close()
    writer.close()
    with pytest.raises(ValueError, match="closed"):
        writer.write(make_record())


def _read(data: bytes) -> list[HistoryRecord]:
    return list(read_jsonl(io.BytesIO(data), "day.jsonl"))


def test_reader_skips_blank_lines_and_accepts_crlf_archives() -> None:
    line = jsonl_line(make_record()).encode("utf-8")
    assert len(_read(line + b"\n" + line)) == 2
    assert len(_read(line.replace(b"\n", b"\r\n") * 2)) == 2
    assert _read(b"") == []


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"{broken\n", "day.jsonl, line 1: invalid JSON"),
        (b"[1, 2]\n", "day.jsonl, line 1: the row must be an object"),
    ],
)
def test_reader_names_the_offending_line(data: bytes, message: str) -> None:
    valid = jsonl_line(make_record()).encode("utf-8")
    with pytest.raises(ValidationError, match=message.replace("line 1", "line 2")):
        _read(valid + data)
