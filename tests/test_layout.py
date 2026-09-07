"""The export directory layout, stated literally.

Every path here was written into existing archives and is globbed by the
analysis tooling. This module spells the expected strings out rather than
recomputing them, so a change to the construction logic cannot make the test
agree with itself.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path

import pytest

from ha_history_exporter.layout import ExportLayout

ROOT = Path("archive") / "ha-history-exports"


@pytest.fixture
def layout(tmp_path: Path) -> ExportLayout:
    return ExportLayout(tmp_path)


def relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


# ── fixed directories ─────────────────────────────────────────────────────────

def test_top_level_directories_are_unchanged(layout: ExportLayout, tmp_path: Path):
    assert relative(layout.daily_root, tmp_path) == "exports/daily"
    assert relative(layout.metadata_dir, tmp_path) == "metadata"
    assert relative(layout.logs_dir, tmp_path) == "logs"


def test_the_layout_creates_nothing(tmp_path: Path):
    """Resolving a path must not touch the file system."""
    root = tmp_path / "untouched"
    root.mkdir()
    layout = ExportLayout(root)

    layout.day_file(date(2026, 6, 15), "jsonl")
    layout.day_dir("2026-06-15")
    assert layout.metadata_dir.name == "metadata"
    assert layout.logs_dir.name == "logs"

    assert list(root.iterdir()) == []


# ── day directories and files ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 1, 1), "exports/daily/2026/01"),
        (date(2026, 6, 15), "exports/daily/2026/06"),
        (date(2026, 9, 30), "exports/daily/2026/09"),
        (date(2026, 10, 1), "exports/daily/2026/10"),
        (date(2026, 12, 31), "exports/daily/2026/12"),
        (date(2027, 2, 28), "exports/daily/2027/02"),
    ],
)
def test_day_directories_are_year_then_zero_padded_month(
    layout: ExportLayout, tmp_path: Path, day: date, expected: str
):
    assert relative(layout.day_dir(day), tmp_path) == expected


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        ("jsonl", "exports/daily/2026/06/2026-06-15.jsonl"),
        ("csv", "exports/daily/2026/06/2026-06-15.csv"),
        ("parquet", "exports/daily/2026/06/2026-06-15.parquet"),
        ("manifest.json", "exports/daily/2026/06/2026-06-15.manifest.json"),
    ],
)
def test_day_files_carry_the_iso_date_and_the_suffix(
    layout: ExportLayout, tmp_path: Path, suffix: str, expected: str
):
    path = layout.day_file(date(2026, 6, 15), suffix)
    assert relative(path, tmp_path) == expected


def test_iso_strings_and_date_objects_resolve_identically(layout: ExportLayout):
    assert layout.day_file("2026-06-15", "jsonl") == layout.day_file(
        date(2026, 6, 15), "jsonl"
    )
    assert layout.day_dir("2026-06-15") == layout.day_dir(date(2026, 6, 15))


def test_an_unparsable_day_string_is_rejected(layout: ExportLayout):
    with pytest.raises(ValueError):
        layout.day_file("15.06.2026", "jsonl")


# ── behaviour as a value object ───────────────────────────────────────────────

def test_the_layout_is_immutable():
    """A run must not be able to move its own output directory mid-flight."""
    layout = ExportLayout(ROOT)
    with pytest.raises(FrozenInstanceError):
        layout.root = ROOT / "elsewhere"  # type: ignore[misc]


def test_two_layouts_on_the_same_root_are_equal():
    assert ExportLayout(ROOT) == ExportLayout(ROOT)
    assert ExportLayout(ROOT) != ExportLayout(ROOT / "other")
