from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.domain.query import HistoryQuery
from ha_history_exporter.domain.records import HistoryRecord
from ha_history_exporter.domain.wire import iter_normalized_history
from ha_history_exporter.errors import ValidationError
from tests.helpers import state_row

BERLIN = ZoneInfo("Europe/Berlin")
START = datetime(2026, 7, 28, tzinfo=UTC)
END = datetime(2026, 7, 29, tzinfo=UTC)


def normalize(
    payload: list[list[dict[str, Any]]],
    *,
    requested: tuple[str, ...] = ("sensor.one",),
    query: HistoryQuery | None = None,
) -> list[HistoryRecord]:
    return list(
        iter_normalized_history(
            payload,
            requested_entity_ids=requested,
            start=START,
            end=END,
            query=query or HistoryQuery(),
            timezone=BERLIN,
        )
    )


def test_full_rows_are_normalised_to_utc_and_keep_unknown_fields() -> None:
    row = state_row("sensor.one", timestamp="2026-07-28T12:00:00+02:00")
    row["context"] = {"id": "synthetic-context"}

    [record] = normalize([[row]])

    assert record == HistoryRecord(
        entity_id="sensor.one",
        state="21.5",
        last_changed=datetime(2026, 7, 28, 10, tzinfo=UTC),
        last_updated=datetime(2026, 7, 28, 10, tzinfo=UTC),
        attributes=row["attributes"],
        local_offset="+02:00",
        extra=(("context", {"id": "synthetic-context"}),),
    )


def test_minimal_response_rows_are_completed_without_inventing_attributes() -> None:
    first = state_row("sensor.one", "1", "2026-07-28T01:00:00+00:00")
    reduced = {"state": "2", "last_changed": "2026-07-28T02:00:00+00:00"}
    full = state_row("sensor.one", "3", "2026-07-28T03:00:00+00:00")
    later_reduced = {"state": "4", "last_changed": "2026-07-28T04:00:00+00:00"}

    records = normalize(
        [[first, reduced, full, later_reduced]],
        query=HistoryQuery(minimal_response=True),
    )

    assert [record.entity_id for record in records] == ["sensor.one"] * 4
    assert records[1].last_changed == records[1].last_updated
    assert records[1].attributes is None
    assert records[2].attributes == full["attributes"]
    assert records[3].attributes is None


def test_no_attributes_is_recorded_as_null_not_as_an_empty_object() -> None:
    row = state_row("sensor.one")
    row.pop("attributes")

    [record] = normalize([[row]], query=HistoryQuery(no_attributes=True))

    assert record.attributes is None
    assert record.attributes_json == "null"


def test_an_empty_state_string_is_valid_home_assistant_data() -> None:
    [record] = normalize([[state_row("sensor.one", "")]])
    assert record.state == ""


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([[]], "empty entity history group"),
        ([["not an object"]], "row 1 must be an object"),
        ([[{"state": "1", "last_changed": "2026-07-28T01:00:00+00:00"}]], "entity_id"),
        ([[state_row("sensor.foreign")]], "not requested"),
        ([[state_row("sensor.one")], [state_row("sensor.one")]], "duplicate"),
        ([[state_row("sensor.one"), state_row("sensor.other")]], "does not match"),
    ],
)
def test_group_identity_violations_are_rejected(
    payload: list[list[dict[str, Any]]], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        normalize(payload)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"state": None}, "state"),
        ({"attributes": []}, "attributes"),
        ({"last_changed": "not-a-time"}, "row 1: last_changed is not valid ISO 8601"),
        ({"last_updated": "2026-07-28T10:00:00"}, "timezone"),
        (
            {
                "last_changed": "2026-07-28T11:00:00+00:00",
                "last_updated": "2026-07-28T10:00:00+00:00",
            },
            "after last_updated",
        ),
        (
            {
                "last_changed": "2026-07-29T00:00:00+00:00",
                "last_updated": "2026-07-29T00:00:00+00:00",
            },
            "outside",
        ),
        ({"local_offset": "+02:00"}, "reserved field 'local_offset'"),
    ],
)
def test_record_contract_violations_are_rejected(change: dict[str, Any], message: str) -> None:
    row = state_row("sensor.one")
    row.update(change)
    with pytest.raises(ValidationError, match=message):
        normalize([[row]])


def test_missing_attributes_are_rejected_for_a_full_profile() -> None:
    row = state_row("sensor.one")
    row.pop("attributes")
    with pytest.raises(ValidationError, match="attributes are missing"):
        normalize([[row]])


def test_the_start_instant_is_only_allowed_for_the_first_carry_in_state() -> None:
    carry_in = state_row("sensor.one", "1", START.isoformat())
    with_carry_in = HistoryQuery(skip_initial_state=False)

    assert normalize([[carry_in]], query=with_carry_in)[0].last_updated == START

    with pytest.raises(ValidationError, match="outside"):
        normalize([[carry_in]])

    later_boundary = state_row("sensor.one", "2", START.isoformat())
    with pytest.raises(ValidationError, match="outside"):
        normalize([[state_row("sensor.one"), later_boundary]], query=with_carry_in)


def test_bounds_must_be_aware_and_ordered() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        list(
            iter_normalized_history(
                [],
                requested_entity_ids=[],
                start=datetime(2026, 7, 28),
                end=END,
                query=HistoryQuery(),
                timezone=BERLIN,
            )
        )
    with pytest.raises(ValueError, match="before its end"):
        list(
            iter_normalized_history(
                [],
                requested_entity_ids=[],
                start=END,
                end=START,
                query=HistoryQuery(),
                timezone=BERLIN,
            )
        )
