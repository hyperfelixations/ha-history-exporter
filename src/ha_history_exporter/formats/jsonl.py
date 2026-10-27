"""JSONL: one JSON object per record and line, LF-terminated.

Field order: ``entity_id, state, last_changed, last_updated, attributes``,
then unknown wire fields in their original order, then ``local_offset``.
"""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

from ..domain.records import HistoryRecord, encode_json
from ..domain.stored import stored_record
from ..errors import ValidationError


def jsonl_line(record: HistoryRecord) -> str:
    """The record's line, identical to ``json.dumps`` of its ordered mapping."""
    parts = [
        f'"entity_id": {encode_json(record.entity_id)}',
        f'"state": {encode_json(record.state)}',
        f'"last_changed": {encode_json(record.last_changed.isoformat())}',
        f'"last_updated": {encode_json(record.last_updated.isoformat())}',
        f'"attributes": {record.attributes_json}',
    ]
    parts.extend(f"{encode_json(key)}: {encode_json(value)}" for key, value in record.extra)
    parts.append(f'"local_offset": {encode_json(record.local_offset)}')
    return "{" + ", ".join(parts) + "}\n"


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle: io.TextIOWrapper | None = path.open("w", encoding="utf-8", newline="\n")

    def write(self, record: HistoryRecord) -> None:
        if self._handle is None:
            raise ValueError("The JSONL writer is closed.")
        self._handle.write(jsonl_line(record))

    def close(self) -> None:
        if self._handle is None:
            return
        handle, self._handle = self._handle, None
        try:
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def read_jsonl(stream: BinaryIO, name: str) -> Iterator[HistoryRecord]:
    text = io.TextIOWrapper(stream, encoding="utf-8", newline="")
    try:
        for number, line in enumerate(text, start=1):
            if not line.strip():
                continue
            location = f"{name}, line {number}"
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"{location}: invalid JSON.") from exc
            if not isinstance(row, dict):
                raise ValidationError(f"{location}: the row must be an object.")
            yield stored_record(row, location=location)
    finally:
        text.detach()
