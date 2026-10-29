"""When to retry a request to Home Assistant, and how long to wait.

Pure functions of the attempt number and the server's ``Retry-After``; the
clock and the sleep belong to the caller. A server may ask for at most
:data:`RETRY_AFTER_CAP` seconds. See internal dev doc § "Home Assistant source".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime

RETRY_AFTER_CAP = 300.0


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int
    backoff: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.max_retries < 0 or (self.max_retries and not self.backoff):
            raise ValueError("retries need a non-empty backoff schedule")

    def delay(self, retry: int, retry_after: float | None = None) -> float:
        """Seconds to wait before retry number *retry*, counted from 0."""
        if retry_after is not None:
            return min(max(retry_after, 0.0), RETRY_AFTER_CAP)
        return self.backoff[min(retry, len(self.backoff) - 1)]


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """Seconds from a ``Retry-After`` header: delta seconds or an HTTP date."""
    if value is None:
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max((when - now).total_seconds(), 0.0)
