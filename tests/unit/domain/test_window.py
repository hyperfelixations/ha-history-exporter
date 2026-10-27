from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.domain.window import is_offset_label, offset_label

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
