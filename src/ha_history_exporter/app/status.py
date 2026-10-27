"""Which days an archive holds, month by month, without the lock.

Without a selection, the covered range runs from the first to the last day
with a manifest; every day between them without one is missing. See internal
dev doc § "Commands".
"""

from __future__ import annotations

from datetime import date, timedelta

from ..archive.layout import ArchiveLayout
from ..archive.reader import ArchiveReader
from ..domain.clock import Clock
from ..domain.plan import DaySelection
from ..errors import ExportError
from ..settings import Config
from .reading import archive_state, requested_range
from .results import DayState, DayStatus, StatusResult


def archive_status(cfg: Config, selection: DaySelection | None, *, clock: Clock) -> StatusResult:
    reader = ArchiveReader.open(
        ArchiveLayout(cfg.export.output_dir), configured=cfg.export.timezone
    )
    manifests = reader.manifest_days()
    state = archive_state(reader)
    span = requested_range(reader, selection, clock)
    if span is None:
        if not manifests:
            return StatusResult(state, ())
        span = manifests[0], manifests[-1]
    start, end = span
    present = set(manifests)
    days = []
    day = start
    while day <= end:
        days.append(DayStatus(day, _state(reader, day) if day in present else DayState.MISSING))
        day += timedelta(days=1)
    return StatusResult(state, tuple(days))


def _state(reader: ArchiveReader, day: date) -> DayState:
    try:
        manifest = reader.manifest(day)
    except ExportError:
        return DayState.INVALID
    if manifest is None:  # pragma: no cover - the manifest vanished since it was listed
        return DayState.MISSING
    return DayState(manifest.status.value)
