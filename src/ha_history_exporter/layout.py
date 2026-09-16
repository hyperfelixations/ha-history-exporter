"""Where an export run puts its files.

This is the one part of HHE that must never change: existing archives were
written with this layout, and the analysis tooling globs for exactly these
paths. It lives in its own module so the contract has a single, small,
heavily tested home instead of being spread over configuration properties.

Layout below the configured output directory::

    exports/daily/YYYY/MM/YYYY-MM-DD.jsonl
    exports/daily/YYYY/MM/YYYY-MM-DD.csv
    exports/daily/YYYY/MM/YYYY-MM-DD.parquet
    exports/daily/YYYY/MM/YYYY-MM-DD.manifest.json
    metadata/entity_snapshot_<time>_<run-id>.json
    metadata/export_runs.jsonl
    logs/ha_history_export_<time>.log

See internal dev doc, Ausgabedateien.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

#: Directory names, fixed by the contract.
EXPORTS_DIR = "exports"
DAILY_DIR = "daily"
METADATA_DIR = "metadata"
LOGS_DIR = "logs"


@dataclass(frozen=True)
class ExportLayout:
    """Resolves every output path below one output directory."""

    root: Path

    @property
    def daily_root(self) -> Path:
        return self.root / EXPORTS_DIR / DAILY_DIR

    @property
    def metadata_dir(self) -> Path:
        return self.root / METADATA_DIR

    @property
    def logs_dir(self) -> Path:
        return self.root / LOGS_DIR

    def day_dir(self, day: date | str) -> Path:
        """Directory holding one day's files: ``.../YYYY/MM``.

        The month is zero-padded so directories sort and glob correctly.
        """
        parsed, _ = _normalize_day(day)
        return self.daily_root / f"{parsed.year:04d}" / f"{parsed.month:02d}"

    def day_file(self, day: date | str, suffix: str) -> Path:
        """One day's file, for example ``.../2026/06/2026-06-15.jsonl``."""
        parsed, text = _normalize_day(day)
        return self.day_dir(parsed) / f"{text}.{suffix}"


def _normalize_day(day: date | str) -> tuple[date, str]:
    """Accept both a date object and an ISO string, return both forms."""
    if isinstance(day, str):
        return date.fromisoformat(day), day
    return day, day.isoformat()
