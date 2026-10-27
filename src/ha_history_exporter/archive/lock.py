"""Exclusive file locks that the operating system releases with the process.

POSIX uses ``fcntl.flock``; Windows uses ``msvcrt.locking`` on one byte far
beyond the file content, so the diagnostic text (run, PID, host, start) stays
readable for another process while the lock is held. A crashed run therefore
never blocks the next one, and no age heuristic is involved. The lock file
itself is never deleted. Locks on network file systems (SMB, NFS) depend on
the server and are not guaranteed. See internal dev doc § "Archive lock".
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

_BINARY = getattr(os, "O_BINARY", 0)
#: Windows byte-range lock position: beyond any content, below 2**31 for old CRTs.
_WINDOWS_LOCK_OFFSET = 0x7FFF_0000
_ATTEMPTS = 5


class LockHeld(Exception):
    """Another process holds the lock."""

    def __init__(self, holder: str | None) -> None:
        super().__init__(holder or "unknown holder")
        self.holder = holder


if sys.platform == "win32":
    import msvcrt

    def _try_lock(descriptor: int) -> bool:
        os.lseek(descriptor, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(descriptor: int) -> None:
        os.lseek(descriptor, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(descriptor: int) -> bool:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


def read_holder(path: Path) -> str | None:
    """The diagnostic text of a lock file, or None when it has none."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


class FileLock:
    """An exclusive, non-blocking lock on *path*, created on demand."""

    def __init__(self, path: Path, holder: str = "") -> None:
        self.path = path
        self._holder = holder
        self._descriptor: int | None = None

    def acquire(self) -> None:
        """Take the lock or raise :class:`LockHeld`; write the holder text."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(_ATTEMPTS):
            descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | _BINARY, 0o644)
            if not _try_lock(descriptor):
                os.close(descriptor)
                raise LockHeld(read_holder(self.path))
            if self._is_current_file(descriptor):
                self._write_holder(descriptor)
                self._descriptor = descriptor
                return
            # The file was replaced or removed between open and lock.
            _unlock(descriptor)
            os.close(descriptor)
        raise LockHeld(read_holder(self.path))

    def _is_current_file(self, descriptor: int) -> bool:
        try:
            current = self.path.stat()
        except FileNotFoundError:
            return False
        opened = os.fstat(descriptor)
        return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)

    def _write_holder(self, descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, self._holder.encode("utf-8"))

    def release(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        try:
            _unlock(descriptor)
        finally:
            os.close(descriptor)

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


@dataclass(frozen=True)
class LockState:
    held: bool
    holder: str | None


def probe(path: Path) -> LockState:
    """Whether another process holds the lock on *path*; creates nothing."""
    try:
        descriptor = os.open(path, os.O_RDONLY | _BINARY)
    except FileNotFoundError:
        return LockState(held=False, holder=None)
    try:
        if not _try_lock(descriptor):
            return LockState(held=True, holder=read_holder(path))
        _unlock(descriptor)
        return LockState(held=False, holder=read_holder(path))
    finally:
        os.close(descriptor)
