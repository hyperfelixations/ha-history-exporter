"""Streaming file writers for JSONL and CSV output, plus Parquet post-processing.

Write pattern for JSONL / CSV:
  1. Open a temporary file in temp_dir, outside the output directory.
  2. Stream state objects line by line as they arrive from each batch.
  3. After all batches: validate, then atomic-replace to the final path.
     os.replace() is used; on PermissionError (a file lock) we retry.

Parquet post-processing (convert_jsonl_to_parquet):
  Reads the already-validated JSONL temp file and converts it to Parquet.
  Done AFTER JSONL validation so the Parquet is always derived from good data.
  pyarrow is imported lazily — only when Parquet output is enabled.

JSONL is the primary format: one JSON object per line, no trailing comma,
no outer array.  This allows streaming reads with DuckDB, Polars, Pandas, etc.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar, TextIO

logger = logging.getLogger(__name__)

_CANONICAL_ROW_FIELDS = frozenset(
    {
        "entity_id",
        "state",
        "last_changed",
        "last_updated",
        "attributes",
        "local_offset",
    }
)


# ── JSONL streaming writer ────────────────────────────────────────────────────

class JsonlWriter:
    """Context manager that streams state objects to a JSONL temp file.

    Usage::

        with JsonlWriter(tmp_path) as w:
            for row in batch:
                w.write(row)
        # after __exit__, tmp_path contains the complete JSONL file.

    Call ``w.row_count`` for the total number of lines written.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._f: TextIO | None = None
        self._count = 0

    def __enter__(self) -> JsonlWriter:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self._path.open("w", encoding="utf-8")
        return self

    def write(self, row: dict) -> None:
        assert self._f is not None, "JsonlWriter must be used as a context manager"
        self._f.write(json.dumps(row, ensure_ascii=False, default=str))
        self._f.write("\n")
        self._count += 1

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._f:
            self._f.flush()
            self._f.close()
            self._f = None

    @property
    def row_count(self) -> int:
        return self._count


# ── CSV streaming writer ──────────────────────────────────────────────────────

class CsvWriter:
    """Context manager that streams state objects to a CSV temp file.

    Schema: entity_id, state, last_changed, last_updated, attributes_json

    Attributes are stored as a JSON string to avoid dynamic column explosion
    (attribute keys differ across entities and change over time).
    """

    _FIELDS: ClassVar[list[str]] = [
        "entity_id",
        "state",
        "last_changed",
        "last_updated",
        "attributes_json",
        "local_offset",
        "extra_json",
    ]

    def __init__(self, path: Path) -> None:
        self._path = path
        self._f: TextIO | None = None
        self._writer: csv.DictWriter[str] | None = None
        self._count = 0

    def __enter__(self) -> CsvWriter:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self._path.open("w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._f, fieldnames=self._FIELDS, extrasaction="ignore")
        self._writer.writeheader()
        return self

    def write(self, row: dict) -> None:
        assert self._writer is not None, "CsvWriter must be used as a context manager"
        extra = {
            key: value for key, value in row.items() if key not in _CANONICAL_ROW_FIELDS
        }
        self._writer.writerow(
            {
                "entity_id": row.get("entity_id", ""),
                "state": row.get("state", ""),
                "last_changed": row.get("last_changed", ""),
                "last_updated": row.get("last_updated", ""),
                "attributes_json": json.dumps(row.get("attributes"), ensure_ascii=False),
                "local_offset": row.get("local_offset", ""),
                "extra_json": json.dumps(extra, ensure_ascii=False) if extra else "",
            }
        )
        self._count += 1

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._f:
            self._f.flush()
            self._f.close()
            self._f = None

    @property
    def row_count(self) -> int:
        return self._count


# ── Atomic finalisation ───────────────────────────────────────────────────────

def atomic_replace(
    src: Path,
    dst: Path,
    locked_file_retries: int = 5,
    locked_file_retry_sleep: float = 2.0,
) -> None:
    """Move *src* to *dst* atomically, retrying on PermissionError.

    Any program may hold a lock on an output file: a sync client, a virus
    scanner, an indexer. We retry up to *locked_file_retries* times with
    *locked_file_retry_sleep* second intervals before giving up.

    os.replace() is atomic when source and destination are on the same
    volume, which the workspace guarantees.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, locked_file_retries + 2):
        try:
            src.replace(dst)
            return
        except PermissionError:
            if attempt > locked_file_retries:
                raise
            logger.warning(
                "PermissionError replacing %s (file locked?) - "
                "attempt %d/%d, sleeping %.1f s ...",
                dst.name,
                attempt,
                locked_file_retries,
                locked_file_retry_sleep,
            )
            time.sleep(locked_file_retry_sleep)


# ── History payload flattening ────────────────────────────────────────────────

def flatten_payload(payload: list) -> list[dict]:
    """Flatten the HA history response (list-of-lists) to a flat iterable.

    The /api/history/period endpoint returns:
        [ [state, state, ...], [state, state, ...], ... ]
    one inner list per entity that had state changes.

    Entities with no changes in the period simply do not appear — not an error.
    """
    result: list[dict] = []
    for entity_history in payload:
        if isinstance(entity_history, list):
            result.extend(entity_history)
    return result


# ── Parquet post-processing ───────────────────────────────────────────────────

def _parquet_schema():
    """Return the fixed pyarrow schema for HA history state objects.

    Schema design notes:
    - ``last_changed`` / ``last_updated``: stored as ``timestamp[us, tz=UTC]``
      so DuckDB / Pandas can do native time arithmetic without string parsing.
    - ``attributes_json``: stored as a JSON string because HA attributes are
      heterogeneous across entity types (sensors, zones, lights, …) and cannot
      be mapped to a fixed column schema.
    - ``local_offset``: e.g. '+02:00', computed per row at export time, tells
      consumers how to convert UTC to local time without reading the manifest.
    - ``extra_json`` preserves top-level fields added by the HA API without
      making the fixed column schema depend on a particular Core version.
    """
    import pyarrow as pa
    return pa.schema([
        pa.field("entity_id",       pa.string(),                  nullable=False),
        pa.field("state",           pa.string(),                  nullable=False),
        pa.field("last_changed",    pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("last_updated",    pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("attributes_json", pa.string(),                  nullable=True),
        pa.field("local_offset",    pa.string(),                  nullable=False),
        pa.field("extra_json",      pa.string(),                  nullable=True),
    ])


def _parse_ts_utc(ts_str: str | None) -> datetime:
    """Parse an ISO-8601 timestamp string and return a UTC-aware datetime.

    Handles both microsecond and non-microsecond variants from the HA API:
      '2026-06-17T06:55:31.889481+00:00'   (with microseconds)
      '2026-06-16T22:00:00+00:00'           (without microseconds)

    Invalid or missing timestamps are rejected before Parquet publication.
    """
    from .errors import ValidationError

    if not ts_str:
        raise ValidationError("A required timestamp is missing.")
    try:
        parsed = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValidationError("A timestamp is not valid ISO 8601.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError("A timestamp needs a timezone.")
    return parsed.astimezone(timezone.utc)


def convert_jsonl_to_parquet(
    src: Path,
    dst: Path,
    row_group_size: int = 200_000,
    compression: str = "snappy",
) -> int:
    """Convert a validated JSONL export file to a Parquet file.

    Reads *src* in streaming batches of *row_group_size* rows and writes each
    batch as one Parquet row group.  Peak memory usage is bounded to roughly
    one batch at a time (~100–200 MB for a 200 k-row batch).

    The Parquet schema is fixed (see ``_parquet_schema``). Unknown top-level
    fields present in JSONL are retained as an object in ``extra_json`` so
    schema evolution does not discard data or add dynamic columns.

    Args:
        src:            Path to the validated JSONL temp file (local temp dir).
        dst:            Destination path for the Parquet temp file (local temp dir).
        row_group_size: Rows per Parquet row group.  DuckDB can parallelise
                        across row groups, so ≥100 k is recommended.
        compression:    Parquet compression codec.  'snappy' is the default:
                        fast, widely supported, ~5–10× smaller than raw JSONL.

    Returns:
        Total number of rows written.

    Raises:
        ImportError  if pyarrow is not installed.
        ValidationError if *src* does not exist.
    """
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ImportError(
            "pyarrow is required for Parquet output. "
            "Install it with: pip install pyarrow>=15.0"
        ) from exc

    if not src.exists():
        from .validators import ValidationError
        raise ValidationError(f"Source JSONL file does not exist: {src}")

    schema = _parquet_schema()
    dst.parent.mkdir(parents=True, exist_ok=True)
    total_rows = 0

    # Accumulator lists for the current row group batch.
    buf_entity_ids:    list = []
    buf_states:        list = []
    buf_last_changed:  list = []
    buf_last_updated:  list = []
    buf_attributes:    list = []
    buf_local_offsets: list = []
    buf_extras: list = []

    def _flush(writer: pq.ParquetWriter) -> None:
        if not buf_entity_ids:
            return
        table = pa.table(
            {
                "entity_id":       pa.array(buf_entity_ids,    type=pa.string()),
                "state":           pa.array(buf_states,         type=pa.string()),
                "last_changed":    pa.array(buf_last_changed,   type=pa.timestamp("us", tz="UTC")),
                "last_updated":    pa.array(buf_last_updated,   type=pa.timestamp("us", tz="UTC")),
                "attributes_json": pa.array(buf_attributes,     type=pa.string()),
                "local_offset":    pa.array(buf_local_offsets,  type=pa.string()),
                "extra_json":      pa.array(buf_extras,         type=pa.string()),
            },
            schema=schema,
        )
        writer.write_table(table)
        buf_entity_ids.clear()
        buf_states.clear()
        buf_last_changed.clear()
        buf_last_updated.clear()
        buf_attributes.clear()
        buf_local_offsets.clear()
        buf_extras.clear()

    from .readers import read_jsonl

    try:
        with pq.ParquetWriter(str(dst), schema, compression=compression) as writer:
            for record in read_jsonl(src):
                buf_entity_ids.append(record.entity_id)
                buf_states.append(record.state)
                buf_last_changed.append(record.last_changed)
                buf_last_updated.append(record.last_updated)
                buf_attributes.append(
                    json.dumps(record.attributes, ensure_ascii=False)
                )
                buf_local_offsets.append(record.local_offset)
                buf_extras.append(
                    json.dumps(record.extra, ensure_ascii=False) if record.extra else None
                )
                total_rows += 1

                if total_rows % row_group_size == 0:
                    _flush(writer)

            _flush(writer)  # write remaining rows in the last (partial) batch
    except Exception:
        dst.unlink(missing_ok=True)
        raise

    n_groups = max(1, -(-total_rows // row_group_size))  # ceiling division
    logger.info(
        "Parquet written: %s (%d rows, ~%d row group(s), compression=%s).",
        dst.name,
        total_rows,
        n_groups,
        compression,
    )
    return total_rows
