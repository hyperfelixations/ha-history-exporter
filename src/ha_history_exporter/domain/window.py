"""Local calendar time: UTC offsets as stored in every record."""

from __future__ import annotations

from datetime import datetime
from typing import TypeGuard
from zoneinfo import ZoneInfo


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
