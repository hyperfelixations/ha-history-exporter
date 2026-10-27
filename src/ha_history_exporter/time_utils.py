"""Calendar days as the command line names them: today, yesterday, ranges.

Day windows themselves live in :mod:`ha_history_exporter.domain.window`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


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
    raise ValueError(f"Unrecognised date argument: '{s}'. Use YYYY-MM-DD, 'yesterday', or 'today'.")
