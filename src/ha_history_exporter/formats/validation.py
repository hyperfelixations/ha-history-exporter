"""Independent read-back of written artifacts.

Each artifact is decoded by its own reader after it was written; its row
count and logical digest must equal those of the captured records. Size and
SHA-256 come from the bytes on disk, hashed while they are decoded. See
internal dev doc § "Record contract".
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..domain.formats import FORMAT_ORDER, Format
from ..domain.records import HistoryRecord, LogicalDigest
from ..domain.stored import RecordContract, check_against_request
from ..errors import ValidationError
from . import FORMATS
from .base import HashingReader, sha256_of


@dataclass(frozen=True)
class ArtifactMetadata:
    """Physical and logical evidence for one artifact, as the manifest records it."""

    filename: str
    size_bytes: int
    sha256: str
    row_count: int
    logical_sha256: str


@dataclass(frozen=True)
class CaptureSummary:
    """What the capture wrote: the row count and logical digest every artifact must match."""

    rows: int
    logical_sha256: str


def inspect_artifact(
    path: Path,
    fmt: Format,
    *,
    contract: RecordContract | None = None,
) -> ArtifactMetadata:
    """Decode *path* completely and report what it holds.

    With a *contract*, every record must belong to that request.
    """
    spec = FORMATS[fmt]
    label = fmt.value.upper()
    digest = LogicalDigest()
    seen: set[str] = set()

    def consume(records: Iterator[HistoryRecord]) -> None:
        for index, record in enumerate(records, start=1):
            if contract is not None:
                check_against_request(
                    record,
                    contract,
                    first_for_entity=record.entity_id not in seen,
                    location=f"{path.name}, record {index}",
                )
            seen.add(record.entity_id)
            digest.add(record)

    try:
        with path.open("rb") as raw:
            if spec.sequential:
                hashing = HashingReader(raw)
                stream = io.BufferedReader(hashing)
                consume(spec.read(stream, path.name))
                stream.read()
                sha256, size = hashing.hexdigest(), hashing.size
            else:
                sha256, size = sha256_of(raw)
                raw.seek(0)
                consume(spec.read(raw, path.name))
    except FileNotFoundError as exc:
        raise ValidationError(f"{label} file {path.name} does not exist.") from exc
    except OSError as exc:
        raise ValidationError(f"Cannot read {label} file {path.name}.") from exc
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{label} file {path.name} is not valid UTF-8.") from exc

    return ArtifactMetadata(
        filename=path.name,
        size_bytes=size,
        sha256=sha256,
        row_count=digest.rows,
        logical_sha256=digest.hexdigest(),
    )


def validate_artifacts(
    paths: Mapping[Format, Path],
    *,
    contract: RecordContract,
    expected: CaptureSummary,
) -> dict[Format, ArtifactMetadata]:
    """Inspect every artifact and prove it holds exactly the captured records."""
    artifacts: dict[Format, ArtifactMetadata] = {}
    for fmt in FORMAT_ORDER:
        if fmt not in paths:
            continue
        path = paths[fmt]
        metadata = inspect_artifact(path, fmt, contract=contract)
        if metadata.row_count != expected.rows:
            raise ValidationError(
                f"{fmt.value.upper()} row count mismatch in {path.name}: "
                f"expected {expected.rows}, found {metadata.row_count}."
            )
        if metadata.logical_sha256 != expected.logical_sha256:
            raise ValidationError(f"{path.name} does not hold the captured records.")
        artifacts[fmt] = metadata
    return artifacts
