"""Local calendar time: day windows, day identities and record UTC offsets.

A day window is the half-open interval ``[start, end)`` from local midnight
to the next local midnight, or to the moment a partial export runs. Every
instant belongs to exactly one day. Home Assistant returns rows with
``start_time < last_updated < end_time``, so a window asks from
:attr:`DayWindow.query_start`, one microsecond before its start. See internal
dev doc § "Day window".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TypeGuard
from zoneinfo import ZoneInfo

from ..errors import ExportError

_ONE_MICROSECOND = timedelta(microseconds=1)


def format_iso(instant: datetime) -> str:
    """ISO 8601 with offset and whole seconds, as manifests record boundaries."""
    return instant.replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class DayIdentity:
    """The textual boundaries that identify one exported day in its manifest."""

    day: date
    timezone: str
    start_local: str
    end_local: str
    start_utc: str
    end_utc: str


def _local_midnight(day: date, timezone: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone)


@dataclass(frozen=True)
class DayWindow:
    """The part of one local calendar day that an export captures."""

    day: date
    timezone: ZoneInfo
    #: Local midnight that opens the day; included.
    start: datetime
    #: The next local midnight, or the cut-off of a partial day; excluded.
    end: datetime
    partial: bool = False

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Day window bounds must be timezone-aware.")
        if self.start_utc >= self.end_utc:
            raise ExportError(
                f"Day {self.day} has no time to export in {self.timezone.key}.",
                details=(
                    "The local day is empty: it was skipped by a calendar change, "
                    "or a partial export started at its first second."
                ),
                context={"day": str(self.day)},
            )

    @classmethod
    def full(cls, day: date, timezone: ZoneInfo) -> DayWindow:
        """From local midnight of *day* to local midnight of the next day."""
        return cls(
            day,
            timezone,
            _local_midnight(day, timezone),
            _local_midnight(day + timedelta(days=1), timezone),
        )

    @classmethod
    def elapsed(cls, day: date, timezone: ZoneInfo, now: datetime) -> DayWindow:
        """From local midnight of the running *day* to *now* in whole seconds."""
        current = now.astimezone(timezone)
        if day != current.date():
            raise ValueError(
                f"Day {day} is not today ({current.date()}) in {timezone.key}; "
                "only the running day can be exported partially."
            )
        return cls(
            day,
            timezone,
            _local_midnight(day, timezone),
            current.replace(microsecond=0),
            partial=True,
        )

    @property
    def start_utc(self) -> datetime:
        return self.start.astimezone(UTC)

    @property
    def end_utc(self) -> datetime:
        return self.end.astimezone(UTC)

    @property
    def query_start(self) -> datetime:
        """The exclusive start to send to Home Assistant, so that ``start`` is included."""
        return self.start_utc - _ONE_MICROSECOND

    def contains(self, instant: datetime) -> bool:
        return self.start_utc <= instant < self.end_utc

    def identity(self) -> DayIdentity:
        return DayIdentity(
            day=self.day,
            timezone=self.timezone.key,
            start_local=format_iso(self.start),
            end_local=format_iso(self.end),
            start_utc=format_iso(self.start_utc),
            end_utc=format_iso(self.end_utc),
        )


def offset_label(instant: datetime, timezone: ZoneInfo) -> str:
    """The UTC offset of *instant* in *timezone*, for example ``+02:00``.

    Sub-minute offsets of historical local mean time are floored to whole
    minutes, so the label always has the form ``±HH:MM``.
    """
    delta = instant.astimezone(timezone).utcoffset()
    if delta is None:  # pragma: no cover - ZoneInfo always supplies an offset
        raise ValueError(f"{timezone} supplies no UTC offset.")
    minutes = int(delta.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def is_offset_label(value: object) -> TypeGuard[str]:
    """True for a well-formed ``±HH:MM`` label with HH ≤ 23 and MM ≤ 59."""
    return (
        isinstance(value, str)
        and len(value) == 6
        and value[0] in "+-"
        and value[3] == ":"
        and (value[1:3] + value[4:6]).isdigit()
        and int(value[1:3]) <= 23
        and int(value[4:6]) <= 59
    )
