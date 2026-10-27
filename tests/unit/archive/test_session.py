from __future__ import annotations

from pathlib import Path

import pytest

from ha_history_exporter.archive.layout import ArchiveLayout
from ha_history_exporter.archive.lock import probe
from ha_history_exporter.archive.session import ArchiveSession
from ha_history_exporter.errors import ExportError


def session(tmp_path: Path, run_id: str) -> ArchiveSession:
    return ArchiveSession(
        ArchiveLayout(tmp_path / "output"),
        tmp_path / "temp",
        run_id,
        holder=f"run_id: {run_id}\n",
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
