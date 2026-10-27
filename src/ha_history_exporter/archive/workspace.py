"""Per-run working directories below the temporary root.

Each run writes into ``<temp>/run-<run id>/`` and holds a lock on a file
inside it for as long as it lives. A later run removes another run's
directory only after taking that lock, so a live run is never touched, no
matter how long it takes. A directory without a lock file is removed once it
is older than :data:`ORPHAN_AFTER`. See internal dev doc § "Run workspace".
"""

from __future__ import annotations

import os
import secrets
import shutil
import string
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import fsio
from .lock import FileLock, LockHeld

RUN_PREFIX = "run-"
RUN_LOCK = ".run.lock"
ORPHAN_AFTER = 24 * 3600


def new_run_id(now_utc: datetime) -> str:
    """``<UTC YYYYMMDDTHHMMSS>-<pid>-<six lowercase letters or digits>``."""
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(6))
    return f"{now_utc:%Y%m%dT%H%M%S}-{os.getpid()}-{suffix}"


@dataclass
class Workspace:
    run_id: str
    work_dir: Path
    lock: FileLock

    def close(self) -> None:
        self.lock.release()
        shutil.rmtree(self.work_dir, ignore_errors=True)


def open_workspace(temp_root: Path, run_id: str) -> Workspace:
    work_dir = temp_root / f"{RUN_PREFIX}{run_id}"
    fsio.create_dir(work_dir)
    lock = FileLock(work_dir / RUN_LOCK, f"run_id: {run_id}\npid: {os.getpid()}\n")
    try:
        lock.acquire()
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    return Workspace(run_id=run_id, work_dir=work_dir, lock=lock)


def cleanup_stale(temp_root: Path) -> int:
    """Remove the directories of finished or crashed runs; return how many."""
    if not temp_root.is_dir():
        return 0
    cutoff = time.time() - ORPHAN_AFTER
    removed = 0
    for candidate in sorted(temp_root.glob(f"{RUN_PREFIX}*")):
        if not candidate.is_dir():
            continue
        lock_file = candidate / RUN_LOCK
        if lock_file.exists():
            lock = FileLock(lock_file)
            try:
                lock.acquire()
            except LockHeld:
                continue
            lock.release()
        else:
            try:
                if candidate.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
        shutil.rmtree(candidate, ignore_errors=True)
        removed += 1
    return removed
