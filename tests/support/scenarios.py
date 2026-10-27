"""Export scenarios whose output is pinned by golden fixtures.

``REFERENCE`` covers a normal day and the 25-hour autumn DST day with every
format, Unicode, commas, line breaks, nested JSON and an entity without
history. The other scenarios pin the failure, partial-day and carry-in shapes.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from tests.support.harness import Failure, Response, Scenario

BERLIN = ZoneInfo("Europe/Berlin")

#: 2026-10-24 is a normal CEST day; on 2026-10-25 Europe/Berlin leaves CEST at
#: 01:00 UTC, so rows of that day carry two different offsets.
NORMAL_DAY = date(2026, 10, 24)
DST_DAY = date(2026, 10, 25)
PARTIAL_DAY = date(2026, 10, 26)

_TEMPERATURE_ATTRS = {
    "friendly_name": "Küche Temperatur — über dem Herd",
    "unit_of_measurement": "°C",
    "device_class": "temperature",
}
_NOTE_ATTRS = {
    "friendly_name": "Status note",
    "description": "line one\nline two, with a comma",
}
_DOOR_ATTRS = {"friendly_name": "Front door", "device_class": "door"}
_UNICODE_ATTRS = {
    "friendly_name": "Ünïcödé \U0001f321",
    "nested": {"values": [1, 2, None], "note": 'he said "yes"'},
}


def row(
    entity_id: str,
    state: str,
    changed: str,
    attributes: dict[str, Any],
    updated: str | None = None,
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": state,
        "last_changed": changed,
        "last_updated": updated or changed,
        "attributes": attributes,
    }


REFERENCE_ENTITY_IDS = (
    "sensor.kitchen_temperature",
    "sensor.status_note",
    "binary_sensor.front_door",
    "sensor.unicode_payload",
    "sensor.never_recorded",
)

#: One entry per history request, in call order: batches of two, two and one
#: entity for each of the two days. An empty list is a batch in which no
#: requested entity had recorded history.
_REFERENCE_RESPONSES: tuple[Response, ...] = (
    [
        [
            row(
                "sensor.kitchen_temperature",
                "21.5",
                "2026-10-24T08:30:00+00:00",
                _TEMPERATURE_ATTRS,
            ),
            row(
                "sensor.kitchen_temperature",
                "22.0",
                "2026-10-24T09:15:42.123456+00:00",
                _TEMPERATURE_ATTRS,
                updated="2026-10-24T09:15:43.500000+00:00",
            ),
        ],
        [
            row(
                "sensor.status_note",
                'Alarm, "high" level',
                "2026-10-24T10:00:00+00:00",
                _NOTE_ATTRS,
            ),
        ],
    ],
    [
        [row("binary_sensor.front_door", "on", "2026-10-24T11:00:00+00:00", _DOOR_ATTRS)],
        [row("sensor.unicode_payload", "zwölf", "2026-10-24T12:00:00+00:00", _UNICODE_ATTRS)],
    ],
    [],
    [
        [
            # Local 01:30 CEST, still +02:00.
            row(
                "sensor.kitchen_temperature",
                "19.5",
                "2026-10-24T23:30:00+00:00",
                _TEMPERATURE_ATTRS,
            ),
            # Local 02:30 CET, already +01:00.
            row(
                "sensor.kitchen_temperature",
                "18.0",
                "2026-10-25T01:30:00+00:00",
                _TEMPERATURE_ATTRS,
            ),
        ],
    ],
    [
        [
            # Changed one second before the changeover, updated two seconds
            # after it: local_offset follows last_updated, already +01:00.
            row(
                "binary_sensor.front_door",
                "off",
                "2026-10-25T00:59:59+00:00",
                _DOOR_ATTRS,
                updated="2026-10-25T01:00:01+00:00",
            ),
        ],
        [
            # The exact changeover instant.
            row(
                "sensor.unicode_payload",
                "unavailable",
                "2026-10-25T01:00:00+00:00",
                _UNICODE_ATTRS,
            ),
        ],
    ],
    [],
)

REFERENCE = Scenario(
    name="reference",
    days=(NORMAL_DAY, DST_DAY),
    responses=_REFERENCE_RESPONSES,
    entity_ids=REFERENCE_ENTITY_IDS,
    entity_count_total=7,
    formats=frozenset({"jsonl", "csv", "parquet"}),
)

_THREE_ENTITIES = (
    "sensor.kitchen_temperature",
    "sensor.status_note",
    "binary_sensor.front_door",
)

#: The second of two batches fails with an API error, so the day fails.
FAILED_DAY = Scenario(
    name="failed_day",
    days=(NORMAL_DAY,),
    responses=(
        [
            [
                row(
                    "sensor.kitchen_temperature",
                    "21.5",
                    "2026-10-24T08:30:00+00:00",
                    _TEMPERATURE_ATTRS,
                )
            ]
        ],
        Failure("api"),
    ),
    entity_ids=_THREE_ENTITIES,
    entity_count_total=3,
    formats=frozenset({"jsonl", "csv"}),
)

#: Home Assistant becomes unreachable during the first of two days.
UNREACHABLE = Scenario(
    name="unreachable",
    days=(NORMAL_DAY, DST_DAY),
    responses=(
        Failure("unreachable"),
        [[row("binary_sensor.front_door", "on", "2026-10-24T11:00:00+00:00", _DOOR_ATTRS)]],
        [],
        [[row("binary_sensor.front_door", "off", "2026-10-25T09:00:00+00:00", _DOOR_ATTRS)]],
    ),
    entity_ids=_THREE_ENTITIES,
    entity_count_total=3,
)

#: Home Assistant rejects the token on the first request; the run stops.
AUTH_ABORT = Scenario(
    name="auth_abort",
    days=(NORMAL_DAY, DST_DAY),
    responses=(Failure("auth"),),
    entity_ids=_THREE_ENTITIES,
    entity_count_total=3,
)

#: Today, exported up to 14:30:15 local time.
PARTIAL = Scenario(
    name="partial_day",
    days=(PARTIAL_DAY,),
    responses=(
        [
            [
                row(
                    "sensor.kitchen_temperature",
                    "17.0",
                    "2026-10-26T06:00:00+00:00",
                    _TEMPERATURE_ATTRS,
                )
            ]
        ],
        [],
    ),
    entity_ids=_THREE_ENTITIES,
    entity_count_total=3,
    formats=frozenset({"jsonl", "csv"}),
    partial_day=PARTIAL_DAY,
    now=datetime(2026, 10, 26, 14, 30, 15, tzinfo=BERLIN),
    today=PARTIAL_DAY,
)

#: skip_initial_state = false: Home Assistant prepends each entity's state at
#: the window start, stamped exactly at local midnight.
CARRY_IN = Scenario(
    name="carry_in",
    days=(NORMAL_DAY,),
    responses=(
        [
            [
                row(
                    "sensor.kitchen_temperature",
                    "20.0",
                    "2026-10-23T22:00:00+00:00",
                    _TEMPERATURE_ATTRS,
                ),
                row(
                    "sensor.kitchen_temperature",
                    "21.5",
                    "2026-10-24T08:30:00+00:00",
                    _TEMPERATURE_ATTRS,
                ),
            ],
            [
                row(
                    "sensor.status_note",
                    "idle",
                    "2026-10-23T22:00:00+00:00",
                    _NOTE_ATTRS,
                ),
            ],
        ],
        [[row("binary_sensor.front_door", "off", "2026-10-23T22:00:00+00:00", _DOOR_ATTRS)]],
    ),
    entity_ids=_THREE_ENTITIES,
    entity_count_total=3,
    formats=frozenset({"jsonl", "csv", "parquet"}),
    skip_initial_state=False,
)

#: /api/states of the snapshot contract: Unicode, unknown and unavailable states.
SNAPSHOT_STATES: list[dict[str, Any]] = [
    {
        "entity_id": "sensor.kitchen_temperature",
        "state": "21.5",
        "attributes": {"friendly_name": "Küche Temperatur", "unit_of_measurement": "°C"},
    },
    {"entity_id": "binary_sensor.front_door", "state": "unavailable", "attributes": {}},
    {"entity_id": "light.hall", "state": "unknown", "attributes": {"friendly_name": "Hall"}},
]

SCENARIOS: tuple[Scenario, ...] = (
    REFERENCE,
    FAILED_DAY,
    UNREACHABLE,
    AUTH_ABORT,
    PARTIAL,
    CARRY_IN,
)
