"""Valid manifests for tests, built through the domain model."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from ha_history_exporter.domain.formats import Format
from ha_history_exporter.domain.manifest import (
    Artifact,
    Manifest14,
    ManifestStatus,
    Retention,
    RetentionStatus,
)
from ha_history_exporter.domain.query import HistoryQuery
from ha_history_exporter.domain.window import DayIdentity, DayWindow

DAY = date(2026, 7, 28)


def identity(day: date = DAY, timezone: str = "Europe/Berlin") -> DayIdentity:
    return DayWindow.full(day, ZoneInfo(timezone)).identity()


def manifest14(
    day: date = DAY,
    *,
    status: ManifestStatus = ManifestStatus.OK,
    formats: Iterable[Format] = (Format.JSONL,),
    state_object_count: int = 4,
    timezone: str = "Europe/Berlin",
) -> Manifest14:
    """A consistent manifest; a failed one carries an error and no artifacts."""
    failed = status is ManifestStatus.FAILED
    artifacts = (
        {}
        if failed
        else {
            fmt: Artifact(
                filename=f"{day}.{fmt.value}",
                size_bytes=100,
                sha256="a" * 64,
                row_count=state_object_count,
                logical_sha256="b" * 64,
            )
            for fmt in formats
        }
    )
    return Manifest14(
        identity=identity(day, timezone),
        status=status,
        export_started_at="2026-07-29T03:30:01+02:00",
        export_finished_at="2026-07-29T03:30:05+02:00",
        duration_seconds=4.2,
        entity_count_current=2,
        entity_count_requested=2,
        entity_count_with_history=0 if failed else 1,
        entity_count_zero_history=0 if failed else 1,
        state_object_count=0 if failed else state_object_count,
        batch_size_entities=15,
        request_count=1,
        failed_request_count=1 if failed else 0,
        retried_request_count=0,
        query=HistoryQuery(),
        retention=Retention(RetentionStatus.KNOWN_SAFE, 14),
        artifacts=artifacts,
        zero_history_entities=() if failed else ("sensor.no_history",),
        failed_batches=(),
        error="Synthetic export failure." if failed else None,
        error_code="export_error" if failed else None,
        script_version="1.4.0",
    )


def write_manifest(output_dir: Path, manifest: Manifest14) -> Path:
    day = manifest.identity.day
    path = output_dir / "exports" / "daily" / f"{day.year}" / f"{day.month:02d}"
    path = path / f"{day}.manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(manifest.encode())
    return path
