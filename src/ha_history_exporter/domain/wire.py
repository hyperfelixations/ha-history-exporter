"""Home Assistant history payloads, validated against the request.

``/api/history/period`` answers with one list per entity. With
``minimal_response``, rows after the first may be reduced to ``state`` and
``last_changed``. Every row becomes one :class:`HistoryRecord`; nothing is
invented for fields the selected profile omits.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..errors import ValidationError
from .query import HistoryQuery
from .records import HistoryRecord, JsonValue, parse_utc_timestamp
from .window import offset_label

_WIRE_FIELDS = frozenset({"entity_id", "state", "last_changed", "last_updated", "attributes"})

#: Field names a stored record reserves; a wire row must not bring its own.
_RESERVED_FIELDS = frozenset({"local_offset"})

type WirePayload = Sequence[Sequence[Mapping[str, JsonValue]]]


def iter_normalized_history(
    payload: WirePayload,
    *,
    requested_entity_ids: Iterable[str],
    start: datetime,
    end: datetime,
    query: HistoryQuery,
    timezone: ZoneInfo,
) -> Iterator[HistoryRecord]:
    """Validate *payload* and yield its records in wire order."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("History bounds must be timezone-aware.")
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    if start_utc >= end_utc:
        raise ValueError("History start must be before its end.")

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
                start=start_utc,
                end=end_utc,
                query=query,
                timezone=timezone,
            )


def _record(
    row: Mapping[str, JsonValue],
    *,
    entity_id: str,
    location: str,
    first_in_group: bool,
    start: datetime,
    end: datetime,
    query: HistoryQuery,
    timezone: ZoneInfo,
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

    carry_in = not query.skip_initial_state and first_in_group and last_updated == start
    if not (start < last_updated < end or carry_in):
        raise ValidationError(f"{location}: timestamp is outside the requested history window.")

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
        local_offset=offset_label(last_updated, timezone),
        extra=tuple((key, value) for key, value in row.items() if key not in _WIRE_FIELDS),
    )
