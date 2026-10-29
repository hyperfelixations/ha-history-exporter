"""The archive layout, stated literally.

Every visible path was written into existing archives and is globbed by
analysis tooling, so the expected strings are spelled out rather than
recomputed.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.archive.layout import ArchiveLayout

ROOT = Path("archive")


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


LAYOUT = ArchiveLayout(ROOT)


def test_fixed_locations() -> None:
    assert rel(LAYOUT.daily_root) == "exports/daily"
    assert rel(LAYOUT.metadata_dir) == "metadata"
    assert rel(LAYOUT.run_log) == "metadata/export_runs.jsonl"
    assert rel(LAYOUT.logs_dir) == "logs"
    assert rel(LAYOUT.lock_file) == ".hhe-archive.lock"
    assert rel(LAYOUT.staging_dir("run")) == ".hhe-staging/run"
    assert rel(LAYOUT.journal_root) == ".hhe-transactions"


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 1, 1), "exports/daily/2026/01"),
        (date(2026, 9, 30), "exports/daily/2026/09"),
        (date(2026, 12, 31), "exports/daily/2026/12"),
        (date(2027, 2, 28), "exports/daily/2027/02"),
    ],
)
def test_day_directories_are_year_then_zero_padded_month(day: date, expected: str) -> None:
    assert rel(LAYOUT.day_dir(day)) == expected


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        ("jsonl", "exports/daily/2026/06/2026-06-15.jsonl"),
        ("csv", "exports/daily/2026/06/2026-06-15.csv"),
        ("parquet", "exports/daily/2026/06/2026-06-15.parquet"),
        ("manifest.json", "exports/daily/2026/06/2026-06-15.manifest.json"),
    ],
)
def test_day_files_carry_the_iso_date_and_the_suffix(suffix: str, expected: str) -> None:
    assert rel(LAYOUT.day_file(date(2026, 6, 15), suffix)) == expected
    assert LAYOUT.manifest_file(date(2026, 6, 15)) == LAYOUT.day_file(
        date(2026, 6, 15), "manifest.json"
    )


def test_snapshot_and_log_names_carry_local_time_and_run() -> None:
    local = datetime(2026, 10, 16, 10, 44, 6, 123, tzinfo=ZoneInfo("Europe/Berlin"))
    run_id = "20261016T084406-711-trvw22"
    assert rel(LAYOUT.snapshot_file(local, run_id)) == (
        "metadata/entity_snapshot_20261016T104406_20261016T084406-711-trvw22.json"
    )
    assert rel(LAYOUT.log_file(local)) == "logs/ha_history_export_2026-10-16_104406.log"


def test_resolving_paths_creates_nothing(tmp_path: Path) -> None:
    layout = ArchiveLayout(tmp_path / "archive")
    layout.day_file(date(2026, 6, 15), "jsonl")
    layout.snapshot_file(datetime(2026, 6, 15, tzinfo=ZoneInfo("UTC")), "run")
    assert not layout.root.exists()


def test_the_layout_is_an_immutable_value() -> None:
    with pytest.raises(FrozenInstanceError):
        LAYOUT.root = ROOT / "elsewhere"  # type: ignore[misc]
    assert ArchiveLayout(ROOT) == ArchiveLayout(ROOT)
    assert ArchiveLayout(ROOT) != ArchiveLayout(ROOT / "other")


def test_artifact_paths_lists_day_files_only(tmp_path: Path) -> None:
    layout = ArchiveLayout(tmp_path)
    for name in (
        "2026/07/2026-07-28.csv",
        "2026/07/2026-07-28.jsonl",
        "2026/07/2026-07-27.parquet",
        "2026/07/2026-07-28.manifest.json",
        "2026/07/2026-08-01.jsonl",
        "2026/07/notes.csv",
        "2026/07/.2026-07-28.jsonl.1a2b.tmp",
    ):
        path = layout.daily_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    found = [(day.isoformat(), fmt.value) for day, fmt, _ in layout.artifact_paths()]
    assert found == [
        ("2026-07-27", "parquet"),
        ("2026-07-28", "jsonl"),
        ("2026-07-28", "csv"),
    ]
