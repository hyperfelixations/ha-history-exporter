from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.archive.layout import ArchiveLayout
from ha_history_exporter.archive.lock import probe
from ha_history_exporter.archive.session import ArchiveSession
from ha_history_exporter.archive.transaction import Action, Entry, Journal, Phase
from ha_history_exporter.errors import ConfigError, ExportError
from ha_history_exporter.fsio import atomic_write

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 27, 20, tzinfo=UTC)


def session(tmp_path: Path, run_id: str, *, configured: ZoneInfo | None = None) -> ArchiveSession:
    return ArchiveSession(
        ArchiveLayout(tmp_path / "output"),
        tmp_path / "temp",
        run_id,
        holder=f"run_id: {run_id}\n",
        configured_zone=configured,
        home_assistant_zone=BERLIN,
        now=NOW,
    )


def test_a_session_locks_the_archive_and_owns_a_workspace(tmp_path: Path) -> None:
    layout = ArchiveLayout(tmp_path / "output")
    with session(tmp_path, "first") as opened:
        assert probe(layout.lock_file).held
        work_dir = opened.workspace.work_dir
        assert work_dir.is_dir()
        layout.staging_dir("first").mkdir(parents=True)
    assert not probe(layout.lock_file).held
    with pytest.raises(RuntimeError, match="not open"):
        _ = opened.workspace
    assert not work_dir.exists()
    assert not layout.staging_root.exists()
    assert list((tmp_path / "temp").iterdir()) == []


def test_a_second_session_on_the_same_archive_is_refused(tmp_path: Path) -> None:
    with session(tmp_path, "first"):
        with (
            pytest.raises(ExportError, match="Another export is already running") as caught,
            session(tmp_path, "second"),
        ):
            pass
        assert "run_id: first" in (caught.value.details or "")
    with session(tmp_path, "third"):
        pass


def test_sessions_on_different_archives_do_not_interfere(tmp_path: Path) -> None:
    with session(tmp_path / "a", "a") as first, session(tmp_path / "b", "b") as second:
        assert first.workspace.work_dir != second.workspace.work_dir


def test_a_failure_while_opening_releases_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ha_history_exporter.archive.session as module

    def broken(*_args: object) -> int:
        raise ExportError("An export transaction journal cannot be read safely.")

    monkeypatch.setattr(module, "recover", broken)
    with pytest.raises(ExportError, match="journal"), session(tmp_path, "run"):
        pass
    assert not probe(ArchiveLayout(tmp_path / "output").lock_file).held


def test_a_session_records_the_archive_zone_once(tmp_path: Path) -> None:
    layout = ArchiveLayout(tmp_path / "output")
    with session(tmp_path, "first") as opened:
        assert opened.descriptor.timezone == BERLIN
    created = layout.descriptor_file.read_bytes()
    with session(tmp_path, "second", configured=BERLIN) as opened:
        assert opened.descriptor.timezone == BERLIN
    assert layout.descriptor_file.read_bytes() == created


def test_a_zone_change_is_one_clear_error_and_changes_nothing(tmp_path: Path) -> None:
    layout = ArchiveLayout(tmp_path / "output")
    with session(tmp_path, "first"):
        pass
    created = layout.descriptor_file.read_bytes()
    with (
        pytest.raises(ConfigError, match="this archive's days are in Europe/Berlin"),
        session(tmp_path, "second", configured=ZoneInfo("America/New_York")),
    ):
        pass
    assert layout.descriptor_file.read_bytes() == created
    assert not probe(layout.lock_file).held


def test_a_newer_archive_is_refused_before_recovery_touches_it(tmp_path: Path) -> None:
    layout = ArchiveLayout(tmp_path / "output")
    layout.root.mkdir(parents=True)
    layout.descriptor_file.write_text('{"format_version": 2}', encoding="utf-8")
    journal = Journal("old", "2026-07-28", Phase.PREPARED, (Entry(Action.REMOVE, "x", "y", True),))
    journal_path = layout.journal_root / "old-2026-07-28.json"
    atomic_write(journal_path, journal.encode())
    with pytest.raises(ExportError, match="newer version"), session(tmp_path, "run"):
        pass
    assert journal_path.is_file()
