"""What a use case reports back to the command line."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..archive.descriptor import ArchiveZone
from ..domain.manifest import ManifestStatus
from ..domain.plan import ExportPlan
from ..errors import ExitCode


@dataclass(frozen=True)
class EntitySummary:
    """The entities of ``/api/states`` and the ones whose history is requested."""

    candidates: int
    requested: tuple[str, ...]
    excluded: int
    unknown: int
    unavailable: int


@dataclass(frozen=True)
class DayResult:
    day: date
    status: ManifestStatus
    state_object_count: int
    error: str | None = None


@dataclass(frozen=True)
class ExportResult:
    archive_zone: ArchiveZone
    #: None for a snapshot-only configuration, which plans no day.
    plan: ExportPlan | None
    #: None when the run ended before Home Assistant was asked.
    entities: EntitySummary | None = None
    days: tuple[DayResult, ...] = ()
    dry_run: bool = False
    snapshot_file: Path | None = None
    log_file: Path | None = None

    @property
    def exit_code(self) -> ExitCode:
        if any(day.status is ManifestStatus.FAILED for day in self.days):
            return ExitCode.FAILED
        return ExitCode.OK
