from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter import time_utils

BERLIN = ZoneInfo("Europe/Berlin")


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 7, 30, 12, 0, 0, tzinfo=BERLIN)
        return value if tz is None else value.astimezone(tz)


def test_today_and_latest_complete_day_are_local(monkeypatch):
    monkeypatch.setattr(time_utils, "datetime", FrozenDateTime)

    assert time_utils.today_local(BERLIN) == date(2026, 7, 30)
    assert time_utils.latest_complete_day(BERLIN) == date(2026, 7, 29)
    assert time_utils.is_day_complete(date(2026, 7, 29), BERLIN)
    assert not time_utils.is_day_complete(date(2026, 7, 30), BERLIN)


def test_iter_days_is_inclusive_and_empty_when_reversed():
    assert list(time_utils.iter_days(date(2026, 7, 27), date(2026, 7, 29))) == [
        date(2026, 7, 27),
        date(2026, 7, 28),
        date(2026, 7, 29),
    ]
    assert list(time_utils.iter_days(date(2026, 7, 30), date(2026, 7, 29))) == []


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("2026-07-28", date(2026, 7, 28)),
        (" 2026-07-28 ", date(2026, 7, 28)),
        ("yesterday", date(2026, 7, 29)),
        ("TODAY", date(2026, 7, 30)),
    ],
)
def test_parse_date_arg(monkeypatch, argument, expected):
    monkeypatch.setattr(time_utils, "datetime", FrozenDateTime)
    assert time_utils.parse_date_arg(argument, BERLIN) == expected


@pytest.mark.parametrize("argument", ["", "2026/07/28", "28.07.2026", "tomorrow"])
def test_parse_date_arg_rejects_unknown_formats(argument):
    with pytest.raises(ValueError, match="Unrecognised date argument"):
        time_utils.parse_date_arg(argument, BERLIN)
