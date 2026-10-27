"""Which requested day is exported, skipped or blocked, and why.

A day before *today* is complete. A complete day whose manifest says ``ok`` is
skipped unless forced. A complete day at or beyond the known Recorder
retention boundary is blocked, because its history may already be purged.
The running day is exported only when it was requested by name.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum


class DayAction(StrEnum):
    EXPORT = "export"
    EXPORT_PARTIAL = "export_partial"
    SKIP_EXISTING = "skip_existing"
    SKIP_INCOMPLETE = "skip_incomplete"
    BLOCKED_RETENTION = "blocked_retention"


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
        return DayDecision(
            day,
            DayAction.BLOCKED_RETENTION,
            f"Day {day} touches or exceeds the {purge_keep_days}-day Recorder retention.",
        )
    return DayDecision(day, DayAction.EXPORT)
