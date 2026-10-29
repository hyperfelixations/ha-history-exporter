"""Publishing one day generation: artifacts and manifest through one day transaction."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

from .. import fsio
from ..archive.layout import ArchiveLayout
from ..archive.transaction import DayTransaction
from ..archive.workspace import Workspace
from ..domain.formats import Format
from ..domain.manifest import Manifest14
from ..settings import StorageSettings


def publish_day(
    layout: ArchiveLayout,
    workspace: Workspace,
    day: date,
    artifacts: Mapping[Format, Path],
    manifest: Manifest14,
    *,
    retire: Sequence[Path] = (),
    storage: StorageSettings,
) -> None:
    """Move the prepared *artifacts* and *manifest* into place, or leave the day as it was."""
    manifest_temp = workspace.work_dir / f"{day.isoformat()}.manifest.json"
    fsio.atomic_write(manifest_temp, manifest.encode())
    DayTransaction(
        layout,
        workspace.run_id,
        day,
        artifacts=[(path, layout.day_file(day, fmt.value)) for fmt, path in artifacts.items()],
        manifest=(manifest_temp, layout.manifest_file(day)),
        retire=retire,
        retries=storage.locked_file_retries,
        retry_sleep=storage.locked_file_retry_sleep,
    ).commit()
