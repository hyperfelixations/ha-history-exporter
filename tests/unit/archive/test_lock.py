from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from ha_history_exporter.archive.lock import FileLock, LockHeld, probe

_HOLDER = """
import sys, time
from pathlib import Path
from ha_history_exporter.archive.lock import FileLock
lock = FileLock(Path(sys.argv[1]), "run_id: child\\npid: child\\n")
lock.acquire()
print("locked", flush=True)
time.sleep(120)
"""


@contextmanager
def held_by_another_process(path: Path) -> Iterator[subprocess.Popen[str]]:
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        yield child
    finally:
        child.kill()
        child.wait(timeout=30)


def test_a_lock_is_exclusive_within_one_process(tmp_path: Path) -> None:
    path = tmp_path / "archive.lock"
    with FileLock(path, "run_id: first\n"):
        with pytest.raises(LockHeld) as caught:
            FileLock(path).acquire()
        assert caught.value.holder == "run_id: first"
    with FileLock(path):
        pass


def test_the_holder_text_stays_readable_while_locked(tmp_path: Path) -> None:
    path = tmp_path / "archive.lock"
    with FileLock(path, "run_id: abc\npid: 1\n"):
        assert path.read_text(encoding="utf-8") == "run_id: abc\npid: 1\n"
        assert probe(path).held
        assert probe(path).holder == "run_id: abc\npid: 1"


def test_the_lock_file_remains_after_release(tmp_path: Path) -> None:
    path = tmp_path / "archive.lock"
    lock = FileLock(path, "x")
    lock.acquire()
    lock.release()
    lock.release()
    assert path.is_file()
    assert not lock.held
    assert not probe(path).held


def test_probe_creates_nothing(tmp_path: Path) -> None:
    path = tmp_path / "absent.lock"
    state = probe(path)
    assert (state.held, state.holder) == (False, None)
    assert not path.exists()


def test_a_lock_held_by_another_process_is_refused_and_named(tmp_path: Path) -> None:
    path = tmp_path / "archive.lock"
    with held_by_another_process(path):
        assert probe(path).held
        with pytest.raises(LockHeld) as caught:
            FileLock(path).acquire()
        assert caught.value.holder == "run_id: child\npid: child"


def test_a_killed_holder_releases_the_lock(tmp_path: Path) -> None:
    """Windows releases the locks of a terminated process asynchronously."""
    path = tmp_path / "archive.lock"
    with held_by_another_process(path) as child:
        child.kill()
        child.wait(timeout=30)
        lock = FileLock(path, "run_id: next\n")
        deadline = time.monotonic() + 10
        while True:
            try:
                lock.acquire()
            except LockHeld:
                assert time.monotonic() < deadline, "the lock outlived its holder"
                time.sleep(0.05)
            else:
                break
        try:
            assert path.read_text(encoding="utf-8") == "run_id: next\n"
        finally:
            lock.release()


def test_a_lock_file_replaced_while_locking_is_locked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "archive.lock"
    answers = iter([False, True])
    monkeypatch.setattr(FileLock, "_is_current_file", lambda self, descriptor: next(answers))
    with FileLock(path, "run_id: retried\n") as lock:
        assert lock.held
        assert path.read_text(encoding="utf-8") == "run_id: retried\n"


def test_a_lock_file_that_keeps_changing_counts_as_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "archive.lock"
    monkeypatch.setattr(FileLock, "_is_current_file", lambda self, descriptor: False)
    with pytest.raises(LockHeld):
        FileLock(path).acquire()
    assert not probe(path).held
