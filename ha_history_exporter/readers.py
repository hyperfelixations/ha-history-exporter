"""Streaming adapters from every public format to ``HistoryRecord``."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator, Mapping
from pathlib import Path

from .errors import ValidationError
from .history_records import HistoryRecord, stored_record
from .settings.model import Format

CSV_FIELDS = (
    "entity_id",
    "state",
    "last_changed",
    "last_updated",
    "attributes_json",
    "local_offset",
    "extra_json",
)


def _json_mapping(value: str | None, field: str, location: str) -> dict | None:
    if value in (None, ""):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{location}: {field} is not valid JSON.") from exc
    if decoded is not None and not isinstance(decoded, dict):
        raise ValidationError(f"{location}: {field} must contain an object or null.")
    return decoded


def read_jsonl(path: Path) -> Iterator[HistoryRecord]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            location = f"{path.name}, line {line_number}"
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValidationError(
                    f"Invalid JSON on line {line_number} of {path.name}."
                ) from exc
            if not isinstance(row, Mapping):
                raise ValidationError(f"{location}: the row must be an object.")
            yield stored_record(row, location=location)


def read_csv(path: Path) -> Iterator[HistoryRecord]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValidationError(f"CSV file is empty (no header): {path.name}")
        if tuple(reader.fieldnames) != CSV_FIELDS:
            raise ValidationError(f"CSV header does not match the HHE schema in {path.name}.")
        for line_number, row in enumerate(reader, start=2):
            location = f"{path.name}, line {line_number}"
            if None in row:
                raise ValidationError(
                    f"{location}: row contains an extra value beyond the HHE schema."
                )
            attributes = _json_mapping(row["attributes_json"], "attributes_json", location)
            extra = _json_mapping(row["extra_json"], "extra_json", location) or {}
            yield stored_record(
                {
                    "entity_id": row["entity_id"],
                    "state": row["state"],
                    "last_changed": row["last_changed"],
                    "last_updated": row["last_updated"],
                    "attributes": attributes,
                    "local_offset": row["local_offset"],
                },
                location=location,
                extra=extra,
            )


def read_parquet(path: Path) -> Iterator[HistoryRecord]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - guarded by export preflight
        raise ImportError("pyarrow is required for Parquet input.") from exc
    try:
        parquet = pq.ParquetFile(path)
    except Exception as exc:
        raise ValidationError(f"Cannot read Parquet metadata from {path.name}.") from exc
    import pyarrow as pa

    expected_schema = pa.schema(
        [
            pa.field("entity_id", pa.string(), nullable=False),
            pa.field("state", pa.string(), nullable=False),
            pa.field("last_changed", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("last_updated", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("attributes_json", pa.string(), nullable=True),
            pa.field("local_offset", pa.string(), nullable=False),
            pa.field("extra_json", pa.string(), nullable=True),
        ]
    )
    if not parquet.schema_arrow.equals(expected_schema, check_metadata=False):
        raise ValidationError(f"Parquet schema does not match the HHE schema in {path.name}.")
    row_number = 0
    try:
        for batch in parquet.iter_batches():
            for row in batch.to_pylist():
                row_number += 1
                location = f"{path.name}, row {row_number}"
                attributes = _json_mapping(
                    row["attributes_json"], "attributes_json", location
                )
                extra = _json_mapping(row["extra_json"], "extra_json", location) or {}
                yield stored_record(
                    {
                        "entity_id": row["entity_id"],
                        "state": row["state"],
                        "last_changed": row["last_changed"].isoformat()
                        if row["last_changed"] is not None
                        else None,
                        "last_updated": row["last_updated"].isoformat()
                        if row["last_updated"] is not None
                        else None,
                        "attributes": attributes,
                        "local_offset": row["local_offset"],
                    },
                    location=location,
                    extra=extra,
                )
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(f"Cannot read Parquet rows from {path.name}.") from exc


def read_records(path: Path, fmt: Format) -> Iterator[HistoryRecord]:
    readers = {
        Format.JSONL: read_jsonl,
        Format.CSV: read_csv,
        Format.PARQUET: read_parquet,
    }
    yield from readers[fmt](path)
