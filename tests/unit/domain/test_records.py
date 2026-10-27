from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from ha_history_exporter.domain.records import (
    LogicalDigest,
    canonical_line,
    parse_utc_timestamp,
)
from ha_history_exporter.errors import ValidationError
from tests.support.records import make_record


def _digest(*records: object) -> str:
    digest = LogicalDigest()
    for record in records:
        digest.add(record)  # type: ignore[arg-type]
    return digest.hexdigest()


def test_attribute_json_keeps_insertion_order_and_unicode() -> None:
    record = make_record(attributes={"zeta": "Küche", "alpha": [1, None]})
    assert record.attributes_json == '{"zeta": "Küche", "alpha": [1, null]}'
    assert make_record(attributes=None).attributes_json == "null"


def test_extra_json_is_absent_without_unknown_fields() -> None:
    assert make_record().extra_json is None
    record = make_record(extra=(("context", {"id": "x"}), ("origin", "LOCAL")))
    assert record.extra_json == '{"context": {"id": "x"}, "origin": "LOCAL"}'


def test_canonical_line_sorts_every_level_and_is_compact() -> None:
    record = make_record(attributes={"b": {"y": 1, "x": 2}, "a": "Ä"})
    line = canonical_line(record)
    assert line.endswith(b"\n")
    assert json.loads(line) == {
        "attributes": {"a": "Ä", "b": {"x": 2, "y": 1}},
        "entity_id": "sensor.one",
        "extra": {},
        "last_changed": "2026-07-28T10:00:00+00:00",
        "last_updated": "2026-07-28T10:00:00+00:00",
        "local_offset": "+02:00",
        "state": "21.5",
    }
    assert b", " not in line
    assert line.decode("utf-8").startswith('{"attributes":{"a":"Ä","b":{"x":2,"y":1}}')


def test_logical_digest_ignores_attribute_order_but_not_values() -> None:
    first = make_record(attributes={"a": 1, "b": 2})
    reordered = make_record(attributes={"b": 2, "a": 1})
    changed = replace(first, state="different")

    assert _digest(first) == _digest(reordered)
    assert _digest(first) != _digest(changed)
    assert _digest(first, changed) != _digest(changed, first)


def test_logical_digest_counts_rows() -> None:
    digest = LogicalDigest()
    for _ in range(3):
        digest.add(make_record())
    assert digest.rows == 3


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-28T12:00:00+02:00", datetime(2026, 7, 28, 10, tzinfo=UTC)),
        ("2026-07-28T10:00:00Z", datetime(2026, 7, 28, 10, tzinfo=UTC)),
        (
            "2026-07-28T10:00:00.123456+00:00",
            datetime(2026, 7, 28, 10, 0, 0, 123456, tzinfo=UTC),
        ),
    ],
)
def test_timestamps_are_normalised_to_utc(value: str, expected: datetime) -> None:
    assert parse_utc_timestamp(value, name="last_changed", location="here") == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "here: last_changed must be a timestamp"),
        ("", "here: last_changed must be a timestamp"),
        ("garbage", "here: last_changed is not valid ISO 8601"),
        ("2026-07-28T10:00:00", "here: last_changed needs a timezone"),
    ],
)
def test_invalid_timestamps_name_their_location(value: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_utc_timestamp(value, name="last_changed", location="here")
