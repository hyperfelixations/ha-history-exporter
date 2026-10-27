"""Independent read-back of written artifacts.

Each artifact is decoded by its own reader after it was written; its row
count and logical digest must equal those of the captured records. Size and
SHA-256 come from the bytes on disk, hashed while they are decoded. See
internal dev doc § "Record contract".
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..domain.formats import FORMAT_ORDER, Format
from ..domain.manifest import Artifact
from ..domain.records import HistoryRecord, LogicalDigest
from ..domain.stored import RecordContract, check_against_request
from ..errors import ValidationError
from . import FORMATS
from .base import HashingReader, sha256_of


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
    each: Callable[[HistoryRecord], None] | None = None,
) -> Artifact:
    """Decode *path* completely and report what it holds.

    With a *contract*, every record must belong to that request. *each*
    receives every record as it is decoded.
    """
    spec = FORMATS[fmt]
    label = fmt.value.upper()
    digest = LogicalDigest()

    def consume(records: Iterator[HistoryRecord]) -> None:
        for index, record in enumerate(records, start=1):
            if contract is not None:
                check_against_request(record, contract, location=f"{path.name}, record {index}")
            digest.add(record)
            if each is not None:
                each(record)

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

    return Artifact(
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
) -> dict[Format, Artifact]:
    """Inspect every artifact and prove it holds exactly the captured records."""
    artifacts: dict[Format, Artifact] = {}
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
