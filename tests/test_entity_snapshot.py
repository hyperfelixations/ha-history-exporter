from __future__ import annotations

import json
from zoneinfo import ZoneInfo

from ha_history_exporter.entity_snapshot import (
    apply_optional_excludes,
    build_snapshot,
    extract_entity_ids,
    save_snapshot,
)


BERLIN = ZoneInfo("Europe/Berlin")


def synthetic_states():
    return [
        {
            "entity_id": "sensor.zeta",
            "state": "unknown",
            "attributes": {"friendly_name": "Zeta"},
        },
        {
            "entity_id": "binary_sensor.alpha",
            "state": "unavailable",
            "attributes": None,
        },
        {
            "entity_id": "sensor.beta",
            "state": "12",
            "attributes": {"friendly_name": "Beta"},
        },
    ]


def test_build_snapshot_sorts_and_counts_entities():
    snapshot = build_snapshot(synthetic_states(), BERLIN)

    assert snapshot["source"] == "/api/states"
    assert snapshot["entity_count"] == 3
    assert snapshot["entity_count_unknown"] == 1
    assert snapshot["entity_count_unavailable"] == 1
    assert [item["entity_id"] for item in snapshot["entities"]] == [
        "binary_sensor.alpha",
        "sensor.beta",
        "sensor.zeta",
    ]
    assert snapshot["entities"][0]["domain"] == "binary_sensor"
    assert snapshot["entities"][0]["friendly_name"] == ""


def test_extract_entity_ids_is_sorted_unique_and_ignores_missing_ids():
    states = synthetic_states() + [
        {"entity_id": "sensor.beta", "state": "13"},
        {"state": "missing id"},
    ]
    assert extract_entity_ids(states) == [
        "binary_sensor.alpha",
        "sensor.beta",
        "sensor.zeta",
    ]


def test_optional_excludes_apply_domains_and_globs():
    entity_ids = [
        "binary_sensor.window",
        "sensor.keep",
        "sensor.room_debug",
        "update.core",
    ]

    filtered, count = apply_optional_excludes(
        entity_ids,
        exclude_domains=["update"],
        exclude_patterns=["sensor.*_debug"],
    )

    assert filtered == ["binary_sensor.window", "sensor.keep"]
    assert count == 2


def test_save_snapshot_writes_atomic_json(tmp_path):
    snapshot = build_snapshot(synthetic_states(), BERLIN)

    path = save_snapshot(snapshot, tmp_path)

    assert path.parent == tmp_path
    assert path.name.startswith("entity_snapshot_")
    assert json.loads(path.read_text(encoding="utf-8")) == snapshot
    assert list(tmp_path.glob("*.tmp")) == []


def test_extract_entity_ids_includes_unknown_and_unavailable_by_default():
    assert extract_entity_ids(synthetic_states()) == [
        "binary_sensor.alpha",
        "sensor.beta",
        "sensor.zeta",
    ]


def test_extract_entity_ids_can_skip_unknown_entities():
    assert extract_entity_ids(synthetic_states(), include_unknown=False) == [
        "binary_sensor.alpha",
        "sensor.beta",
    ]


def test_extract_entity_ids_can_skip_unavailable_entities():
    assert extract_entity_ids(synthetic_states(), include_unavailable=False) == [
        "sensor.beta",
        "sensor.zeta",
    ]


def test_extract_entity_ids_can_skip_both():
    assert extract_entity_ids(
        synthetic_states(), include_unknown=False, include_unavailable=False
    ) == ["sensor.beta"]


def test_snapshot_stays_complete_even_when_history_selection_is_narrowed():
    """The snapshot documents every entity; only the request list narrows."""
    states = synthetic_states()
    snapshot = build_snapshot(states, BERLIN)

    assert snapshot["entity_count"] == 3
    assert len(extract_entity_ids(states, include_unknown=False)) == 2
