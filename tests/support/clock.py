"""A clock that stands still, for runs whose every timestamp is known."""

from __future__ import annotations

from datetime import datetime


class FixedClock:
    def __init__(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("a clock tells aware time")
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0
