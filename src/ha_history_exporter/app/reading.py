"""What every reading command needs: the archive's state and the covered days."""

from __future__ import annotations

from datetime import date, datetime

from ..archive.reader import ArchiveReader
from ..domain.plan import DaySelection
from .results import ArchiveState


def archive_state(reader: ArchiveReader) -> ArchiveState:
    lock = reader.lock()
    return ArchiveState(
        output_dir=reader.layout.root,
        zone=reader.zone,
        described=reader.descriptor is not None,
        pending_transactions=reader.pending_transactions,
        running=(lock.holder or "unknown holder").replace("\n", ", ") if lock.held else None,
    )


def requested_range(
    reader: ArchiveReader, selection: DaySelection | None, now: datetime
) -> tuple[date, date] | None:
    """The first and last day *selection* names at *now* in the archive's calendar.

    None stands for every day.
    """
    if selection is None:
        return None
    today = (now.astimezone(reader.zone.zone) if reader.zone else now.astimezone()).date()
    requested = selection.resolve(today)
    return requested.start, requested.end
