from __future__ import annotations

from datetime import date

import pytest

from ha_history_exporter.domain.plan import (
    DaySelection,
    RelativeDay,
    RequestedDays,
    parse_day,
)
from ha_history_exporter.errors import UsageError

TODAY = date(2026, 7, 30)
YESTERDAY = date(2026, 7, 29)


@pytest.mark.parametrize(
    ("text", "day"),
    [
        ("2026-06-15", date(2026, 6, 15)),
        (" Yesterday ", RelativeDay.YESTERDAY),
        ("TODAY", RelativeDay.TODAY),
    ],
)
def test_days_are_written_as_iso_dates_or_words(text: str, day: object) -> None:
    assert parse_day(text) == day


@pytest.mark.parametrize("text", ["", "20260615", "2026/06/15", "15.06.2026", "tomorrow"])
def test_other_spellings_are_rejected_with_the_accepted_ones(text: str) -> None:
    with pytest.raises(ValueError, match="use YYYY-MM-DD, yesterday or today"):
        parse_day(text)


def test_without_a_selection_the_most_recent_complete_day_is_meant() -> None:
    assert DaySelection().resolve(TODAY) == RequestedDays(YESTERDAY, YESTERDAY)


def test_the_last_n_days_end_yesterday() -> None:
    assert DaySelection(last_days=1).resolve(TODAY) == RequestedDays(YESTERDAY, YESTERDAY)
    assert DaySelection(last_days=10).resolve(TODAY) == RequestedDays(date(2026, 7, 20), YESTERDAY)


def test_only_today_named_alone_asks_for_the_running_day() -> None:
    assert DaySelection.single(RelativeDay.TODAY).resolve(TODAY).partial_day == TODAY
    assert DaySelection.single(TODAY).resolve(TODAY).partial_day == TODAY
    reaching_today = DaySelection(start=RelativeDay.YESTERDAY, end=RelativeDay.TODAY)
    assert reaching_today.resolve(TODAY) == RequestedDays(YESTERDAY, TODAY)


def test_a_range_must_not_run_backwards() -> None:
    with pytest.raises(ValueError, match="starts after it ends"):
        DaySelection(start=TODAY, end=YESTERDAY)
    with pytest.raises(ValueError, match="starts after it ends"):
        DaySelection(start=RelativeDay.TODAY, end=RelativeDay.YESTERDAY)
    with pytest.raises(UsageError, match="starts after it ends"):
        DaySelection(start=RelativeDay.TODAY, end=YESTERDAY).resolve(TODAY)


@pytest.mark.parametrize(
    "arguments",
    [
        {"last_days": 0},
        {"last_days": 3, "start": TODAY, "end": TODAY},
        {"start": TODAY},
        {"end": TODAY},
    ],
)
def test_contradictory_selections_cannot_be_built(arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"last_days|range"):
        DaySelection(**arguments)  # type: ignore[arg-type]
