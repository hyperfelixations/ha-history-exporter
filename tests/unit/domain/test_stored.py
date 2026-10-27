from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.domain.stored import (
    RecordContract,
    check_against_request,
    stored_record,
)
from ha_history_exporter.errors import ValidationError
from tests.support.records import make_record

BERLIN = ZoneInfo("Europe/Berlin")
START = datetime(2026, 7, 27, 22, tzinfo=UTC)
END = datetime(2026, 7, 28, 22, tzinfo=UTC)


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


def contract(*, allow_start_state: bool = False) -> RecordContract:
    return RecordContract(
        requested_entity_ids=frozenset({"sensor.one"}),
        start=START,
        end=END,
        allow_start_state=allow_start_state,
        timezone=BERLIN,
    )


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


def test_records_of_the_request_pass() -> None:
    check_against_request(make_record(), contract(), first_for_entity=True, location="f")


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (make_record("sensor.other"), "not requested"),
        (make_record(updated=END), "outside"),
        (make_record(updated=START), "outside"),
    ],
)
def test_records_outside_the_request_are_rejected(record: Any, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        check_against_request(record, contract(), first_for_entity=True, location="f")


def test_a_carry_in_at_the_start_is_allowed_only_first_and_only_when_requested() -> None:
    carry_in = make_record(updated=START)
    check_against_request(
        carry_in, contract(allow_start_state=True), first_for_entity=True, location="f"
    )
    with pytest.raises(ValidationError, match="outside"):
        check_against_request(
            carry_in, contract(allow_start_state=True), first_for_entity=False, location="f"
        )


def test_an_inconsistent_local_offset_is_rejected() -> None:
    record = make_record(timezone=ZoneInfo("UTC"))
    with pytest.raises(ValidationError, match="inconsistent local_offset"):
        check_against_request(record, contract(), first_for_entity=True, location="f")
