"""Exclusive, recovered access to one archive for one run.

Opening a session takes the archive lock, refuses an archive of a newer
format, finishes interrupted day transactions, establishes the archive's
time zone, removes the working directories of finished runs and creates this
run's working directory; closing it reverses that. See internal dev doc
§ "Archive session".
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self
from zoneinfo import ZoneInfo

from .. import fsio
from ..errors import ExportError, Remedy
from . import descriptor
from .descriptor import ArchiveDescriptor
from .layout import ArchiveLayout
from .lock import FileLock, LockHeld
from .transaction import recover
from .workspace import Workspace, cleanup_stale, open_workspace

logger = logging.getLogger(__name__)


def archive_locked(lock_file: Path, holder: str | None) -> ExportError:
    return ExportError(
        "Another export is already running for this output directory.",
        details=(
            "Only one run may write an output directory at a time. "
            f"Lock holder: {holder.replace(chr(10), ', ') if holder else 'unknown'}."
        ),
        remedies=(Remedy("Wait for the other run to finish, then start again."),),
        context={"output_dir": str(lock_file.parent)},
    )


class ArchiveSession:
    def __init__(
        self,
        layout: ArchiveLayout,
        temp_root: Path,
        run_id: str,
        *,
        holder: str,
        configured_zone: ZoneInfo | None,
        home_assistant_zone: ZoneInfo,
        now: datetime,
        retries: int = 0,
        retry_sleep: float = 0.0,
    ) -> None:
        self.layout = layout
        self.temp_root = temp_root
        self.run_id = run_id
        self.retries = retries
        self.retry_sleep = retry_sleep
        self._zones = (configured_zone, home_assistant_zone)
        self._now = now
        self._lock = FileLock(layout.lock_file, holder)
        self._workspace: Workspace | None = None
        self._descriptor: ArchiveDescriptor | None = None

    @property
    def workspace(self) -> Workspace:
        if self._workspace is None:
            raise RuntimeError("The archive session is not open.")
        return self._workspace

    @property
    def descriptor(self) -> ArchiveDescriptor:
        if self._descriptor is None:
            raise RuntimeError("The archive session is not open.")
        return self._descriptor

    def __enter__(self) -> Self:
        fsio.ensure_dir(self.layout.root)
        try:
            self._lock.acquire()
        except LockHeld as exc:
            raise archive_locked(self.layout.lock_file, exc.holder) from None
        try:
            recorded = descriptor.load(self.layout)
            recovered = recover(self.layout, self.retries, self.retry_sleep)
            if recovered:
                logger.warning("Recovered %d interrupted day transaction(s).", recovered)
            configured, home_assistant = self._zones
            self._descriptor = descriptor.establish(
                self.layout,
                recorded,
                configured=configured,
                home_assistant=home_assistant,
                now=self._now,
            )
            removed = cleanup_stale(self.temp_root)
            if removed:
                logger.info("Removed %d finished working director(ies).", removed)
            self._workspace = open_workspace(self.temp_root, self.run_id)
        except BaseException:
            self._lock.release()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if self._workspace is not None:
                self._workspace.close()
                self._workspace = None
            shutil.rmtree(self.layout.staging_dir(self.run_id), ignore_errors=True)
            staging_root = self.layout.staging_root
            if staging_root.is_dir() and not any(staging_root.iterdir()):
                staging_root.rmdir()
        finally:
            self._lock.release()
