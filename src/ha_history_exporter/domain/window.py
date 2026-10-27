"""Local calendar time: day identities and the UTC offsets stored in every record."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TypeGuard
from zoneinfo import ZoneInfo


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

    @classmethod
    def of(cls, day: date, timezone: ZoneInfo, start: datetime, end: datetime) -> DayIdentity:
        return cls(
            day=day,
            timezone=timezone.key,
            start_local=format_iso(start),
            end_local=format_iso(end),
            start_utc=format_iso(start.astimezone(UTC)),
            end_utc=format_iso(end.astimezone(UTC)),
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
