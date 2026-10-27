from __future__ import annotations

import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from ha_history_exporter.archive.lock import FileLock
from ha_history_exporter.archive.workspace import (
    ORPHAN_AFTER,
    RUN_LOCK,
    RUN_PREFIX,
    cleanup_stale,
    new_run_id,
    open_workspace,
)

NOW = datetime(2026, 10, 16, 8, 44, 6, tzinfo=UTC)


def test_run_ids_name_the_time_and_process_and_are_unique() -> None:
    ids = {new_run_id(NOW) for _ in range(50)}
    assert len(ids) == 50
    assert all(re.fullmatch(rf"20261016T084406-{os.getpid()}-[a-z0-9]{{6}}", i) for i in ids)


def test_a_workspace_owns_a_locked_directory_until_closed(tmp_path: Path) -> None:
    workspace = open_workspace(tmp_path / "temp", "run-a")
    assert workspace.work_dir == tmp_path / "temp" / f"{RUN_PREFIX}run-a"
    assert workspace.lock.held
    assert (workspace.work_dir / RUN_LOCK).is_file()
    workspace.close()
    assert not workspace.work_dir.exists()


def test_cleanup_never_touches_a_live_run(tmp_path: Path) -> None:
    temp = tmp_path / "temp"
    live = open_workspace(temp, "live")
    old = time.time() - 10 * ORPHAN_AFTER
    os.utime(live.work_dir, (old, old))
    try:
        assert cleanup_stale(temp) == 0
        assert live.work_dir.is_dir()
    finally:
        live.close()


def test_cleanup_removes_a_finished_run_at_once(tmp_path: Path) -> None:
    temp = tmp_path / "temp"
    finished = temp / f"{RUN_PREFIX}finished"
    finished.mkdir(parents=True)
    with FileLock(finished / RUN_LOCK):
        pass
    (finished / "leftover.jsonl").write_text("x", encoding="utf-8")
    assert cleanup_stale(temp) == 1
    assert not finished.exists()


def test_a_run_directory_without_lock_is_removed_only_when_old(tmp_path: Path) -> None:
    temp = tmp_path / "temp"
    orphan = temp / f"{RUN_PREFIX}orphan"
    orphan.mkdir(parents=True)
    assert cleanup_stale(temp) == 0
    old = time.time() - ORPHAN_AFTER - 60
    os.utime(orphan, (old, old))
    assert cleanup_stale(temp) == 1
    assert not orphan.exists()


def test_cleanup_only_touches_run_directories(tmp_path: Path) -> None:
    temp = tmp_path / "temp"
    foreign_dir = temp / "someone-elses-directory"
    foreign_dir.mkdir(parents=True)
    foreign_file = temp / f"{RUN_PREFIX}looks-like-a-run.txt"
    foreign_file.write_text("x", encoding="utf-8")
    old = time.time() - 10 * ORPHAN_AFTER
    for path in (foreign_dir, foreign_file):
        os.utime(path, (old, old))
    assert cleanup_stale(temp) == 0
    assert foreign_dir.is_dir()
    assert foreign_file.is_file()


def test_cleanup_on_a_missing_root_is_harmless(tmp_path: Path) -> None:
    assert cleanup_stale(tmp_path / "absent") == 0
