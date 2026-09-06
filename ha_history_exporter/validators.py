"""Post-write file validation.

After streaming is complete, we verify that the output files are coherent
before atomically renaming them to their final names.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .errors import ValidationError

logger = logging.getLogger(__name__)

__all__ = ["ValidationError", "validate_csv", "validate_jsonl", "validate_parquet"]


def validate_jsonl(path: Path, expected_rows: int) -> int:
    """Validate a JSONL file and return the actual row count.

    Checks:
      - File exists and is not empty (unless expected_rows == 0).
      - Every non-empty line is valid JSON.
      - Actual row count matches expected_rows.

    Raises:
        ValidationError on any mismatch.
    """
    if not path.exists():
        raise ValidationError(f"JSONL file does not exist: {path}")

    actual = 0
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValidationError(
                    f"Invalid JSON on line {lineno} of {path.name}: {exc}"
                ) from exc
            actual += 1

    if actual != expected_rows:
        raise ValidationError(
            f"JSONL row count mismatch in {path.name}: "
            f"expected {expected_rows}, found {actual}."
        )

    logger.debug("JSONL validated: %s (%d rows).", path.name, actual)
    return actual


def validate_csv(path: Path, expected_rows: int) -> int:
    """Validate a CSV file (header + data rows) and return the data row count.

    Raises:
        ValidationError on mismatch.
    """
    if not path.exists():
        raise ValidationError(f"CSV file does not exist: {path}")

    import csv

    actual = 0
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try:
            next(reader)  # skip header
        except StopIteration:
            raise ValidationError(
                f"CSV file is empty (no header): {path.name}"
            ) from None
        for _ in reader:
            actual += 1

    if actual != expected_rows:
        raise ValidationError(
            f"CSV row count mismatch in {path.name}: "
            f"expected {expected_rows}, found {actual}."
        )

    logger.debug("CSV validated: %s (%d data rows).", path.name, actual)
    return actual


def validate_parquet(path: Path, expected_rows: int) -> int:
    """Validate a Parquet file and return the actual row count.

    Uses pyarrow.parquet.read_metadata() which reads only the Parquet file
    footer (a few KB) — it does NOT load row data into memory, so this is
    fast even for files with millions of rows.

    Also verifies that the required columns are present in the schema so that
    a corrupt or truncated write is caught before the file is moved to cloud-storage.

    Raises:
        ValidationError on any mismatch or missing column.
        ImportError     if pyarrow is not installed.
    """
    if not path.exists():
        raise ValidationError(f"Parquet file does not exist: {path}")

    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ImportError(
            "pyarrow is required for Parquet validation. "
            "Install it with: pip install pyarrow>=15.0"
        ) from exc

    try:
        meta = pq.read_metadata(path)
        schema = pq.read_schema(path)
    except Exception as exc:
        raise ValidationError(
            f"Cannot read Parquet metadata from {path.name}: {exc}"
        ) from exc

    # Check required columns
    required_columns = {
        "entity_id", "state", "last_changed", "last_updated",
        "attributes_json", "local_offset",
    }
    present = set(schema.names)
    missing = required_columns - present
    if missing:
        raise ValidationError(
            f"Parquet schema missing required columns in {path.name}: {sorted(missing)}"
        )

    actual = meta.num_rows
    if actual != expected_rows:
        raise ValidationError(
            f"Parquet row count mismatch in {path.name}: "
            f"expected {expected_rows}, found {actual}."
        )

    logger.debug(
        "Parquet validated: %s (%d rows, %d row group(s)).",
        path.name,
        actual,
        meta.num_row_groups,
    )
    return actual
