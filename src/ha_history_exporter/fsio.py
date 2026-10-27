"""Atomic and durable file-system primitives.

Every file HHE writes goes through this module. A write is durable once the
file content and the directory entry that names it are flushed to stable
storage. On Windows, NTFS journals directory metadata and directory handles
cannot be flushed, so :func:`fsync_dir` is a no-op there. See internal dev doc
§ "Durability".
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

_BINARY = getattr(os, "O_BINARY", 0)


def fsync_file(path: Path) -> None:
    """Flush *path*'s content to stable storage.

    Windows flushes through a writable handle only, so the file is opened for
    update even though nothing is written.
    """
    with path.open("rb+") as handle:
        os.fsync(handle.fileno())


def fsync_dir(path: Path) -> None:
    """Flush the directory entries of *path* (POSIX only)."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_dir(path: Path) -> None:
    """Create *path* and its missing parents, each durably named in its parent."""
    missing = []
    current = path
    while not current.is_dir() and current.parent != current:
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        fsync_dir(directory.parent)


def replace(
    source: Path,
    target: Path,
    *,
    retries: int = 0,
    retry_sleep: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Rename *source* over *target*, retrying while another program locks it.

    Sync clients, virus scanners and indexers briefly lock files on Windows,
    which surfaces as ``PermissionError``.
    """
    for attempt in range(retries + 1):
        try:
            source.replace(target)
        except PermissionError:
            if attempt == retries:
                raise
            logger.warning(
                "%s is locked by another program (attempt %d of %d); retrying in %.1f s.",
                target.name,
                attempt + 1,
                retries + 1,
                retry_sleep,
            )
            sleep(retry_sleep)
        else:
            return


def replace_durable(
    source: Path,
    target: Path,
    *,
    retries: int = 0,
    retry_sleep: float = 0.0,
) -> None:
    """:func:`replace`, then flush both directory entries."""
    replace(source, target, retries=retries, retry_sleep=retry_sleep)
    fsync_dir(target.parent)
    if source.parent != target.parent:
        fsync_dir(source.parent)


def atomic_write(
    path: Path,
    data: bytes,
    *,
    mode: int = 0o666,
    retries: int = 0,
    retry_sleep: float = 0.0,
) -> None:
    """Make *data* the durable content of *path*, all at once or not at all.

    The data goes to a uniquely named sibling first, is flushed, and then
    replaces *path*. *mode* applies to a newly created file and is subject to
    the umask.
    """
    ensure_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        replace(temporary, path, retries=retries, retry_sleep=retry_sleep)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)


def append_durable(path: Path, data: bytes) -> None:
    """Append *data* to *path* and flush it; creates the file when missing."""
    ensure_dir(path.parent)
    created = not path.exists()
    with path.open("ab") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if created:
        fsync_dir(path.parent)


def same_device(first: Path, second: Path) -> bool:
    """True when both existing paths live on the same file system."""
    try:
        return first.stat().st_dev == second.stat().st_dev
    except OSError:
        return False


def is_within(root: Path, path: Path) -> bool:
    """True when *path* lies inside *root*, compared lexically.

    Symbolic links and junctions are not resolved: an output tree may
    deliberately place a subdirectory on another disk.
    """
    base = os.path.normcase(os.path.normpath(root.absolute()))
    candidate = os.path.normcase(os.path.normpath(path.absolute()))
    try:
        return os.path.commonpath([base, candidate]) == base
    except ValueError:
        return False
