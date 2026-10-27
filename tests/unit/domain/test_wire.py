from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.domain.query import HistoryQuery
from ha_history_exporter.domain.records import HistoryRecord
from ha_history_exporter.domain.window import DayWindow
from ha_history_exporter.domain.wire import iter_normalized_history
from ha_history_exporter.errors import ValidationError
from tests.helpers import state_row

BERLIN = ZoneInfo("Europe/Berlin")
WINDOW = DayWindow.full(date(2026, 7, 28), BERLIN)
START = WINDOW.start_utc
TICK = timedelta(microseconds=1)


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
            window=WINDOW,
            query=query or HistoryQuery(),
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


def test_the_window_start_and_end_are_half_open() -> None:
    at_start = state_row("sensor.one", "1", START.isoformat())
    before_end = state_row("sensor.one", "2", (WINDOW.end_utc - TICK).isoformat())
    records = normalize([[at_start, before_end]])
    assert [record.last_updated for record in records] == [START, WINDOW.end_utc - TICK]
    assert records[0].local_offset == "+02:00"

    with pytest.raises(ValidationError, match="outside"):
        normalize([[state_row("sensor.one", "3", WINDOW.end_utc.isoformat())]])


def test_a_carry_in_is_restamped_to_the_window_start() -> None:
    carry_in = state_row("sensor.one", "1", WINDOW.query_start.isoformat())
    event_at_start = state_row("sensor.one", "2", START.isoformat())

    records = normalize([[carry_in, event_at_start]], query=HistoryQuery(skip_initial_state=False))

    assert [(r.state, r.last_changed, r.last_updated) for r in records] == [
        ("1", START, START),
        ("2", START, START),
    ]
    assert records[0].local_offset == "+02:00"


@pytest.mark.parametrize(
    ("rows", "query"),
    [
        pytest.param([("1", WINDOW.query_start)], HistoryQuery(), id="not-requested"),
        pytest.param(
            [("1", START + TICK), ("2", WINDOW.query_start)],
            HistoryQuery(skip_initial_state=False),
            id="not-first",
        ),
        pytest.param(
            [("1", WINDOW.query_start - TICK)],
            HistoryQuery(skip_initial_state=False),
            id="before-the-query",
        ),
    ],
)
def test_only_a_requested_first_row_may_precede_the_window(
    rows: list[tuple[str, Any]], query: HistoryQuery
) -> None:
    payload = [[state_row("sensor.one", state, instant.isoformat()) for state, instant in rows]]
    with pytest.raises(ValidationError, match="outside"):
        normalize(payload, query=query)
