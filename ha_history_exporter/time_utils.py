"""Timezone-aware day boundary utilities for Europe/Berlin with full DST support.

All "local day" operations use the calendar date in the configured timezone.
DST-transition days (23 h or 25 h) are handled automatically by zoneinfo.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Iterator
from zoneinfo import ZoneInfo

_UTC = ZoneInfo("UTC")


def today_local(tz: ZoneInfo) -> date:
    """Current calendar date in *tz* (e.g. Europe/Berlin)."""
    return datetime.now(tz).date()


def latest_complete_day(tz: ZoneInfo) -> date:
    """The most recent fully-elapsed calendar day in *tz* (= yesterday local)."""
    return today_local(tz) - timedelta(days=1)


def is_day_complete(day: date, tz: ZoneInfo) -> bool:
    """True iff *day* is strictly in the past relative to today in *tz*.

    A day is complete once its last second has elapsed — i.e. once the next
    calendar day has started in the local timezone.
    """
    return day < today_local(tz)


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Return (start, end) as timezone-aware datetimes for one calendar day.

    DST transitions are handled automatically:
      - Summer day (23 h): correct 22:00–22:00 UTC window
      - Winter day (25 h): correct 23:00–23:00 UTC window
      - Transition days: `zoneinfo` picks the correct offset

    Returns:
        (start, end) where start == 00:00:00 local on *day* and
        end   == 00:00:00 local on *day + 1 day* (exclusive upper bound).
    """
    start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=tz)
    next_day = day + timedelta(days=1)
    end = datetime(next_day.year, next_day.month, next_day.day, 0, 0, 0, tzinfo=tz)
    return start, end


def to_utc(dt: datetime) -> datetime:
    """Convert any timezone-aware datetime to UTC."""
    return dt.astimezone(_UTC)


def last_n_complete_days(n: int, tz: ZoneInfo) -> tuple[date, date]:
    """Return (start, end) covering the *n* most recent fully elapsed local days.

    ``n = 1`` is yesterday; ``n = 10`` is the ten days ending yesterday. The
    still incomplete current day is never part of the range.

    Raises:
        ValueError if *n* is smaller than 1.
    """
    if n < 1:
        raise ValueError("--last-days must be 1 or greater.")
    end = latest_complete_day(tz)
    return end - timedelta(days=n - 1), end


def iter_days(start: date, end: date) -> Iterator[date]:
    """Yield calendar dates from *start* to *end* inclusive."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def parse_date_arg(s: str, tz: ZoneInfo) -> date:
    """Parse a user-supplied date string into a calendar date.

    Supported formats:
        YYYY-MM-DD   — literal date
        yesterday    — today_local - 1 day
        today        — today_local (caller should warn about incomplete data)

    Raises:
        ValueError if the string does not match any supported format.
    """
    s = s.strip().lower()
    if s == "yesterday":
        return today_local(tz) - timedelta(days=1)
    if s == "today":
        return today_local(tz)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return date.fromisoformat(s)
    raise ValueError(
        f"Unrecognised date argument: '{s}'. "
        "Use YYYY-MM-DD, 'yesterday', or 'today'."
    )


def format_iso(dt: datetime) -> str:
    """Return an ISO-8601 string with UTC offset (no microseconds)."""
    return dt.replace(microsecond=0).isoformat()


def local_offset_str(ts_str: str | None, tz: ZoneInfo) -> str | None:
    """Return the UTC offset for *ts_str* expressed in *tz*, e.g. '+02:00' or '+01:00'.

    Computed per timestamp so DST transition days are handled correctly:
    two state changes on the same day may return different offsets if they
    straddle the clock-change moment (e.g. 01:29 UTC → '+02:00',
    01:31 UTC → '+01:00' on the October fallback night).

    Returns None if *ts_str* is missing or cannot be parsed.
    """
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        offset = dt.astimezone(tz).utcoffset()
        if offset is None:
            return None
        total_sec = int(offset.total_seconds())
        sign = "+" if total_sec >= 0 else "-"
        total_sec = abs(total_sec)
        h, rem = divmod(total_sec, 3600)
        m = rem // 60
        return f"{sign}{h:02d}:{m:02d}"
    except (ValueError, AttributeError, TypeError):
        return None
