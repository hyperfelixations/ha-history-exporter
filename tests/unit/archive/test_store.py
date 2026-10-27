from __future__ import annotations

from pathlib import Path

import pytest

from ha_history_exporter.archive.store import load_manifest
from ha_history_exporter.domain.manifest import ManifestStatus
from ha_history_exporter.errors import ExportError
from ha_history_exporter.layout import ExportLayout
from tests.support.manifests import DAY, identity, manifest14, write_manifest


def test_a_day_without_manifest_has_none(tmp_path: Path) -> None:
    assert load_manifest(ExportLayout(tmp_path), identity()) is None


def test_a_stored_manifest_is_parsed(tmp_path: Path) -> None:
    write_manifest(tmp_path, manifest14())
    loaded = load_manifest(ExportLayout(tmp_path), identity())
    assert loaded is not None
    assert loaded.status is ManifestStatus.OK


def test_a_corrupt_manifest_is_an_error_not_a_missing_day(tmp_path: Path) -> None:
    path = write_manifest(tmp_path, manifest14())
    path.write_bytes(b"{broken")
    with pytest.raises(ExportError, match="not valid JSON"):
        load_manifest(ExportLayout(tmp_path), identity())


def test_an_unreadable_manifest_is_an_error(tmp_path: Path) -> None:
    path = write_manifest(tmp_path, manifest14())
    path.unlink()
    path.mkdir()
    with pytest.raises(ExportError, match=f"Cannot read the manifest for {DAY}"):
        load_manifest(ExportLayout(tmp_path), identity())


def test_a_file_where_the_month_directory_belongs_means_no_manifest(tmp_path: Path) -> None:
    (tmp_path / "exports" / "daily" / "2026").mkdir(parents=True)
    (tmp_path / "exports" / "daily" / "2026" / "07").write_bytes(b"")
    assert load_manifest(ExportLayout(tmp_path), identity()) is None
