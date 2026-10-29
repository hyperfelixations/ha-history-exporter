"""The entity snapshot and the selection of entities whose history is requested.

The snapshot lists every entity of ``/api/states`` at export time; it
documents what existed, not what was requested. A state object without a
usable ``entity_id`` is skipped everywhere.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase
from typing import Any

from .window import format_iso


def _entity_id(state: Mapping[str, Any]) -> str | None:
    value = state.get("entity_id")
    return value if isinstance(value, str) and value else None


def snapshot_document(
    states: Sequence[Mapping[str, Any]], created_local: datetime
) -> dict[str, Any]:
    """The snapshot file content for *states*, sorted by entity ID."""
    entities = []
    for state in states:
        entity_id = _entity_id(state)
        if entity_id is None:
            continue
        attributes = state.get("attributes")
        friendly_name = attributes.get("friendly_name", "") if isinstance(attributes, dict) else ""
        entities.append(
            {
                "entity_id": entity_id,
                "domain": entity_id.split(".")[0] if "." in entity_id else "",
                "state_at_snapshot": state.get("state", ""),
                "friendly_name": friendly_name,
            }
        )
    entities.sort(key=lambda entity: entity["entity_id"])
    return {
        "created_at": format_iso(created_local),
        "source": "/api/states",
        "entity_count": len(entities),
        "entity_count_unknown": sum(1 for e in entities if e["state_at_snapshot"] == "unknown"),
        "entity_count_unavailable": sum(
            1 for e in entities if e["state_at_snapshot"] == "unavailable"
        ),
        "entities": entities,
    }


@dataclass(frozen=True)
class EntitySelection:
    include_unknown: bool = True
    include_unavailable: bool = True
    exclude_domains: tuple[str, ...] = ()
    #: Case-sensitive glob patterns on the entity ID.
    exclude_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class SelectedEntities:
    entity_ids: tuple[str, ...]
    #: Entities left after the state filters, before domain and pattern excludes.
    candidate_count: int
    excluded_count: int


def select_entities(
    states: Sequence[Mapping[str, Any]], selection: EntitySelection
) -> SelectedEntities:
    skipped_states = set()
    if not selection.include_unknown:
        skipped_states.add("unknown")
    if not selection.include_unavailable:
        skipped_states.add("unavailable")
    candidates = sorted(
        {
            entity_id
            for state in states
            if (entity_id := _entity_id(state)) is not None
            and state.get("state") not in skipped_states
        }
    )
    domains = set(selection.exclude_domains)
    selected = tuple(
        entity_id
        for entity_id in candidates
        if entity_id.split(".")[0] not in domains
        and not any(fnmatchcase(entity_id, pattern) for pattern in selection.exclude_patterns)
    )
    return SelectedEntities(
        entity_ids=selected,
        candidate_count=len(candidates),
        excluded_count=len(candidates) - len(selected),
    )
