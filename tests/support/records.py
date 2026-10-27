"""Record factories and Hypothesis strategies for format and domain tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from hypothesis import strategies as st

from ha_history_exporter.domain.records import HistoryRecord, JsonValue
from ha_history_exporter.domain.window import offset_label

BERLIN = ZoneInfo("Europe/Berlin")
DAY_START = datetime(2026, 7, 27, 22, tzinfo=UTC)
DAY_END = datetime(2026, 7, 28, 22, tzinfo=UTC)


_DEFAULT_ATTRIBUTES: dict[str, JsonValue] = {"friendly_name": "One", "unit_of_measurement": "°C"}
_UNSET: Any = object()


def make_record(
    entity_id: str = "sensor.one",
    state: str = "21.5",
    updated: datetime = datetime(2026, 7, 28, 10, tzinfo=UTC),
    *,
    changed: datetime | None = None,
    attributes: dict[str, JsonValue] | None = _UNSET,
    extra: tuple[tuple[str, JsonValue], ...] = (),
    timezone: ZoneInfo = BERLIN,
) -> HistoryRecord:
    """A valid record; attributes default to a small object, ``None`` omits them."""
    return HistoryRecord(
        entity_id=entity_id,
        state=state,
        last_changed=changed or updated,
        last_updated=updated,
        attributes=dict(_DEFAULT_ATTRIBUTES) if attributes is _UNSET else attributes,
        local_offset=offset_label(updated, timezone),
        extra=extra,
    )


_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    max_size=12,
)
_json_scalars = st.none() | st.booleans() | st.integers(-(2**53), 2**53) | _text
json_values: st.SearchStrategy[Any] = st.recursive(
    _json_scalars,
    lambda children: st.lists(children, max_size=3) | st.dictionaries(_text, children, max_size=3),
    max_leaves=8,
)
_entity_ids = st.from_regex(r"[a-z_]{1,8}\.[a-z0-9_]{1,10}", fullmatch=True)
_reserved = frozenset(
    {"entity_id", "state", "last_changed", "last_updated", "attributes", "local_offset"}
)
_extra_keys = _text.filter(lambda key: key not in _reserved)


@st.composite
def records(draw: st.DrawFn) -> HistoryRecord:
    """Any valid record inside the reference day of :data:`DAY_START`."""
    seconds = draw(st.integers(1, int((DAY_END - DAY_START).total_seconds()) - 1))
    micro = draw(st.integers(0, 999_999))
    updated = DAY_START + timedelta(seconds=seconds, microseconds=micro)
    changed = updated - timedelta(seconds=draw(st.integers(0, 3600)))
    attributes = draw(st.none() | st.dictionaries(_text, json_values, max_size=4))
    extra = tuple(draw(st.dictionaries(_extra_keys, json_values, max_size=2)).items())
    return make_record(
        draw(_entity_ids),
        draw(_text),
        updated,
        changed=changed,
        attributes=attributes,
        extra=extra,
    )
