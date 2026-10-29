"""The identity of one run and the instant it started.

The clock is read once, when the run starts. The run ID, the lock holder,
the name of the log file and of the entity snapshot, the meaning of "today"
and the end of a partial day all derive from that instant. See internal dev
doc § "Export run".
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from ..archive.workspace import new_run_id
from ..domain.clock import Clock


@dataclass(frozen=True)
class RunContext:
    run_id: str
    #: The start of the run, in UTC.
    started_at: datetime

    @classmethod
    def start(cls, clock: Clock) -> RunContext:
        started = clock.now().astimezone(UTC)
        return cls(new_run_id(started), started)

    def local(self, zone: ZoneInfo) -> datetime:
        return self.started_at.astimezone(zone)

    def today(self, zone: ZoneInfo) -> date:
        return self.local(zone).date()

    def lock_holder(self) -> str:
        """What the archive lock file says about the run that holds it."""
        return (
            f"run_id: {self.run_id}\npid: {os.getpid()}\nhost: {socket.gethostname()}\n"
            f"started_at: {self.started_at.isoformat()}\n"
        )
