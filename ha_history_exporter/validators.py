"""Strict post-write validation shared by every output format."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .errors import ValidationError
from .history_records import HistoryRecord, validate_stored_record
from .readers import read_records
from .settings.model import Format, HistoryRequestSettings

logger = logging.getLogger(__name__)

__all__ = [
    "ArtifactMetadata",
    "ValidationError",
    "validate_csv",
    "validate_export_artifacts",
    "validate_jsonl",
    "validate_parquet",
]


@dataclass(frozen=True)
class ArtifactMetadata:
    """Physical and logical evidence for one validated output file."""

    filename: str
    size_bytes: int
    sha256: str
    row_count: int
    logical_sha256: str


def _physical_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _logical_bytes(record: HistoryRecord) -> bytes:
    return (
        json.dumps(
            record.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _validate_records(
    path: Path,
    fmt: Format,
    expected_rows: int,
    *,
    requested_entity_ids: frozenset[str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    settings: HistoryRequestSettings | None = None,
    local_timezone: ZoneInfo | None = None,
) -> ArtifactMetadata:
    if not path.exists():
        raise ValidationError(f"{fmt.value.upper()} file does not exist: {path}")

    logical = hashlib.sha256()
    actual = 0
    seen_entities: set[str] = set()
    contract_values = (start, end, settings, local_timezone)
    if any(value is not None for value in contract_values) and not all(
        value is not None for value in contract_values
    ):
        raise ValueError("The complete validation contract must be supplied together.")

    try:
        records: Iterable[HistoryRecord] = read_records(path, fmt)
        for record in records:
            if start is not None:
                assert end is not None
                assert settings is not None
                assert local_timezone is not None
                assert requested_entity_ids is not None
                validate_stored_record(
                    record,
                    requested_entity_ids=requested_entity_ids,
                    start=start.astimezone(timezone.utc),
                    end=end.astimezone(timezone.utc),
                    allow_start_state=not settings.skip_initial_state,
                    first_for_entity=record.entity_id not in seen_entities,
                    local_timezone=local_timezone,
                )
            seen_entities.add(record.entity_id)
            logical.update(_logical_bytes(record))
            actual += 1
    except OSError as exc:
        raise ValidationError(f"Cannot read {fmt.value.upper()} file {path.name}.") from exc

    if actual != expected_rows:
        raise ValidationError(
            f"{fmt.value.upper()} row count mismatch in {path.name}: "
            f"expected {expected_rows}, found {actual}."
        )

    result = ArtifactMetadata(
        filename=path.name,
        size_bytes=path.stat().st_size,
        sha256=_physical_sha256(path),
        row_count=actual,
        logical_sha256=logical.hexdigest(),
    )
    logger.debug("%s validated: %s (%d rows).", fmt.value.upper(), path.name, actual)
    return result


def validate_jsonl(path: Path, expected_rows: int) -> int:
    return _validate_records(path, Format.JSONL, expected_rows).row_count


def validate_csv(path: Path, expected_rows: int) -> int:
    return _validate_records(path, Format.CSV, expected_rows).row_count


def validate_parquet(path: Path, expected_rows: int) -> int:
    return _validate_records(path, Format.PARQUET, expected_rows).row_count


def validate_export_artifacts(
    paths: Mapping[Format, Path],
    *,
    expected_rows: int,
    requested_entity_ids: Iterable[str],
    start: datetime,
    end: datetime,
    settings: HistoryRequestSettings,
    local_timezone: ZoneInfo,
) -> dict[Format, ArtifactMetadata]:
    """Validate all artifacts and prove that their logical rows are equal."""
    requested = frozenset(requested_entity_ids)
    artifacts = {
        fmt: _validate_records(
            path,
            fmt,
            expected_rows,
            requested_entity_ids=requested,
            start=start,
            end=end,
            settings=settings,
            local_timezone=local_timezone,
        )
        for fmt, path in paths.items()
    }
    logical_digests = {artifact.logical_sha256 for artifact in artifacts.values()}
    if len(logical_digests) > 1:
        raise ValidationError("The logical records differ between output formats.")
    return artifacts
