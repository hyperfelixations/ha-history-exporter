"""What a use case reports back to the command line.

Every result knows its exit code and its ``--json`` document: plain JSON
values, dates and paths as strings, enumerations by value. See internal dev
doc § "Commands".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..archive.descriptor import ArchiveZone
from ..domain.formats import FORMAT_ORDER, Format
from ..domain.manifest import ManifestStatus
from ..domain.plan import ExportPlan
from ..errors import ExitCode


def _path(path: Path | None) -> str | None:
    return None if path is None else str(path)


def _formats(formats: Iterable[Format]) -> list[str]:
    chosen = set(formats)
    return [fmt.value for fmt in FORMAT_ORDER if fmt in chosen]


def _zone(zone: ArchiveZone | None) -> dict[str, Any] | None:
    if zone is None:
        return None
    return {"timezone": zone.zone.key, "source": zone.source.value}


# ── archive state ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ArchiveState:
    """What a reading command found besides the days: the state a writer would change."""

    output_dir: Path
    #: None for an archive without days and without a configured zone.
    zone: ArchiveZone | None
    #: False until the first writing command records ``hhe-archive.json``.
    described: bool
    pending_transactions: int
    #: The lock holder's description while an export or conversion runs.
    running: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "zone": _zone(self.zone),
            "described": self.described,
            "pending_transactions": self.pending_transactions,
            "running": self.running,
        }


# ── export ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EntitySummary:
    """The entities of ``/api/states`` and the ones whose history is requested."""

    candidates: int
    requested: tuple[str, ...]
    excluded: int
    unknown: int
    unavailable: int

    def to_json(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "requested": len(self.requested),
            "excluded": self.excluded,
            "unknown": self.unknown,
            "unavailable": self.unavailable,
        }


@dataclass(frozen=True)
class DayResult:
    day: date
    status: ManifestStatus
    state_object_count: int
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "status": self.status.value,
            "state_object_count": self.state_object_count,
            "error": self.error,
        }


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

    def to_json(self) -> dict[str, Any]:
        plan = self.plan
        return {
            "command": "export",
            "exit_code": int(self.exit_code),
            "dry_run": self.dry_run,
            "zone": _zone(self.archive_zone),
            "plan": None
            if plan is None
            else {
                "requested_start": plan.requested_start.isoformat(),
                "requested_end": plan.requested_end.isoformat(),
                "today": plan.today.isoformat(),
                "days": [
                    {
                        "day": decision.day.isoformat(),
                        "action": decision.action.value,
                        "reason": decision.reason,
                    }
                    for decision in plan.decisions
                ],
            },
            "entities": None if self.entities is None else self.entities.to_json(),
            "days": [day.to_json() for day in self.days],
            "snapshot_file": _path(self.snapshot_file),
            "log_file": _path(self.log_file),
        }


# ── verify ────────────────────────────────────────────────────────────────────


class ArtifactStatus(StrEnum):
    #: Size and SHA-256 match the manifest; decoded as well with ``--deep``.
    OK = "ok"
    #: The file exists; its legacy manifest records no checksum to compare.
    PRESENT = "present"
    #: Declared by the manifest, but not there, for example moved to cold storage.
    MISSING = "missing"
    MISMATCH = "mismatch"
    UNREADABLE = "unreadable"
    #: A day file that no manifest declares.
    UNEXPECTED = "unexpected"


#: Findings that make ``verify`` fail; a missing file is reported only.
PROBLEMS = frozenset(
    {ArtifactStatus.MISMATCH, ArtifactStatus.UNREADABLE, ArtifactStatus.UNEXPECTED}
)


@dataclass(frozen=True)
class ArtifactCheck:
    format: Format
    path: Path
    status: ArtifactStatus
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "format": self.format.value,
            "path": str(self.path),
            "status": self.status.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DayVerification:
    day: date
    #: None for files of a day without a manifest.
    status: ManifestStatus | None
    schema_version: str | None
    artifacts: tuple[ArtifactCheck, ...]
    #: Why the manifest cannot be trusted, when it cannot.
    invalid: str | None = None

    @property
    def failed(self) -> bool:
        return self.invalid is not None or any(check.status in PROBLEMS for check in self.artifacts)

    def to_json(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "status": None if self.status is None else self.status.value,
            "schema_version": self.schema_version,
            "invalid": self.invalid,
            "artifacts": [check.to_json() for check in self.artifacts],
        }


@dataclass(frozen=True)
class VerifyResult:
    archive: ArchiveState
    deep: bool
    days: tuple[DayVerification, ...]

    def count(self, status: ArtifactStatus) -> int:
        return sum(check.status is status for day in self.days for check in day.artifacts)

    @property
    def invalid_days(self) -> list[date]:
        return [day.day for day in self.days if day.invalid is not None]

    @property
    def exit_code(self) -> ExitCode:
        return ExitCode.FAILED if any(day.failed for day in self.days) else ExitCode.OK

    def to_json(self) -> dict[str, Any]:
        return {
            "command": "verify",
            "exit_code": int(self.exit_code),
            "deep": self.deep,
            "archive": self.archive.to_json(),
            "summary": {
                "days": len(self.days),
                "invalid_manifests": len(self.invalid_days),
                **{status.value: self.count(status) for status in ArtifactStatus},
            },
            "days": [day.to_json() for day in self.days],
        }


# ── status ────────────────────────────────────────────────────────────────────


class DayState(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    PENDING = "pending"
    INVALID = "invalid"
    MISSING = "missing"


@dataclass(frozen=True)
class DayStatus:
    day: date
    state: DayState


@dataclass(frozen=True)
class StatusResult:
    archive: ArchiveState
    #: The covered days, oldest first; empty for an archive without days.
    days: tuple[DayStatus, ...]

    def months(self) -> list[tuple[str, list[DayStatus]]]:
        """The days grouped by month, ``YYYY-MM``, in order."""
        grouped: dict[str, list[DayStatus]] = {}
        for status in self.days:
            grouped.setdefault(f"{status.day:%Y-%m}", []).append(status)
        return list(grouped.items())

    @property
    def exit_code(self) -> ExitCode:
        return ExitCode.OK

    def to_json(self) -> dict[str, Any]:
        return {
            "command": "status",
            "exit_code": int(self.exit_code),
            "archive": self.archive.to_json(),
            "start": self.days[0].day.isoformat() if self.days else None,
            "end": self.days[-1].day.isoformat() if self.days else None,
            "months": [
                {
                    "month": month,
                    **{
                        state.value: [d.day.isoformat() for d in days if d.state is state]
                        for state in DayState
                    },
                }
                for month, days in self.months()
            ],
        }


# ── convert ───────────────────────────────────────────────────────────────────


class ConversionOutcome(StrEnum):
    CONVERTED = "converted"
    #: The day already has every requested format.
    PRESENT = "present"
    #: The day has no successful schema 1.4 manifest to derive from.
    SKIPPED = "skipped"
    #: The source artifact is missing or differs from its manifest.
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class DayConversion:
    day: date
    outcome: ConversionOutcome
    added: tuple[Format, ...] = ()
    source: Format | None = None
    reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "outcome": self.outcome.value,
            "added": _formats(self.added),
            "source": None if self.source is None else self.source.value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ConvertResult:
    formats: frozenset[Format]
    days: tuple[DayConversion, ...]
    log_file: Path | None = None

    def count(self, outcome: ConversionOutcome) -> int:
        return sum(day.outcome is outcome for day in self.days)

    @property
    def exit_code(self) -> ExitCode:
        if self.count(ConversionOutcome.BLOCKED) or self.count(ConversionOutcome.FAILED):
            return ExitCode.FAILED
        return ExitCode.OK

    def to_json(self) -> dict[str, Any]:
        return {
            "command": "convert",
            "exit_code": int(self.exit_code),
            "formats": _formats(self.formats),
            "summary": {outcome.value: self.count(outcome) for outcome in ConversionOutcome},
            "days": [day.to_json() for day in self.days],
            "log_file": _path(self.log_file),
        }


# ── doctor ────────────────────────────────────────────────────────────────────


class CheckStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    status: CheckStatus
    label: str
    detail: str = ""
    remedy: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "label": self.label,
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass(frozen=True)
class DoctorResult:
    checks: tuple[Check, ...]

    def count(self, status: CheckStatus) -> int:
        return sum(check.status is status for check in self.checks)

    @property
    def exit_code(self) -> ExitCode:
        return ExitCode.FAILED if self.count(CheckStatus.FAIL) else ExitCode.OK

    def to_json(self) -> dict[str, Any]:
        return {
            "command": "doctor",
            "exit_code": int(self.exit_code),
            "checks": [check.to_json() for check in self.checks],
        }
