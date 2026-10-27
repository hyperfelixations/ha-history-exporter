"""Parquet: typed UTC timestamps, JSON text for attributes and unknown fields.

Records are streamed into row groups of :data:`ROW_GROUP_SIZE` rows with
:data:`COMPRESSION`; both are part of the output contract. ``pyarrow`` is an
optional dependency and is imported only when Parquet is read or written.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, BinaryIO, Self

from ..domain.records import HistoryRecord, JsonValue
from ..domain.stored import stored_record
from ..errors import ValidationError
from ..fsio import ensure_dir, fsync_file

if TYPE_CHECKING:  # pragma: no cover
    import pyarrow as pa

ROW_GROUP_SIZE = 200_000
COMPRESSION = "snappy"

_COLUMNS = (
    "entity_id",
    "state",
    "last_changed",
    "last_updated",
    "attributes_json",
    "local_offset",
    "extra_json",
)


@functools.cache
def schema() -> pa.Schema:
    import pyarrow as pa

    timestamp = pa.timestamp("us", tz="UTC")
    return pa.schema(
        [
            pa.field("entity_id", pa.string(), nullable=False),
            pa.field("state", pa.string(), nullable=False),
            pa.field("last_changed", timestamp, nullable=False),
            pa.field("last_updated", timestamp, nullable=False),
            pa.field("attributes_json", pa.string(), nullable=True),
            pa.field("local_offset", pa.string(), nullable=False),
            pa.field("extra_json", pa.string(), nullable=True),
        ]
    )


class ParquetWriter:
    def __init__(self, path: Path) -> None:
        import pyarrow.parquet as pq

        ensure_dir(path.parent)
        self._path = path
        self._writer: Any = pq.ParquetWriter(str(path), schema(), compression=COMPRESSION)
        self._columns: dict[str, list[Any]] = {name: [] for name in _COLUMNS}

    def write(self, record: HistoryRecord) -> None:
        if self._writer is None:
            raise ValueError("The Parquet writer is closed.")
        columns = self._columns
        columns["entity_id"].append(record.entity_id)
        columns["state"].append(record.state)
        columns["last_changed"].append(record.last_changed)
        columns["last_updated"].append(record.last_updated)
        columns["attributes_json"].append(record.attributes_json)
        columns["local_offset"].append(record.local_offset)
        columns["extra_json"].append(record.extra_json)
        if len(columns["entity_id"]) == ROW_GROUP_SIZE:
            self._flush()

    def _flush(self) -> None:
        import pyarrow as pa

        if not self._columns["entity_id"]:
            return
        table = pa.table(self._columns, schema=schema())
        self._writer.write_table(table)
        self._columns = {name: [] for name in _COLUMNS}

    def close(self) -> None:
        if self._writer is None:
            return
        try:
            self._flush()
        finally:
            writer, self._writer = self._writer, None
            writer.close()
        fsync_file(self._path)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _json_object(value: str | None, column: str, location: str) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{location}: {column} is not valid JSON.") from exc
    if decoded is not None and not isinstance(decoded, dict):
        raise ValidationError(f"{location}: {column} must contain an object or null.")
    return decoded


def read_parquet(stream: BinaryIO, name: str) -> Iterator[HistoryRecord]:
    import pyarrow.parquet as pq

    try:
        parquet = pq.ParquetFile(stream)
    except Exception as exc:
        raise ValidationError(f"{name}: cannot read the Parquet metadata.") from exc
    if not parquet.schema_arrow.equals(schema(), check_metadata=False):
        raise ValidationError(f"{name}: the Parquet schema does not match the HHE schema.")

    number = 0
    batches = parquet.iter_batches()
    while True:
        try:
            batch = next(batches)
        except StopIteration:
            return
        except Exception as exc:
            raise ValidationError(f"{name}: cannot read the Parquet rows.") from exc
        for row in batch.to_pylist():
            number += 1
            location = f"{name}, row {number}"
            yield stored_record(
                {
                    "entity_id": row["entity_id"],
                    "state": row["state"],
                    "last_changed": row["last_changed"].isoformat(),
                    "last_updated": row["last_updated"].isoformat(),
                    "attributes": _json_object(row["attributes_json"], "attributes_json", location),
                    "local_offset": row["local_offset"],
                },
                location=location,
                extra=_json_object(row["extra_json"], "extra_json", location) or {},
            )
