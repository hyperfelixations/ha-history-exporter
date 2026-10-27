"""The canonical history record shared by every output format.

A record is created once, either from the Home Assistant wire payload or from
a stored artifact, and is written to every requested format unchanged. Its
attribute object is encoded once (``attributes_json``), so JSONL, CSV and
Parquet carry byte-identical attribute text. The logical digest proves that
two artifacts hold the same records independently of their encoding. See
internal dev doc § "Record contract".
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..errors import ValidationError

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None

#: Top-level fields of a stored JSONL row that are not carried in ``extra``.
STORAGE_FIELDS = frozenset(
    {"entity_id", "state", "last_changed", "last_updated", "attributes", "local_offset"}
)


def encode_json(value: Any) -> str:
    """JSON text exactly as every HHE format stores it."""
    return json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class HistoryRecord:
    """One validated history event, independent of any file format.

    Timestamps are timezone-aware UTC datetimes. ``attributes`` is ``None``
    when the selected response profile omits attribute snapshots. ``extra``
    keeps unknown top-level wire fields in their original order.
    """

    entity_id: str
    state: str
    last_changed: datetime
    last_updated: datetime
    attributes: Mapping[str, JsonValue] | None
    local_offset: str
    extra: tuple[tuple[str, JsonValue], ...] = ()
    attributes_json: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes_json", encode_json(self.attributes))

    @property
    def extra_json(self) -> str | None:
        """The ``extra`` mapping as JSON text, or None when there is none."""
        return encode_json(dict(self.extra)) if self.extra else None


def canonical_line(record: HistoryRecord) -> bytes:
    """The record's contribution to the logical digest.

    Keys are sorted at every level, so the digest ignores attribute order and
    the physical encoding of each format.
    """
    canonical = {
        "entity_id": record.entity_id,
        "state": record.state,
        "last_changed": record.last_changed.isoformat(),
        "last_updated": record.last_updated.isoformat(),
        "attributes": record.attributes,
        "local_offset": record.local_offset,
        "extra": dict(record.extra),
    }
    text = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


class LogicalDigest:
    """SHA-256 over the canonical lines of a record sequence, with a row count."""

    def __init__(self) -> None:
        self._hash = hashlib.sha256()
        self.rows = 0

    def add(self, record: HistoryRecord) -> None:
        self._hash.update(canonical_line(record))
        self.rows += 1

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


def parse_utc_timestamp(value: object, *, name: str, location: str) -> datetime:
    """Parse an ISO 8601 timestamp with offset and normalise it to UTC."""
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{location}: {name} must be a timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{location}: {name} is not valid ISO 8601.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{location}: {name} needs a timezone.")
    return parsed.astimezone(UTC)
