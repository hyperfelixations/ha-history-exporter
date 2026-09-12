from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.errors import ValidationError
from ha_history_exporter.history_records import (
    HistoryRecord,
    iter_normalized_history,
    logical_digest,
    stored_record,
)
from ha_history_exporter.settings import HistoryRequestSettings
from tests.helpers import state_row

UTC = timezone.utc
LOCAL_TZ = ZoneInfo("Europe/Berlin")
START = datetime(2026, 7, 28, tzinfo=UTC)
END = datetime(2026, 7, 29, tzinfo=UTC)


def normalize(payload, *, requested=("sensor.one",), settings=None):
    return list(
        iter_normalized_history(
            payload,
            requested_entity_ids=requested,
            start=START,
            end=END,
            settings=settings or HistoryRequestSettings(),
            local_timezone=LOCAL_TZ,
        )
    )


def test_full_history_rows_are_normalized_to_utc_and_preserve_extras():
    row = state_row(
        "sensor.one",
        timestamp="2026-07-28T12:00:00+02:00",
    )
    row["context"] = {"id": "synthetic-context"}

    records = normalize([[row]])

    assert records == [
        HistoryRecord(
            entity_id="sensor.one",
            state="21.5",
            last_changed=datetime(2026, 7, 28, 10, tzinfo=UTC),
            last_updated=datetime(2026, 7, 28, 10, tzinfo=UTC),
            attributes=row["attributes"],
            local_offset="+02:00",
            extra={"context": {"id": "synthetic-context"}},
        )
    ]
    written = records[0].as_json_dict()
    assert written["context"] == {"id": "synthetic-context"}
    assert written["last_updated"] == "2026-07-28T10:00:00+00:00"


def test_minimal_response_normalizes_reduced_and_mixed_rows():
    first = state_row("sensor.one", "1", "2026-07-28T01:00:00+00:00")
    reduced = {"state": "2", "last_changed": "2026-07-28T02:00:00+00:00"}
    full = state_row("sensor.one", "3", "2026-07-28T03:00:00+00:00")
    full["attributes"] = {"friendly_name": "Updated"}
    later_reduced = {
        "state": "4",
        "last_changed": "2026-07-28T04:00:00+00:00",
    }

    records = normalize(
        [[first, reduced, full, later_reduced]],
        settings=HistoryRequestSettings(minimal_response=True),
    )

    assert [record.entity_id for record in records] == ["sensor.one"] * 4
    assert records[1].last_changed == records[1].last_updated
    assert records[1].attributes is None
    assert records[3].attributes is None


def test_no_attributes_is_represented_as_unknown_instead_of_empty():
    row = state_row("sensor.one")
    row.pop("attributes")

    [record] = normalize(
        [[row]],
        settings=HistoryRequestSettings(no_attributes=True),
    )

    assert record.attributes is None
    assert record.as_json_dict()["attributes"] is None


def test_empty_string_state_is_preserved_as_valid_home_assistant_data():
    row = state_row("sensor.one", "")

    [wire_record] = normalize([[row]])
    stored = stored_record(
        {**wire_record.as_json_dict()},
        location="synthetic stored record",
    )

    assert wire_record.state == ""
    assert stored.state == ""


@pytest.mark.parametrize("local_offset", ["+24:00", "+02:60", "-99:99"])
def test_stored_records_reject_out_of_range_local_offsets(local_offset):
    row = {
        **state_row("sensor.one"),
        "local_offset": local_offset,
    }

    with pytest.raises(ValidationError, match="local_offset"):
        stored_record(row, location="synthetic record")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([[]], "empty entity history group"),
        (
            [[{"state": "1", "last_changed": "2026-07-28T01:00:00+00:00"}]],
            "entity_id",
        ),
        ([[state_row("sensor.foreign")]], "not requested"),
        ([[state_row("sensor.one")], [state_row("sensor.one")]], "duplicate"),
        (
            [[state_row("sensor.one"), state_row("sensor.other")]],
            "does not match",
        ),
    ],
)
def test_group_identity_violations_are_rejected(payload, message):
    with pytest.raises(ValidationError, match=message):
        normalize(payload)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"state": None}, "state"),
        ({"attributes": []}, "attributes"),
        ({"last_changed": "not-a-time"}, "last_changed"),
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
    ],
)
def test_record_contract_violations_are_rejected(change, message):
    row = state_row("sensor.one")
    row.update(change)
    with pytest.raises(ValidationError, match=message):
        normalize([[row]])


def test_start_boundary_is_only_allowed_for_the_first_carry_in_state():
    carry_in = state_row("sensor.one", "1", START.isoformat())
    settings = HistoryRequestSettings(skip_initial_state=False)

    assert normalize([[carry_in]], settings=settings)[0].last_updated == START

    with pytest.raises(ValidationError, match="outside"):
        normalize([[carry_in]])

    later_boundary = state_row("sensor.one", "2", START.isoformat())
    with pytest.raises(ValidationError, match="outside"):
        normalize([[state_row("sensor.one"), later_boundary]], settings=settings)


def test_logical_digest_changes_with_any_logical_value_but_not_mapping_order():
    first = normalize([[state_row("sensor.one")]])
    reordered = HistoryRecord(
        **{
            **first[0].__dict__,
            "attributes": dict(reversed(list(first[0].attributes.items()))),
        }
    )
    changed = HistoryRecord(**{**first[0].__dict__, "state": "different"})

    assert logical_digest(first) == logical_digest([reordered])
    assert logical_digest(first) != logical_digest([changed])
