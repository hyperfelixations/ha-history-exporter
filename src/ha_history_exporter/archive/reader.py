"""Read-only access to an archive, without its lock.

Reading commands never lock, recover or adopt an archive. They report what
they find, including what the next writer would change: interrupted day
transactions awaiting recovery, an export holding the lock, an archive whose
descriptor is not written yet. See internal dev doc § "Archive session".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from ..domain.formats import Format
from ..domain.manifest import StoredManifest
from ..domain.window import DayWindow
from . import descriptor, transaction
from .descriptor import ArchiveDescriptor, ArchiveZone
from .layout import ArchiveLayout
from .lock import LockState, probe
from .store import load_manifest


@dataclass(frozen=True)
class ArchiveReader:
    layout: ArchiveLayout
    #: None for an archive whose descriptor is not written yet.
    descriptor: ArchiveDescriptor | None
    #: None for an archive without days and without a configured zone.
    zone: ArchiveZone | None

    @classmethod
    def open(cls, layout: ArchiveLayout, *, configured: ZoneInfo | None) -> ArchiveReader:
        """Read the descriptor, or survey the manifests of an archive without one."""
        recorded = descriptor.load(layout)
        return cls(layout, recorded, descriptor.known(layout, recorded, configured=configured))

    @property
    def pending_transactions(self) -> int:
        return transaction.pending(self.layout)

    def lock(self) -> LockState:
        return probe(self.layout.lock_file)

    def manifest_days(self) -> list[date]:
        return [day for day, _ in self.layout.manifest_paths()]

    def manifest(self, day: date) -> StoredManifest | None:
        """The day's manifest; an invalid one raises like everywhere else."""
        if self.zone is None:
            return None
        return load_manifest(self.layout, DayWindow.full(day, self.zone.zone).identity())

    def artifacts(self) -> dict[date, dict[Format, Path]]:
        """Every stored day artifact by day, declared by a manifest or not."""
        found: dict[date, dict[Format, Path]] = {}
        for day, fmt, path in self.layout.artifact_paths():
            found.setdefault(day, {})[fmt] = path
        return found
