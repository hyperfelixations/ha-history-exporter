from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.domain.stored import (
    RecordContract,
    check_against_request,
    stored_record,
)
from ha_history_exporter.domain.window import DayWindow
from ha_history_exporter.errors import ValidationError
from tests.support.records import make_record

BERLIN = ZoneInfo("Europe/Berlin")
WINDOW = DayWindow.full(date(2026, 7, 28), BERLIN)
START, END = WINDOW.start_utc, WINDOW.end_utc


def row(**changes: Any) -> dict[str, Any]:
    return {
        "entity_id": "sensor.one",
        "state": "1",
        "last_changed": "2026-07-28T10:00:00+00:00",
        "last_updated": "2026-07-28T10:00:00+00:00",
        "attributes": {"friendly_name": "One"},
        "local_offset": "+02:00",
        **changes,
    }


def contract() -> RecordContract:
    return RecordContract(requested_entity_ids=frozenset({"sensor.one"}), window=WINDOW)


def test_unknown_jsonl_fields_become_ordered_extras() -> None:
    record = stored_record(row(context={"id": "x"}, origin="LOCAL"), location="f.jsonl, line 1")
    assert record.extra == (("context", {"id": "x"}), ("origin", "LOCAL"))


def test_formats_with_an_extra_column_pass_it_explicitly() -> None:
    record = stored_record(row(), location="f.csv, line 2", extra={"origin": "LOCAL"})
    assert record.extra == (("origin", "LOCAL"),)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"entity_id": ""}, "entity_id"),
        ({"state": 1}, "state must be a string"),
        ({"last_changed": "garbage"}, "2026-07-28.jsonl, line 7: last_changed is not valid"),
        ({"last_updated": "2026-07-28T10:00:00"}, "needs a timezone"),
        ({"last_changed": "2026-07-28T11:00:00+00:00"}, "after last_updated"),
        ({"attributes": ["list"]}, "attributes must be an object or null"),
        ({"local_offset": "+24:00"}, "local_offset is not valid"),
        ({"local_offset": "+02:60"}, "local_offset is not valid"),
        ({"local_offset": "-99:99"}, "local_offset is not valid"),
    ],
)
def test_invalid_stored_rows_name_file_and_line(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        stored_record(row(**changes), location="2026-07-28.jsonl, line 7")


@pytest.mark.parametrize(
    "updated", [START, START + timedelta(hours=12), END - timedelta(microseconds=1)]
)
def test_records_of_the_request_pass(updated: Any) -> None:
    check_against_request(make_record(updated=updated), contract(), location="f")


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (make_record("sensor.other"), "not requested"),
        (make_record(updated=END), "outside"),
        (make_record(updated=START - timedelta(microseconds=1)), "outside"),
    ],
)
def test_records_outside_the_request_are_rejected(record: Any, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        check_against_request(record, contract(), location="f")


def test_an_inconsistent_local_offset_is_rejected() -> None:
    record = make_record(timezone=ZoneInfo("UTC"))
    with pytest.raises(ValidationError, match="inconsistent local_offset"):
        check_against_request(record, contract(), location="f")
