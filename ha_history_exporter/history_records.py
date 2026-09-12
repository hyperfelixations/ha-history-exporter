"""Canonical validation and normalisation for Home Assistant history rows.

The REST history endpoint has two wire shapes: full state objects and, when
``minimal_response`` is enabled, reduced follow-up rows.  No writer consumes
those shapes directly.  This module validates the response against the exact
request and turns both shapes into one immutable record contract first.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TypeGuard
from zoneinfo import ZoneInfo

from .errors import ValidationError
from .settings.model import HistoryRequestSettings

_WIRE_FIELDS = frozenset(
    {"entity_id", "state", "last_changed", "last_updated", "attributes"}
)
_STORAGE_FIELDS = _WIRE_FIELDS | {"local_offset"}


@dataclass(frozen=True)
class HistoryRecord:
    """One validated logical history event, independent of file format."""

    entity_id: str
    state: str
    last_changed: datetime
    last_updated: datetime
    attributes: dict[str, Any] | None
    local_offset: str
    extra: dict[str, Any] = field(default_factory=dict)

    def as_json_dict(self) -> dict[str, Any]:
        """Return the public JSONL shape while retaining unknown wire fields."""
        return {
            "entity_id": self.entity_id,
            "state": self.state,
            "last_changed": self.last_changed.isoformat(),
            "last_updated": self.last_updated.isoformat(),
            "attributes": self.attributes,
            **self.extra,
            "local_offset": self.local_offset,
        }

    def canonical_dict(self) -> dict[str, Any]:
        """Return a format-neutral shape used by cross-format digests."""
        return {
            "entity_id": self.entity_id,
            "state": self.state,
            "last_changed": self.last_changed.isoformat(),
            "last_updated": self.last_updated.isoformat(),
            "attributes": self.attributes,
            "local_offset": self.local_offset,
            "extra": self.extra,
        }


def _parse_timestamp(value: Any, field_name: str, group: int, row: int) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValidationError(
            f"History group {group}, row {row}: {field_name} must be a timestamp."
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(
            f"History group {group}, row {row}: {field_name} is not valid ISO 8601."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(
            f"History group {group}, row {row}: {field_name} needs a timezone."
        )
    return parsed.astimezone(timezone.utc)


def _offset(value: datetime, local_timezone: ZoneInfo) -> str:
    delta = value.astimezone(local_timezone).utcoffset()
    if delta is None:  # pragma: no cover - ZoneInfo always supplies an offset
        raise ValidationError("The configured timezone has no UTC offset.")
    minutes = int(delta.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def _valid_utc_offset(value: Any) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == 6
        and value[0] in "+-"
        and value[3] == ":"
        and (value[1:3] + value[4:6]).isdigit()
        and int(value[1:3]) <= 23
        and int(value[4:6]) <= 59
    )


def stored_record(
    row: Mapping[str, Any],
    *,
    location: str,
    extra: Mapping[str, Any] | None = None,
) -> HistoryRecord:
    """Validate one materialised record read from an output format."""
    entity_id = row.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id:
        raise ValidationError(f"{location}: entity_id must be a non-empty string.")
    state = row.get("state")
    if not isinstance(state, str):
        raise ValidationError(f"{location}: state must be a string.")
    last_changed = _parse_timestamp(
        row.get("last_changed"), "last_changed", 0, 0
    )
    last_updated = _parse_timestamp(
        row.get("last_updated"), "last_updated", 0, 0
    )
    if last_changed > last_updated:
        raise ValidationError(f"{location}: last_changed is after last_updated.")
    attributes = row.get("attributes")
    if attributes is not None and not isinstance(attributes, Mapping):
        raise ValidationError(f"{location}: attributes must be an object or null.")
    local_offset = row.get("local_offset")
    if not _valid_utc_offset(local_offset):
        raise ValidationError(f"{location}: local_offset is not valid.")
    extras = (
        dict(extra)
        if extra is not None
        else {key: value for key, value in row.items() if key not in _STORAGE_FIELDS}
    )
    return HistoryRecord(
        entity_id=entity_id,
        state=state,
        last_changed=last_changed,
        last_updated=last_updated,
        attributes=dict(attributes) if isinstance(attributes, Mapping) else None,
        local_offset=local_offset,
        extra=extras,
    )


def validate_stored_record(
    record: HistoryRecord,
    *,
    requested_entity_ids: frozenset[str],
    start: datetime,
    end: datetime,
    allow_start_state: bool,
    first_for_entity: bool,
    local_timezone: ZoneInfo,
) -> None:
    """Validate an output record against the request that produced it."""
    if record.entity_id not in requested_entity_ids:
        raise ValidationError("An output record belongs to an entity not requested.")
    carry_in = allow_start_state and first_for_entity and record.last_updated == start
    if not (start < record.last_updated < end or carry_in):
        raise ValidationError("An output record is outside the requested history window.")
    if record.local_offset != _offset(record.last_updated, local_timezone):
        raise ValidationError("An output record has an inconsistent local_offset.")


def iter_normalized_history(
    payload: Sequence[Sequence[Mapping[str, Any]]],
    *,
    requested_entity_ids: Iterable[str],
    start: datetime,
    end: datetime,
    settings: HistoryRequestSettings,
    local_timezone: ZoneInfo,
) -> Iterator[HistoryRecord]:
    """Validate a history payload and yield canonical records in wire order."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("History bounds must be timezone-aware.")
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    if start_utc >= end_utc:
        raise ValueError("History start must be before its end.")

    requested = frozenset(requested_entity_ids)
    seen_groups: set[str] = set()

    for group_index, group in enumerate(payload, start=1):
        if not group:
            raise ValidationError(
                f"History group {group_index}: empty entity history group."
            )
        first = group[0]
        if not isinstance(first, Mapping):
            raise ValidationError(
                f"History group {group_index}, row 1 must be an object."
            )
        entity_id = first.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            raise ValidationError(
                f"History group {group_index}: first row needs a non-empty entity_id."
            )
        if entity_id not in requested:
            raise ValidationError(
                f"History group {group_index}: entity was not requested."
            )
        if entity_id in seen_groups:
            raise ValidationError(
                f"History group {group_index}: duplicate entity history group."
            )
        seen_groups.add(entity_id)

        for row_index, row in enumerate(group, start=1):
            if not isinstance(row, Mapping):
                raise ValidationError(
                    f"History group {group_index}, row {row_index} must be an object."
                )

            explicit_entity = row.get("entity_id")
            reduced = (
                settings.minimal_response
                and row_index > 1
                and "last_updated" not in row
            )
            if explicit_entity is None:
                if not reduced:
                    raise ValidationError(
                        f"History group {group_index}, row {row_index} needs entity_id."
                    )
            elif explicit_entity != entity_id:
                raise ValidationError(
                    f"History group {group_index}, row {row_index}: entity_id "
                    "does not match its group."
                )

            state = row.get("state")
            if not isinstance(state, str):
                raise ValidationError(
                    f"History group {group_index}, row {row_index}: state must "
                    "be a string."
                )

            last_changed = _parse_timestamp(
                row.get("last_changed"), "last_changed", group_index, row_index
            )
            last_updated = (
                last_changed
                if reduced
                else _parse_timestamp(
                    row.get("last_updated"),
                    "last_updated",
                    group_index,
                    row_index,
                )
            )
            if last_changed > last_updated:
                raise ValidationError(
                    f"History group {group_index}, row {row_index}: last_changed "
                    "is after last_updated."
                )

            carry_in = (
                not settings.skip_initial_state
                and row_index == 1
                and last_updated == start_utc
            )
            if not (start_utc < last_updated < end_utc or carry_in):
                raise ValidationError(
                    f"History group {group_index}, row {row_index}: timestamp is "
                    "outside the requested history window."
                )

            if "attributes" in row:
                raw_attributes = row["attributes"]
                if raw_attributes is not None and not isinstance(raw_attributes, Mapping):
                    raise ValidationError(
                        f"History group {group_index}, row {row_index}: attributes "
                        "must be an object."
                    )
                attributes = (
                    dict(raw_attributes) if isinstance(raw_attributes, Mapping) else None
                )
            elif settings.no_attributes:
                attributes = None
            elif reduced:
                # A reduced History API row contains no attribute snapshot.
                # Reusing an earlier row's attributes would invent fidelity
                # that the selected response profile deliberately omitted.
                attributes = None
            else:
                raise ValidationError(
                    f"History group {group_index}, row {row_index}: attributes are missing."
                )

            extra = {key: value for key, value in row.items() if key not in _WIRE_FIELDS}
            yield HistoryRecord(
                entity_id=entity_id,
                state=state,
                last_changed=last_changed,
                last_updated=last_updated,
                attributes=attributes,
                local_offset=_offset(last_updated, local_timezone),
                extra=extra,
            )


def logical_digest(records: Iterable[HistoryRecord]) -> str:
    """Hash logical record values independently of file encoding details."""
    digest = hashlib.sha256()
    for record in records:
        encoded = json.dumps(
            record.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(encoded)
        digest.update(b"\n")
    return digest.hexdigest()
