"""Home Assistant history payloads, validated against the request.

``/api/history/period`` answers with one list per entity. With
``minimal_response``, rows after the first may be reduced to ``state`` and
``last_changed``. Every row becomes one :class:`HistoryRecord`; nothing is
invented for fields the selected profile omits.

Without ``skip_initial_state``, Home Assistant opens each entity's group with
the state in effect at the query start, stamped with that instant. Such a
carry-in is the only row allowed before the window start and is restamped to
it, so it belongs to the day it opens.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime

from ..errors import ValidationError
from .query import HistoryQuery
from .records import HistoryRecord, JsonValue, parse_utc_timestamp
from .window import DayWindow, offset_label

_WIRE_FIELDS = frozenset({"entity_id", "state", "last_changed", "last_updated", "attributes"})

#: Field names a stored record reserves; a wire row must not bring its own.
_RESERVED_FIELDS = frozenset({"local_offset"})

type WirePayload = Sequence[Sequence[Mapping[str, JsonValue]]]


def iter_normalized_history(
    payload: WirePayload,
    *,
    requested_entity_ids: Iterable[str],
    window: DayWindow,
    query: HistoryQuery,
) -> Iterator[HistoryRecord]:
    """Validate *payload*, the answer to a request for *window*, and yield its records."""
    requested = frozenset(requested_entity_ids)
    seen_groups: set[str] = set()

    for group_index, group in enumerate(payload, start=1):
        if not group:
            raise ValidationError(f"History group {group_index}: empty entity history group.")
        for row_index, row in enumerate(group, start=1):
            if not isinstance(row, Mapping):
                raise ValidationError(
                    f"History group {group_index}, row {row_index} must be an object."
                )
        entity_id = group[0].get("entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            raise ValidationError(
                f"History group {group_index}: first row needs a non-empty entity_id."
            )
        if entity_id not in requested:
            raise ValidationError(f"History group {group_index}: entity was not requested.")
        if entity_id in seen_groups:
            raise ValidationError(f"History group {group_index}: duplicate entity history group.")
        seen_groups.add(entity_id)

        for row_index, row in enumerate(group, start=1):
            yield _record(
                row,
                entity_id=entity_id,
                location=f"History group {group_index}, row {row_index}",
                first_in_group=row_index == 1,
                window=window,
                query=query,
            )


def _record(
    row: Mapping[str, JsonValue],
    *,
    entity_id: str,
    location: str,
    first_in_group: bool,
    window: DayWindow,
    query: HistoryQuery,
) -> HistoryRecord:
    reduced = query.minimal_response and not first_in_group and "last_updated" not in row
    explicit_entity = row.get("entity_id")
    if explicit_entity is None:
        if not reduced:
            raise ValidationError(f"{location} needs entity_id.")
    elif explicit_entity != entity_id:
        raise ValidationError(f"{location}: entity_id does not match its group.")

    state = row.get("state")
    if not isinstance(state, str):
        raise ValidationError(f"{location}: state must be a string.")

    last_changed = parse_utc_timestamp(
        row.get("last_changed"), name="last_changed", location=location
    )
    last_updated = (
        last_changed
        if reduced
        else parse_utc_timestamp(row.get("last_updated"), name="last_updated", location=location)
    )
    if last_changed > last_updated:
        raise ValidationError(f"{location}: last_changed is after last_updated.")

    if not window.contains(last_updated):
        if not _is_carry_in(last_updated, window, query, first_in_group=first_in_group):
            raise ValidationError(f"{location}: timestamp is outside the requested history window.")
        last_changed = last_updated = window.start_utc

    if "attributes" in row:
        attributes = row["attributes"]
        if attributes is not None and not isinstance(attributes, dict):
            raise ValidationError(f"{location}: attributes must be an object.")
    elif query.no_attributes or reduced:
        # The profile omits the attribute snapshot; carrying an earlier row's
        # attributes forward would invent fidelity the request did not ask for.
        attributes = None
    else:
        raise ValidationError(f"{location}: attributes are missing.")

    reserved = _RESERVED_FIELDS.intersection(row)
    if reserved:
        raise ValidationError(f"{location}: carries the reserved field {min(reserved)!r}.")

    return HistoryRecord(
        entity_id=entity_id,
        state=state,
        last_changed=last_changed,
        last_updated=last_updated,
        attributes=attributes,
        local_offset=offset_label(last_updated, window.timezone),
        extra=tuple((key, value) for key, value in row.items() if key not in _WIRE_FIELDS),
    )


def _is_carry_in(
    last_updated: datetime, window: DayWindow, query: HistoryQuery, *, first_in_group: bool
) -> bool:
    return (
        not query.skip_initial_state
        and first_in_group
        and window.query_start <= last_updated < window.start_utc
    )
