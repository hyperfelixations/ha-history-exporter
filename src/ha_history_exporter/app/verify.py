"""Checking stored days against their manifests, without the lock.

Every artifact a manifest declares is compared with it: size and SHA-256 by
default; ``deep`` also decodes it and compares row count and logical digest,
and checks every record against the day's window. A file of a day that no
manifest declares is unexpected, for example a format left behind by an
earlier generation. A declared file that is missing is reported, not failed:
moving days to other storage is allowed. Legacy manifests record no
checksums, so their files are only found present, or decoded and counted.
See internal dev doc § "Commands".
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from enum import Enum
from pathlib import Path

from ..archive.layout import ArchiveLayout
from ..archive.reader import ArchiveReader
from ..domain.clock import Clock
from ..domain.formats import FORMAT_ORDER, Format
from ..domain.manifest import Artifact, LegacyManifest, Manifest14
from ..domain.plan import DaySelection
from ..domain.stored import RecordContract
from ..domain.window import DayWindow
from ..errors import ExportError, ValidationError
from ..formats import FORMATS
from ..formats.base import sha256_of
from ..formats.validation import inspect_artifact
from ..settings import Config
from .reading import archive_state, requested_range
from .results import ArtifactCheck, ArtifactStatus, DayVerification, VerifyResult


class Depth(Enum):
    """How closely an artifact is checked."""

    #: Declared files exist; nothing is read. ``doctor`` checks this deep.
    PRESENCE = 1
    #: Size and SHA-256.
    CHECKSUMS = 2
    #: Decoded completely as well.
    DECODED = 3


def verify_archive(
    cfg: Config, selection: DaySelection | None, *, depth: Depth, clock: Clock
) -> VerifyResult:
    reader = ArchiveReader.open(
        ArchiveLayout(cfg.export.output_dir), configured=cfg.export.timezone
    )
    span = requested_range(reader, selection, clock)
    stored = reader.artifacts()
    manifests = set(reader.manifest_days())
    days = sorted(
        day for day in manifests | set(stored) if span is None or span[0] <= day <= span[1]
    )
    results = tuple(
        verify_day(reader, day, stored.get(day, {}), has_manifest=day in manifests, depth=depth)
        for day in days
    )
    return VerifyResult(archive_state(reader), depth is Depth.DECODED, results)


def verify_day(
    reader: ArchiveReader,
    day: date,
    stored: dict[Format, Path],
    *,
    has_manifest: bool,
    depth: Depth,
) -> DayVerification:
    unexpected = _unexpected(stored, declared=())
    if not has_manifest or reader.zone is None:
        return DayVerification(day, None, None, unexpected)
    try:
        manifest = reader.manifest(day)
    except ExportError as exc:
        return DayVerification(day, None, None, (), invalid=exc.summary)
    if manifest is None:  # pragma: no cover - the manifest vanished since it was listed
        return DayVerification(day, None, None, unexpected)
    layout = reader.layout
    window = DayWindow.full(day, reader.zone.zone)
    if isinstance(manifest, Manifest14):
        checks = [
            _check_artifact(layout.day_file(day, fmt.value), fmt, artifact, window, depth)
            for fmt, artifact in sorted_artifacts(manifest)
        ]
        declared: Iterable[Format] = manifest.artifacts
    else:
        declared = [fmt for fmt, name in manifest.output_files.items() if name is not None]
        checks = [
            _check_legacy(layout.day_file(day, fmt.value), fmt, manifest, window, depth)
            for fmt in FORMAT_ORDER
            if fmt in declared
        ]
    return DayVerification(
        day,
        manifest.status,
        manifest.schema_version,
        (*checks, *_unexpected(stored, declared)),
    )


def sorted_artifacts(manifest: Manifest14) -> list[tuple[Format, Artifact]]:
    return [(fmt, manifest.artifacts[fmt]) for fmt in FORMAT_ORDER if fmt in manifest.artifacts]


def _unexpected(
    stored: dict[Format, Path], declared: Iterable[Format]
) -> tuple[ArtifactCheck, ...]:
    known = set(declared)
    return tuple(
        ArtifactCheck(fmt, path, ArtifactStatus.UNEXPECTED, "no manifest declares this file")
        for fmt, path in stored.items()
        if fmt not in known
    )


def _check_artifact(
    path: Path, fmt: Format, expected: Artifact, window: DayWindow, depth: Depth
) -> ArtifactCheck:
    if not path.is_file():
        return ArtifactCheck(fmt, path, ArtifactStatus.MISSING)
    if depth is Depth.PRESENCE:
        return ArtifactCheck(fmt, path, ArtifactStatus.OK)
    decode = depth is Depth.DECODED and FORMATS[fmt].requirement_met()
    try:
        if decode:
            found = inspect_artifact(path, fmt, contract=RecordContract(window))
        else:
            with path.open("rb") as stream:
                sha256, size = sha256_of(stream)
            found = Artifact(path.name, size, sha256, expected.row_count, expected.logical_sha256)
    except ValidationError as exc:
        return ArtifactCheck(fmt, path, ArtifactStatus.UNREADABLE, exc.summary)
    except OSError as exc:
        return ArtifactCheck(fmt, path, ArtifactStatus.UNREADABLE, type(exc).__name__)
    differences = [
        name
        for name in ("size_bytes", "sha256", "row_count", "logical_sha256")
        if getattr(found, name) != getattr(expected, name)
    ]
    if differences:
        return ArtifactCheck(
            fmt,
            path,
            ArtifactStatus.MISMATCH,
            f"differs from the manifest: {', '.join(differences)}",
        )
    note = None
    if depth is Depth.DECODED and not decode:
        note = f"checksums only; decoding needs {FORMATS[fmt].requirement}"
    return ArtifactCheck(fmt, path, ArtifactStatus.OK, note)


def _check_legacy(
    path: Path, fmt: Format, manifest: LegacyManifest, window: DayWindow, depth: Depth
) -> ArtifactCheck:
    if not path.is_file():
        return ArtifactCheck(fmt, path, ArtifactStatus.MISSING)
    if depth is not Depth.DECODED or not FORMATS[fmt].requirement_met():
        return ArtifactCheck(
            fmt, path, ArtifactStatus.PRESENT, f"schema {manifest.schema_version} has no checksums"
        )
    try:
        found = inspect_artifact(path, fmt, contract=RecordContract(window))
    except ValidationError as exc:
        return ArtifactCheck(fmt, path, ArtifactStatus.UNREADABLE, exc.summary)
    except OSError as exc:
        return ArtifactCheck(fmt, path, ArtifactStatus.UNREADABLE, type(exc).__name__)
    if found.row_count != manifest.state_object_count:
        return ArtifactCheck(
            fmt,
            path,
            ArtifactStatus.MISMATCH,
            f"{found.row_count} rows, the manifest counts {manifest.state_object_count}",
        )
    return ArtifactCheck(fmt, path, ArtifactStatus.OK, "decoded; the manifest has no checksums")
