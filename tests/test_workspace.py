"""Tests for run isolation: working directories, locking, and promotion."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from ha_history_exporter.errors import ExportError
from ha_history_exporter.runtime import workspace as ws
from tests.helpers import make_config


def config(root: Path):
    return make_config(root)


# ── run identity ──────────────────────────────────────────────────────────────

def test_run_ids_are_unique_within_the_same_second():
    ids = {ws.new_run_id() for _ in range(50)}
    assert len(ids) == 50


def test_workspace_creates_its_own_working_directory(tmp_path):
    cfg = config(tmp_path)
    workspace = ws.open_workspace(cfg)
    try:
        assert workspace.work_dir.is_dir()
        assert workspace.work_dir.name.startswith(ws.RUN_PREFIX)
        assert workspace.work_dir.parent == cfg.resolved_temp_dir
    finally:
        workspace.close()

    assert not workspace.work_dir.exists()


def test_two_runs_on_different_outputs_do_not_interfere(tmp_path):
    first = ws.open_workspace(config(tmp_path / "a"))
    second = ws.open_workspace(config(tmp_path / "b"))
    try:
        (first.work_dir / "own.jsonl").write_text("first", encoding="utf-8")
        (second.work_dir / "own.jsonl").write_text("second", encoding="utf-8")

        assert (first.work_dir / "own.jsonl").read_text(encoding="utf-8") == "first"
        assert (second.work_dir / "own.jsonl").read_text(encoding="utf-8") == "second"
    finally:
        first.close()
        second.close()


# ── locking ───────────────────────────────────────────────────────────────────

def test_second_run_on_the_same_output_is_refused(tmp_path):
    cfg = config(tmp_path)
    first = ws.open_workspace(cfg)
    try:
        with pytest.raises(ExportError) as exc:
            ws.open_workspace(config(tmp_path))
        assert "Another export is already running" in exc.value.summary
        assert any(remedy.command for remedy in exc.value.remedies)
    finally:
        first.close()

    # Once released, the next run may proceed.
    again = ws.open_workspace(config(tmp_path))
    again.close()


def test_lock_is_released_even_after_a_failure(tmp_path):
    cfg = config(tmp_path)
    workspace = ws.open_workspace(cfg)
    assert workspace.lock_path.is_file()
    workspace.close()
    assert not workspace.lock_path.is_file()


def test_a_foreign_lock_is_not_removed_by_close(tmp_path):
    cfg = config(tmp_path)
    workspace = ws.open_workspace(cfg)
    workspace.lock_path.write_text("run_id: someone-else\n", encoding="utf-8")

    workspace.close()

    assert workspace.lock_path.is_file()


def test_an_abandoned_lock_is_taken_over(tmp_path):
    cfg = config(tmp_path)
    first = ws.open_workspace(cfg)
    lock_path = first.lock_path
    # Simulate a process that died long ago without releasing the lock.
    old = time.time() - (ws.STALE_AFTER_HOURS + 1) * 3600
    os.utime(lock_path, (old, old))

    second = ws.open_workspace(config(tmp_path))
    try:
        assert second.lock_path.is_file()
        assert f"run_id: {second.run_id}" in lock_path.read_text(encoding="utf-8")
    finally:
        second.close()


# ── stale cleanup ─────────────────────────────────────────────────────────────

def test_recent_run_directories_survive_cleanup(tmp_path):
    temp_root = tmp_path / "temp"
    recent = temp_root / f"{ws.RUN_PREFIX}recent"
    recent.mkdir(parents=True)

    assert ws.cleanup_stale(temp_root) == 0
    assert recent.is_dir()


def test_abandoned_run_directories_are_removed(tmp_path):
    temp_root = tmp_path / "temp"
    abandoned = temp_root / f"{ws.RUN_PREFIX}abandoned"
    abandoned.mkdir(parents=True)
    (abandoned / "leftover.jsonl").write_text("x", encoding="utf-8")
    old = time.time() - (ws.STALE_AFTER_HOURS + 1) * 3600
    os.utime(abandoned, (old, old))

    assert ws.cleanup_stale(temp_root) == 1
    assert not abandoned.exists()


def test_cleanup_only_touches_run_directories(tmp_path):
    temp_root = tmp_path / "temp"
    temp_root.mkdir(parents=True)
    foreign_dir = temp_root / "someone-elses-directory"
    foreign_dir.mkdir()
    foreign_file = temp_root / "old.tmp"
    foreign_file.write_text("x", encoding="utf-8")
    old = time.time() - (ws.STALE_AFTER_HOURS + 1) * 3600
    os.utime(foreign_dir, (old, old))
    os.utime(foreign_file, (old, old))

    assert ws.cleanup_stale(temp_root) == 0
    assert foreign_dir.is_dir()
    assert foreign_file.is_file()


def test_cleanup_on_a_missing_root_is_harmless(tmp_path):
    assert ws.cleanup_stale(tmp_path / "absent") == 0


# ── promotion ─────────────────────────────────────────────────────────────────

def test_promote_moves_the_file_to_its_final_path(tmp_path):
    cfg = config(tmp_path)
    workspace = ws.open_workspace(cfg)
    try:
        source = workspace.work_dir / "2026-07-28.jsonl"
        source.write_text("payload", encoding="utf-8")
        target = cfg.layout.day_file("2026-07-28", "jsonl")

        workspace.promote(source, target, 0, 0)

        assert target.read_text(encoding="utf-8") == "payload"
        assert not source.exists()
    finally:
        workspace.close()


def test_promote_stages_on_the_target_filesystem_when_devices_differ(
    tmp_path, monkeypatch
):
    """A cross-device move is copied into staging first, then replaced."""
    cfg = config(tmp_path)
    workspace = ws.open_workspace(cfg)
    try:
        monkeypatch.setattr(ws, "same_filesystem", lambda a, b: False)
        source = workspace.work_dir / "2026-07-28.jsonl"
        source.write_text("payload", encoding="utf-8")
        target = cfg.layout.day_file("2026-07-28", "jsonl")

        workspace.promote(source, target, 0, 0)

        assert target.read_text(encoding="utf-8") == "payload"
        assert not source.exists()
        assert workspace.staging_dir.parent.name == ws.STAGING_DIRNAME
    finally:
        workspace.close()

    assert not (Path(cfg.export.output_dir) / ws.STAGING_DIRNAME).exists()


def test_staging_directory_lives_outside_the_daily_export_tree(tmp_path):
    """Analysis globs over exports/daily must never see staging files."""
    cfg = config(tmp_path)
    workspace = ws.open_workspace(cfg)
    try:
        assert ws.STAGING_DIRNAME not in str(cfg.layout.daily_root)
        assert workspace.staging_dir.is_relative_to(Path(cfg.export.output_dir))
        assert not workspace.staging_dir.is_relative_to(cfg.layout.daily_root)
    finally:
        workspace.close()


def test_same_filesystem_reports_false_for_a_missing_path(tmp_path):
    assert ws.same_filesystem(tmp_path, tmp_path / "absent") is False
