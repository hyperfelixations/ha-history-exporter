"""Run-isolated working directories, locking, and final promotion.

Every run owns one working directory under the temporary root and, when the
temporary root sits on a different filesystem than the output, one staging
directory next to the output. A parallel run therefore cannot delete another
run's files, and the last step before a file becomes visible is always an
``os.replace`` within a single filesystem, which is atomic.

See internal dev doc, section "Laufzeit-Workspace".
"""

from __future__ import annotations

import logging
import os
import random
import shutil
import socket
import string
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..errors import ExportError, Remedy

if TYPE_CHECKING:  # pragma: no cover
    from ..settings import AppConfig

logger = logging.getLogger(__name__)

RUN_PREFIX = "run-"
STAGING_DIRNAME = ".hhe-staging"
LOCK_FILENAME = ".hhe.lock"

#: A working directory or lock older than this is considered abandoned.
STALE_AFTER_HOURS = 48


def new_run_id() -> str:
    """A run identifier that is unique across processes and sub-second runs."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(random.choices(alphabet, k=6))  # noqa: S311 - uniqueness, not secrecy
    return f"{stamp}-{os.getpid()}-{suffix}"


@dataclass
class Workspace:
    """The filesystem context of exactly one export run."""

    run_id: str
    work_dir: Path
    output_dir: Path
    lock_path: Path

    @property
    def staging_dir(self) -> Path:
        return self.output_dir / STAGING_DIRNAME / self.run_id

    def promote(
        self,
        src: Path,
        dst: Path,
        cloud_storage_retry_count: int = 5,
        cloud_storage_retry_sleep: float = 2.0,
    ) -> None:
        """Make a validated file visible at *dst*.

        When the working directory and the destination share a filesystem the
        file is replaced directly. Otherwise it is copied into the staging
        directory next to the destination first, so the visible step stays a
        same-filesystem replace.
        """
        from ..writers import atomic_replace

        dst.parent.mkdir(parents=True, exist_ok=True)
        if same_filesystem(src.parent, dst.parent):
            atomic_replace(src, dst, cloud_storage_retry_count, cloud_storage_retry_sleep)
            return

        self.staging_dir.mkdir(parents=True, exist_ok=True)
        staged = self.staging_dir / src.name
        shutil.copy2(src, staged)
        atomic_replace(staged, dst, cloud_storage_retry_count, cloud_storage_retry_sleep)
        src.unlink(missing_ok=True)

    def close(self) -> None:
        """Remove this run's directories and release the lock."""
        shutil.rmtree(self.work_dir, ignore_errors=True)
        shutil.rmtree(self.staging_dir, ignore_errors=True)
        staging_root = self.output_dir / STAGING_DIRNAME
        if staging_root.is_dir() and not any(staging_root.iterdir()):
            staging_root.rmdir()
        _release_lock(self.lock_path, self.run_id)


def same_filesystem(first: Path, second: Path) -> bool:
    """True when both paths live on the same device."""
    try:
        return first.stat().st_dev == second.stat().st_dev
    except OSError:
        return False


def open_workspace(cfg: AppConfig) -> Workspace:
    """Create the working directory for one run and take the output lock."""
    temp_root = cfg.resolved_temp_dir
    output_dir = Path(cfg.export.output_dir)
    temp_root.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    removed = cleanup_stale(temp_root)
    if removed:
        logger.info("Removed %d abandoned working director(ies).", removed)

    run_id = new_run_id()
    lock_path = output_dir / LOCK_FILENAME
    _acquire_lock(lock_path, run_id)

    work_dir = temp_root / f"{RUN_PREFIX}{run_id}"
    work_dir.mkdir(parents=True, exist_ok=False)
    logger.debug("Run %s working directory: %s", run_id, work_dir)
    return Workspace(
        run_id=run_id,
        work_dir=work_dir,
        output_dir=output_dir,
        lock_path=lock_path,
    )


def cleanup_stale(temp_root: Path, max_age_hours: int = STALE_AFTER_HOURS) -> int:
    """Remove abandoned run directories. Only run directories are touched."""
    if not temp_root.is_dir():
        return 0

    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for candidate in temp_root.glob(f"{RUN_PREFIX}*"):
        if not candidate.is_dir():
            continue
        try:
            if candidate.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(candidate, ignore_errors=True)
            removed += 1
        except OSError:  # pragma: no cover - racing with another cleanup
            continue
    return removed


# ── locking ───────────────────────────────────────────────────────────────────

def _acquire_lock(lock_path: Path, run_id: str) -> None:
    payload = (
        f"run_id: {run_id}\n"
        f"pid: {os.getpid()}\n"
        f"host: {socket.gethostname()}\n"
        f"started_at: {datetime.now(timezone.utc).isoformat()}\n"
    )
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if _is_stale(lock_path):
            logger.warning(
                "Taking over an abandoned lock at %s (older than %d hours).",
                lock_path,
                STALE_AFTER_HOURS,
            )
            lock_path.unlink(missing_ok=True)
            _acquire_lock(lock_path, run_id)
            return
        raise _locked(lock_path) from None

    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)


def _release_lock(lock_path: Path, run_id: str) -> None:
    try:
        if lock_path.is_file() and f"run_id: {run_id}" in lock_path.read_text(
            encoding="utf-8"
        ):
            lock_path.unlink()
    except OSError:  # pragma: no cover - the run is finished either way
        logger.debug("Could not remove the lock at %s.", lock_path)


def _is_stale(lock_path: Path) -> bool:
    try:
        age_hours = (time.time() - lock_path.stat().st_mtime) / 3600
    except OSError:  # pragma: no cover - vanished between checks
        return True
    return age_hours >= STALE_AFTER_HOURS


def _locked(lock_path: Path) -> ExportError:
    try:
        holder = lock_path.read_text(encoding="utf-8").strip().replace("\n", ", ")
    except OSError:  # pragma: no cover - vanished between checks
        holder = "unknown"
    return ExportError(
        "Another export is already running for this output directory.",
        details=(
            "Only one run may write an output directory at a time, so two "
            "processes cannot corrupt each other's files. "
            f"Lock holder: {holder}."
        ),
        remedies=(
            Remedy("Wait for the other run to finish, then start again."),
            Remedy(
                "If no export is running any more, remove the stale lock:",
                f"del {lock_path}" if os.name == "nt" else f"rm {lock_path}",
            ),
        ),
    )
