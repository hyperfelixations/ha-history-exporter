from __future__ import annotations

from datetime import date

from ha_history_exporter.domain.plan import DayAction, build_plan

TODAY = date(2026, 7, 30)


def never(_day: date) -> str | None:
    return None


def plan_for(
    start: date,
    end: date,
    *,
    partial_day: date | None = None,
    purge_keep_days: int | None = None,
) -> list[tuple[date, DayAction]]:
    plan = build_plan(
        start,
        end,
        today=TODAY,
        completed=never,
        partial_day=partial_day,
        purge_keep_days=purge_keep_days,
    )
    return [(decision.day, decision.action) for decision in plan.decisions]


def test_complete_days_are_exported_and_the_running_day_is_skipped() -> None:
    assert plan_for(date(2026, 7, 28), date(2026, 7, 31)) == [
        (date(2026, 7, 28), DayAction.EXPORT),
        (date(2026, 7, 29), DayAction.EXPORT),
        (TODAY, DayAction.SKIP_INCOMPLETE),
        (date(2026, 7, 31), DayAction.SKIP_INCOMPLETE),
    ]


def test_the_running_day_is_exported_partially_only_when_named() -> None:
    assert plan_for(TODAY, TODAY, partial_day=TODAY) == [(TODAY, DayAction.EXPORT_PARTIAL)]


def test_a_completed_day_is_skipped_unless_forced() -> None:
    done = {date(2026, 7, 28): "manifest.json status=ok, 4 state objects"}
    plan = build_plan(date(2026, 7, 28), date(2026, 7, 28), today=TODAY, completed=done.get)
    assert plan.decisions[0].action is DayAction.SKIP_EXISTING
    assert plan.decisions[0].reason == "manifest.json status=ok, 4 state objects"
    forced = build_plan(
        date(2026, 7, 28), date(2026, 7, 28), today=TODAY, completed=done.get, force=True
    )
    assert forced.days_to_export == [date(2026, 7, 28)]


def test_days_touching_the_retention_boundary_are_blocked() -> None:
    assert plan_for(date(2026, 7, 16), date(2026, 7, 17), purge_keep_days=14) == [
        (date(2026, 7, 16), DayAction.BLOCKED_RETENTION),
        (date(2026, 7, 17), DayAction.EXPORT),
    ]


def test_a_completed_day_beyond_retention_is_still_skipped() -> None:
    done = {date(2026, 7, 1): "ok"}
    plan = build_plan(
        date(2026, 7, 1), date(2026, 7, 1), today=TODAY, completed=done.get, purge_keep_days=14
    )
    assert plan.decisions[0].action is DayAction.SKIP_EXISTING


def test_plan_partitions_its_decisions() -> None:
    done = {date(2026, 7, 29): "ok"}
    plan = build_plan(date(2026, 7, 28), TODAY, today=TODAY, completed=done.get, partial_day=TODAY)
    assert plan.days_to_export == [date(2026, 7, 28), TODAY]
    assert plan.partial_days == [TODAY]
    assert plan.days(DayAction.SKIP_EXISTING) == [date(2026, 7, 29)]
    assert plan.latest_complete == date(2026, 7, 29)
