from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter import time_utils

BERLIN = ZoneInfo("Europe/Berlin")
UTC = ZoneInfo("UTC")


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


@pytest.mark.parametrize(
    ("day", "expected_hours", "start_utc", "end_utc"),
    [
        (date(2026, 1, 15), 24, "2026-01-14T23:00:00+00:00", "2026-01-15T23:00:00+00:00"),
        (date(2026, 6, 15), 24, "2026-06-14T22:00:00+00:00", "2026-06-15T22:00:00+00:00"),
        (date(2026, 3, 29), 23, "2026-03-28T23:00:00+00:00", "2026-03-29T22:00:00+00:00"),
        (date(2026, 10, 25), 25, "2026-10-24T22:00:00+00:00", "2026-10-25T23:00:00+00:00"),
    ],
)
def test_local_day_bounds_handle_dst(day, expected_hours, start_utc, end_utc):
    start, end = time_utils.local_day_bounds(day, BERLIN)

    assert time_utils.to_utc(start).isoformat() == start_utc
    assert time_utils.to_utc(end).isoformat() == end_utc
    assert (time_utils.to_utc(end) - time_utils.to_utc(start)) == timedelta(
        hours=expected_hours
    )


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


def test_format_iso_removes_microseconds_and_preserves_offset():
    value = datetime(2026, 7, 30, 10, 11, 12, 987654, tzinfo=BERLIN)
    assert time_utils.format_iso(value) == "2026-07-30T10:11:12+02:00"


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("2026-10-25T00:59:59+00:00", "+02:00"),
        ("2026-10-25T01:00:00+00:00", "+01:00"),
        ("2026-03-29T00:59:59+00:00", "+01:00"),
        ("2026-03-29T01:00:00+00:00", "+02:00"),
        (None, None),
        ("not-a-timestamp", None),
    ],
)
def test_local_offset_str_handles_dst_and_invalid_values(timestamp, expected):
    assert time_utils.local_offset_str(timestamp, BERLIN) == expected


def test_to_utc_requires_no_special_case_for_utc_input():
    value = datetime(2026, 7, 30, 10, tzinfo=UTC)
    assert time_utils.to_utc(value) == value
