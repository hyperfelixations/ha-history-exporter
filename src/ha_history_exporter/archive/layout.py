"""Every path below an output directory.

The visible part is the output contract that existing archives and analysis
tooling rely on::

    hhe-archive.json
    exports/daily/YYYY/MM/YYYY-MM-DD.{jsonl,csv,parquet,manifest.json}
    metadata/entity_snapshot_<local YYYYMMDDTHHMMSS>_<run id>.json
    metadata/export_runs.jsonl
    logs/ha_history_export_<local YYYY-MM-DD_HHMMSS>.log

The dot-prefixed entries belong to HHE's own bookkeeping: the archive lock,
staging and transaction journals. See internal dev doc § "Archive layout".
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

DESCRIPTOR_NAME = "hhe-archive.json"
LOCK_NAME = ".hhe-archive.lock"
STAGING_NAME = ".hhe-staging"
JOURNAL_NAME = ".hhe-transactions"
MANIFEST_SUFFIX = "manifest.json"


@dataclass(frozen=True)
class ArchiveLayout:
    root: Path

    @property
    def descriptor_file(self) -> Path:
        return self.root / DESCRIPTOR_NAME

    @property
    def daily_root(self) -> Path:
        return self.root / "exports" / "daily"

    def day_dir(self, day: date) -> Path:
        return self.daily_root / f"{day.year:04d}" / f"{day.month:02d}"

    def day_file(self, day: date, suffix: str) -> Path:
        """One day's file, for example ``exports/daily/2026/06/2026-06-15.jsonl``."""
        return self.day_dir(day) / f"{day.isoformat()}.{suffix}"

    def manifest_file(self, day: date) -> Path:
        return self.day_file(day, MANIFEST_SUFFIX)

    def manifest_paths(self) -> Iterator[tuple[date, Path]]:
        """Every stored day manifest in day order; other files are not HHE's."""
        found = []
        for path in self.daily_root.glob(f"*/*/*.{MANIFEST_SUFFIX}"):
            try:
                day = date.fromisoformat(path.name.removesuffix(f".{MANIFEST_SUFFIX}"))
            except ValueError:
                continue
            if path == self.manifest_file(day):
                found.append((day, path))
        yield from sorted(found)

    @property
    def metadata_dir(self) -> Path:
        return self.root / "metadata"

    @property
    def run_log(self) -> Path:
        return self.metadata_dir / "export_runs.jsonl"

    def snapshot_file(self, created_local: datetime, run_id: str) -> Path:
        stamp = created_local.strftime("%Y%m%dT%H%M%S")
        return self.metadata_dir / f"entity_snapshot_{stamp}_{run_id}.json"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    def log_file(self, started_local: datetime) -> Path:
        return self.logs_dir / f"ha_history_export_{started_local:%Y-%m-%d_%H%M%S}.log"

    @property
    def lock_file(self) -> Path:
        return self.root / LOCK_NAME

    @property
    def staging_root(self) -> Path:
        return self.root / STAGING_NAME

    def staging_dir(self, run_id: str) -> Path:
        return self.staging_root / run_id

    @property
    def journal_root(self) -> Path:
        return self.root / JOURNAL_NAME
