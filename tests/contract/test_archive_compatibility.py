"""Existing archives stay readable.

The fixtures mirror manifests written by earlier releases on Windows: schema
1.1 to 1.3, two-space indentation, CRLF line terminators. A successful legacy
manifest keeps counting as an exported day.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from tests.support.harness import stored_status

FIXTURES = Path(__file__).parent / "fixtures" / "legacy_manifests"
DAY = date(2026, 7, 28)


def _archive_with(tmp_path: Path, schema: str) -> Path:
    output = tmp_path / "archive"
    target = output / "exports" / "daily" / "2026" / "07"
    target.mkdir(parents=True)
    shutil.copy2(FIXTURES / f"schema-{schema}" / f"{DAY}.manifest.json", target)
    return output


@pytest.mark.parametrize("schema", ["1.1", "1.2", "1.3"])
def test_legacy_successful_manifest_counts_as_exported(tmp_path: Path, schema: str) -> None:
    assert stored_status(_archive_with(tmp_path, schema), DAY) == "ok"


@pytest.mark.parametrize("schema", ["1.1", "1.2", "1.3"])
def test_legacy_fixtures_use_windows_line_terminators(schema: str) -> None:
    raw = (FIXTURES / f"schema-{schema}" / f"{DAY}.manifest.json").read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_a_day_without_manifest_is_not_exported(tmp_path: Path) -> None:
    output = tmp_path / "archive"
    output.mkdir()
    assert stored_status(output, DAY) is None
