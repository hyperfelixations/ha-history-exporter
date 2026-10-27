from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.domain.snapshot import (
    EntitySelection,
    select_entities,
    snapshot_document,
)

CREATED = datetime(2026, 10, 16, 10, 44, 6, 900, tzinfo=ZoneInfo("Europe/Berlin"))


def states() -> list[dict[str, Any]]:
    return [
        {"entity_id": "sensor.zeta", "state": "unknown", "attributes": {"friendly_name": "Zeta"}},
        {"entity_id": "binary_sensor.alpha", "state": "unavailable", "attributes": None},
        {"entity_id": "sensor.beta", "state": "12", "attributes": {"friendly_name": "Beta"}},
    ]


def test_snapshot_sorts_counts_and_names_every_entity() -> None:
    snapshot = snapshot_document(states(), CREATED)
    assert snapshot["created_at"] == "2026-10-16T10:44:06+02:00"
    assert snapshot["source"] == "/api/states"
    assert (snapshot["entity_count"], snapshot["entity_count_unknown"]) == (3, 1)
    assert snapshot["entity_count_unavailable"] == 1
    assert [entity["entity_id"] for entity in snapshot["entities"]] == [
        "binary_sensor.alpha",
        "sensor.beta",
        "sensor.zeta",
    ]
    assert snapshot["entities"][0] == {
        "entity_id": "binary_sensor.alpha",
        "domain": "binary_sensor",
        "state_at_snapshot": "unavailable",
        "friendly_name": "",
    }


@pytest.mark.parametrize("bad", [{"state": "1"}, {"entity_id": "", "state": "1"}, {"entity_id": 3}])
def test_states_without_a_usable_entity_id_are_skipped_everywhere(bad: dict[str, Any]) -> None:
    assert snapshot_document([*states(), bad], CREATED)["entity_count"] == 3
    assert len(select_entities([*states(), bad], EntitySelection()).entity_ids) == 3


def test_non_object_attributes_give_an_empty_friendly_name() -> None:
    snapshot = snapshot_document([{"entity_id": "sensor.a", "attributes": ["x"]}], CREATED)
    assert snapshot["entities"][0]["friendly_name"] == ""


def test_selection_is_sorted_and_unique() -> None:
    selected = select_entities(
        [*states(), {"entity_id": "sensor.beta", "state": "13"}], EntitySelection()
    )
    assert selected.entity_ids == ("binary_sensor.alpha", "sensor.beta", "sensor.zeta")
    assert (selected.candidate_count, selected.excluded_count) == (3, 0)


@pytest.mark.parametrize(
    ("unknown", "unavailable", "expected"),
    [
        (True, True, ("binary_sensor.alpha", "sensor.beta", "sensor.zeta")),
        (False, True, ("binary_sensor.alpha", "sensor.beta")),
        (True, False, ("sensor.beta", "sensor.zeta")),
        (False, False, ("sensor.beta",)),
    ],
)
def test_state_filters_narrow_the_request_not_the_snapshot(
    unknown: bool, unavailable: bool, expected: tuple[str, ...]
) -> None:
    selection = EntitySelection(include_unknown=unknown, include_unavailable=unavailable)
    selected = select_entities(states(), selection)
    assert selected.entity_ids == expected
    assert selected.candidate_count == len(expected)
    assert snapshot_document(states(), CREATED)["entity_count"] == 3


def test_domain_and_pattern_excludes_are_counted() -> None:
    entities = [
        {"entity_id": entity_id, "state": "1"}
        for entity_id in ("binary_sensor.window", "sensor.keep", "sensor.room_debug", "update.core")
    ]
    selected = select_entities(
        entities, EntitySelection(exclude_domains=("update",), exclude_patterns=("sensor.*_debug",))
    )
    assert selected.entity_ids == ("binary_sensor.window", "sensor.keep")
    assert (selected.candidate_count, selected.excluded_count) == (4, 2)


def test_patterns_are_case_sensitive_on_every_platform() -> None:
    selected = select_entities(
        [{"entity_id": "sensor.room_debug", "state": "1"}],
        EntitySelection(exclude_patterns=("sensor.*_DEBUG",)),
    )
    assert selected.entity_ids == ("sensor.room_debug",)
