"""Which requested day is exported or skipped, and why.

A day before *today* is complete. A complete day whose manifest says ``ok`` is
skipped unless forced. A complete day at or beyond the known Recorder
retention boundary is skipped even when forced, because its history may
already be purged. The running day is exported only when it was requested by
name.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from ..errors import Remedy, UsageError


class DayAction(StrEnum):
    EXPORT = "export"
    EXPORT_PARTIAL = "export_partial"
    SKIP_EXISTING = "skip_existing"
    SKIP_INCOMPLETE = "skip_incomplete"
    SKIP_RETENTION = "skip_retention"


@dataclass(frozen=True)
class DayDecision:
    day: date
    action: DayAction
    reason: str | None = None


@dataclass(frozen=True)
class ExportPlan:
    requested_start: date
    requested_end: date
    today: date
    decisions: tuple[DayDecision, ...]

    @property
    def latest_complete(self) -> date:
        return self.today - timedelta(days=1)

    def days(self, *actions: DayAction) -> list[date]:
        return [decision.day for decision in self.decisions if decision.action in actions]

    @property
    def days_to_export(self) -> list[date]:
        return self.days(DayAction.EXPORT, DayAction.EXPORT_PARTIAL)

    @property
    def partial_days(self) -> list[date]:
        return self.days(DayAction.EXPORT_PARTIAL)

    @property
    def retention_skipped(self) -> list[date]:
        return self.days(DayAction.SKIP_RETENTION)


def build_plan(
    requested_start: date,
    requested_end: date,
    *,
    today: date,
    completed: Callable[[date], str | None],
    force: bool = False,
    partial_day: date | None = None,
    purge_keep_days: int | None = None,
) -> ExportPlan:
    """Decide every day from *requested_start* to *requested_end* inclusive.

    *completed* returns a description of a day's successful manifest, or None.
    """
    decisions: list[DayDecision] = []
    day = requested_start
    while day <= requested_end:
        decisions.append(_decide(day, today, completed, force, partial_day, purge_keep_days))
        day += timedelta(days=1)
    return ExportPlan(requested_start, requested_end, today, tuple(decisions))


def _decide(
    day: date,
    today: date,
    completed: Callable[[date], str | None],
    force: bool,
    partial_day: date | None,
    purge_keep_days: int | None,
) -> DayDecision:
    if day >= today:
        if day == partial_day:
            return DayDecision(
                day,
                DayAction.EXPORT_PARTIAL,
                f"Day {day} was requested by name and is exported up to now.",
            )
        return DayDecision(
            day, DayAction.SKIP_INCOMPLETE, f"Day {day} is not yet complete (today is {today})."
        )
    existing = completed(day)
    if existing is not None and not force:
        return DayDecision(day, DayAction.SKIP_EXISTING, existing)
    if purge_keep_days is not None and (today - day).days >= purge_keep_days:
        reason = f"Day {day} touches or exceeds the {purge_keep_days}-day Recorder retention."
        if existing is not None:
            reason += " The saved copy stays."
        return DayDecision(day, DayAction.SKIP_RETENTION, reason)
    return DayDecision(day, DayAction.EXPORT)


class RelativeDay(StrEnum):
    TODAY = "today"
    YESTERDAY = "yesterday"


type DaySpec = date | RelativeDay

_RANK = {RelativeDay.YESTERDAY: 0, RelativeDay.TODAY: 1}


def parse_day(text: str) -> DaySpec:
    """``YYYY-MM-DD``, ``today`` or ``yesterday``; raises ValueError otherwise."""
    value = text.strip().lower()
    try:
        return RelativeDay(value)
    except ValueError:
        pass
    if len(value) == 10:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"'{text}' is not a day; use YYYY-MM-DD, yesterday or today.")


def _resolve(spec: DaySpec, today: date) -> date:
    match spec:
        case RelativeDay.TODAY:
            return today
        case RelativeDay.YESTERDAY:
            return today - timedelta(days=1)
        case _:
            return spec


@dataclass(frozen=True)
class RequestedDays:
    start: date
    end: date
    #: The running day, when it was requested by name.
    partial_day: date | None = None


@dataclass(frozen=True)
class DaySelection:
    """The days a run is asked for, before the archive's calendar is known.

    Without a selection the most recent complete day is meant. Only a single
    day that is today asks for the running day; a range that reaches today
    still skips it.
    """

    start: DaySpec | None = None
    end: DaySpec | None = None
    last_days: int | None = None

    def __post_init__(self) -> None:
        if self.last_days is not None and (
            self.last_days < 1 or self.start is not None or self.end is not None
        ):
            raise ValueError("last_days is a positive count without start and end")
        if (self.start is None) != (self.end is None):
            raise ValueError("a range needs a start and an end")
        start, end = self.start, self.end
        backwards = (
            isinstance(start, RelativeDay)
            and isinstance(end, RelativeDay)
            and _RANK[start] > _RANK[end]
        ) or (isinstance(start, date) and isinstance(end, date) and start > end)
        if backwards:
            raise ValueError(f"The range starts after it ends ({start} > {end}).")

    @classmethod
    def single(cls, day: DaySpec) -> DaySelection:
        return cls(start=day, end=day)

    def resolve(self, today: date) -> RequestedDays:
        latest = today - timedelta(days=1)
        if self.last_days is not None:
            return RequestedDays(latest - timedelta(days=self.last_days - 1), latest)
        if self.start is None or self.end is None:
            return RequestedDays(latest, latest)
        start, end = _resolve(self.start, today), _resolve(self.end, today)
        if start > end:
            raise UsageError(
                f"The range starts after it ends ({start} > {end}).",
                remedies=(
                    Remedy(
                        "Name the earlier day first:",
                        "hhe export --start-date 2026-06-01 --end-date yesterday",
                    ),
                ),
            )
        partial = start if start == end == today else None
        return RequestedDays(start, end, partial)
