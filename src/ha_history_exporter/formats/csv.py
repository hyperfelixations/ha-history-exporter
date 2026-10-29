"""CSV: RFC 4180 with CRLF line endings and a fixed header.

Attributes and unknown wire fields are JSON text in ``attributes_json`` and
``extra_json``; ``extra_json`` is empty when a record has none.
"""

from __future__ import annotations

import csv
import io
import os
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

from ..domain.records import HistoryRecord
from ..domain.stored import stored_record
from ..errors import ValidationError
from ..fsio import ensure_dir
from .base import TABLE_COLUMNS, json_object


class CsvWriter:
    def __init__(self, path: Path) -> None:
        ensure_dir(path.parent)
        self._handle: io.TextIOWrapper | None = path.open("w", encoding="utf-8", newline="")
        self._writer = csv.writer(self._handle)
        self._writer.writerow(TABLE_COLUMNS)

    def write(self, record: HistoryRecord) -> None:
        if self._handle is None:
            raise ValueError("The CSV writer is closed.")
        self._writer.writerow(
            (
                record.entity_id,
                record.state,
                record.last_changed_text,
                record.last_updated_text,
                record.attributes_json,
                record.local_offset,
                record.extra_json or "",
            )
        )

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


def read_csv(stream: BinaryIO, name: str) -> Iterator[HistoryRecord]:
    text = io.TextIOWrapper(stream, encoding="utf-8", newline="")
    try:
        reader = csv.reader(text)
        header = next(reader, None)
        if header is None:
            raise ValidationError(f"{name}: the CSV file is empty.")
        if tuple(header) != TABLE_COLUMNS:
            raise ValidationError(f"{name}: the CSV header does not match the HHE schema.")
        for row in reader:
            location = f"{name}, line {reader.line_num}"
            if len(row) != len(TABLE_COLUMNS):
                raise ValidationError(
                    f"{location}: the row has {len(row)} values; "
                    f"the schema has {len(TABLE_COLUMNS)}."
                )
            values = dict(zip(TABLE_COLUMNS, row, strict=True))
            yield stored_record(
                {
                    "entity_id": values["entity_id"],
                    "state": values["state"],
                    "last_changed": values["last_changed"],
                    "last_updated": values["last_updated"],
                    "attributes": json_object(
                        values["attributes_json"] or None, "attributes_json", location
                    ),
                    "local_offset": values["local_offset"],
                },
                location=location,
                extra=json_object(values["extra_json"] or None, "extra_json", location) or {},
            )
    finally:
        text.detach()
