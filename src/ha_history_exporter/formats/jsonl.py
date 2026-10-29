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

from ..domain.records import HistoryRecord, JsonValue, encode_json
from ..domain.stored import stored_record
from ..errors import ValidationError
from ..fsio import ensure_dir


def jsonl_line(record: HistoryRecord) -> str:
    """The record's line: its ordered mapping, encoded once.

    The attributes encode exactly as :attr:`HistoryRecord.attributes_json`,
    because both use :func:`encode_json`'s settings.
    """
    row: dict[str, JsonValue] = {
        "entity_id": record.entity_id,
        "state": record.state,
        "last_changed": record.last_changed_text,
        "last_updated": record.last_updated_text,
        "attributes": record.attributes,  # type: ignore[dict-item]
        **dict(record.extra),
        "local_offset": record.local_offset,
    }
    return encode_json(row) + "\n"


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        ensure_dir(path.parent)
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
