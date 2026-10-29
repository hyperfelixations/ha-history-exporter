"""Records read back from a stored artifact.

Every reader turns its rows into :class:`HistoryRecord` through
:func:`stored_record`; :func:`check_against_request` then proves that the
record belongs to the request that produced the artifact.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..errors import ValidationError
from .records import STORAGE_FIELDS, HistoryRecord, JsonValue, parse_utc_timestamp
from .window import DayWindow, is_offset_label, offset_label


def stored_record(
    row: Mapping[str, JsonValue],
    *,
    location: str,
    extra: Mapping[str, JsonValue] | None = None,
) -> HistoryRecord:
    """Validate one stored row; *location* names the file and line or row.

    Formats that keep unknown fields in a column pass them as *extra*; for
    JSONL they are the row's own keys outside :data:`STORAGE_FIELDS`.
    """
    entity_id = row.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id:
        raise ValidationError(f"{location}: entity_id must be a non-empty string.")
    state = row.get("state")
    if not isinstance(state, str):
        raise ValidationError(f"{location}: state must be a string.")
    last_changed = parse_utc_timestamp(
        row.get("last_changed"), name="last_changed", location=location
    )
    last_updated = parse_utc_timestamp(
        row.get("last_updated"), name="last_updated", location=location
    )
    if last_changed > last_updated:
        raise ValidationError(f"{location}: last_changed is after last_updated.")
    attributes = row.get("attributes")
    if attributes is not None and not isinstance(attributes, dict):
        raise ValidationError(f"{location}: attributes must be an object or null.")
    local_offset = row.get("local_offset")
    if not is_offset_label(local_offset):
        raise ValidationError(f"{location}: local_offset is not valid.")
    pairs = (
        extra.items()
        if extra is not None
        else ((key, value) for key, value in row.items() if key not in STORAGE_FIELDS)
    )
    return HistoryRecord(
        entity_id=entity_id,
        state=state,
        last_changed=last_changed,
        last_updated=last_updated,
        attributes=attributes,
        local_offset=local_offset,
        extra=tuple(pairs),
    )


@dataclass(frozen=True)
class RecordContract:
    """The request a stored artifact must match.

    The requested entities are known while a day is captured; a day read back
    from the archive later is checked against its window only.
    """

    window: DayWindow
    requested_entity_ids: frozenset[str] | None = None


def check_against_request(
    record: HistoryRecord, contract: RecordContract, *, location: str
) -> None:
    """Reject a stored record that the request could not have produced."""
    requested = contract.requested_entity_ids
    if requested is not None and record.entity_id not in requested:
        raise ValidationError(f"{location}: the record belongs to an entity not requested.")
    if not contract.window.contains(record.last_updated):
        raise ValidationError(f"{location}: the record is outside the requested history window.")
    if record.local_offset != offset_label(record.last_updated, contract.window.timezone):
        raise ValidationError(f"{location}: the record has an inconsistent local_offset.")
