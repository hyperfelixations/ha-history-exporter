from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from ha_history_exporter.domain.window import (
    DayIdentity,
    DayWindow,
    is_offset_label,
    offset_label,
)
from ha_history_exporter.errors import ExportError

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.mark.parametrize(
    ("instant", "zone", "expected"),
    [
        (datetime(2026, 10, 25, 0, 59, 59, tzinfo=UTC), BERLIN, "+02:00"),
        (datetime(2026, 10, 25, 1, 0, 0, tzinfo=UTC), BERLIN, "+01:00"),
        (datetime(2026, 3, 29, 0, 59, 59, tzinfo=UTC), BERLIN, "+01:00"),
        (datetime(2026, 3, 29, 1, 0, 0, tzinfo=UTC), BERLIN, "+02:00"),
        (datetime(2026, 7, 28, 12, tzinfo=UTC), ZoneInfo("America/St_Johns"), "-02:30"),
        (datetime(2026, 1, 15, 12, tzinfo=UTC), ZoneInfo("Australia/Lord_Howe"), "+11:00"),
        (datetime(2026, 7, 15, 12, tzinfo=UTC), ZoneInfo("Australia/Lord_Howe"), "+10:30"),
        (datetime(2026, 7, 28, 12, tzinfo=UTC), ZoneInfo("UTC"), "+00:00"),
    ],
)
def test_offset_label_follows_the_zone_at_the_instant(
    instant: datetime, zone: ZoneInfo, expected: str
) -> None:
    assert offset_label(instant, zone) == expected


@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        ("Asia/Kolkata", "+05:21"),  # local mean time +05:21:10
        ("Africa/Monrovia", "-00:44"),  # local mean time -00:43:08
    ],
)
def test_offset_label_floors_historical_sub_minute_offsets(zone: str, expected: str) -> None:
    assert offset_label(datetime(1890, 1, 1, tzinfo=UTC), ZoneInfo(zone)) == expected


@pytest.mark.parametrize("value", ["+02:00", "-02:30", "+00:00", "+23:59"])
def test_well_formed_labels_are_accepted(value: str) -> None:
    assert is_offset_label(value)


@pytest.mark.parametrize("value", ["+24:00", "+02:60", "-99:99", "02:00", "+2:00", None, 2])
def test_malformed_labels_are_rejected(value: object) -> None:
    assert not is_offset_label(value)


@pytest.mark.parametrize(
    ("day", "hours", "start_utc", "end_utc"),
    [
        (date(2026, 1, 15), 24, "2026-01-14T23:00:00+00:00", "2026-01-15T23:00:00+00:00"),
        (date(2026, 6, 15), 24, "2026-06-14T22:00:00+00:00", "2026-06-15T22:00:00+00:00"),
        (date(2026, 3, 29), 23, "2026-03-28T23:00:00+00:00", "2026-03-29T22:00:00+00:00"),
        (date(2026, 10, 25), 25, "2026-10-24T22:00:00+00:00", "2026-10-25T23:00:00+00:00"),
    ],
)
def test_a_full_day_runs_from_local_midnight_to_local_midnight(
    day: date, hours: int, start_utc: str, end_utc: str
) -> None:
    window = DayWindow.full(day, BERLIN)
    assert (window.start_utc.isoformat(), window.end_utc.isoformat()) == (start_utc, end_utc)
    assert window.end_utc - window.start_utc == timedelta(hours=hours)
    assert not window.partial


def test_the_window_is_half_open() -> None:
    window = DayWindow.full(date(2026, 7, 28), BERLIN)
    tick = timedelta(microseconds=1)
    assert window.contains(window.start_utc)
    assert not window.contains(window.start_utc - tick)
    assert window.contains(window.end_utc - tick)
    assert not window.contains(window.end_utc)
    assert window.query_start == window.start_utc - tick
    assert window.query_start.tzinfo is UTC


def test_the_identity_keeps_the_manifest_boundary_strings() -> None:
    identity = DayWindow.full(date(2026, 10, 25), BERLIN).identity()
    assert identity == DayIdentity(
        day=date(2026, 10, 25),
        timezone="Europe/Berlin",
        start_local="2026-10-25T00:00:00+02:00",
        end_local="2026-10-26T00:00:00+01:00",
        start_utc="2026-10-24T22:00:00+00:00",
        end_utc="2026-10-25T23:00:00+00:00",
    )


def test_an_elapsed_day_ends_at_now_in_whole_seconds() -> None:
    now = datetime(2026, 6, 15, 12, 30, 45, 123456, tzinfo=UTC)
    window = DayWindow.elapsed(date(2026, 6, 15), BERLIN, now)
    assert window.partial
    assert window.start.isoformat() == "2026-06-15T00:00:00+02:00"
    assert window.end.isoformat() == "2026-06-15T14:30:45+02:00"


def test_an_elapsed_dst_day_starts_at_its_own_midnight() -> None:
    now = datetime(2026, 10, 25, 5, 0, tzinfo=BERLIN)
    window = DayWindow.elapsed(date(2026, 10, 25), BERLIN, now)
    assert window.start.isoformat() == "2026-10-25T00:00:00+02:00"
    assert window.end.isoformat() == "2026-10-25T05:00:00+01:00"


def test_only_the_running_day_can_be_elapsed() -> None:
    with pytest.raises(ValueError, match="is not today"):
        DayWindow.elapsed(date(2026, 6, 14), BERLIN, datetime(2026, 6, 15, 12, tzinfo=BERLIN))


def test_an_empty_window_is_refused() -> None:
    with pytest.raises(ExportError, match="no time to export"):
        DayWindow.elapsed(date(2026, 6, 15), BERLIN, datetime(2026, 6, 15, 0, 0, 0, 5, BERLIN))
    with pytest.raises(ExportError, match="no time to export"):
        DayWindow.full(date(2011, 12, 30), ZoneInfo("Pacific/Apia"))


def test_bounds_must_be_aware() -> None:
    naive = datetime(2026, 7, 28)  # noqa: DTZ001 - naive on purpose
    with pytest.raises(ValueError, match="timezone-aware"):
        DayWindow(date(2026, 7, 28), BERLIN, naive, naive + timedelta(days=1))


_ZONES = sorted(available_timezones())


@given(
    zone=st.sampled_from(_ZONES),
    day=st.dates(date(1971, 1, 1), date(2037, 12, 30)),
    position=st.floats(0, 1),
)
def test_every_instant_belongs_to_exactly_one_day(zone: str, day: date, position: float) -> None:
    timezone = ZoneInfo(zone)
    windows = []
    for offset in (0, 1):
        try:
            windows.append(DayWindow.full(day + timedelta(days=offset), timezone))
        except ExportError:  # a day the zone skipped
            assume(False)
    first, second = windows
    assert first.end_utc == second.start_utc
    span = second.end_utc - first.start_utc
    instant = first.start_utc + timedelta(
        microseconds=int(span / timedelta(microseconds=1) * position)
    )
    instant = min(instant, second.end_utc - timedelta(microseconds=1))
    assert first.contains(instant) != second.contains(instant)
    assert first.start == datetime(day.year, day.month, day.day, tzinfo=timezone)
