"""A valid and an invalid text for every configuration key, chosen by its kind."""

from __future__ import annotations

from ha_history_exporter.settings.registry import KEYS, Key
from ha_history_exporter.settings.validators import (
    Bool,
    Formats,
    HttpUrl,
    Integer,
    Names,
    OptionalValue,
    PathValue,
    Seconds,
    SecondsList,
    Zone,
)

#: (valid text, invalid text or None when every text is valid) per kind.
_BY_KIND: dict[type, tuple[str, str | None]] = {
    HttpUrl: ("http://home-assistant.invalid:8123", "ftp://home-assistant.invalid"),
    PathValue: ("exports", "$HHE_UNDEFINED_SAMPLE_VARIABLE/exports"),
    Zone: ("America/New_York", "Mars/Olympus"),
    Formats: ("jsonl,csv", "jsonl,xml"),
    Integer: ("7", "-1"),
    Seconds: ("1.5", "-1"),
    SecondsList: ("1,2.5", "1,soon"),
    Names: ("update,button", None),
    Bool: ("false", "maybe"),
}


def _kind_type(key: Key) -> type:
    kind = key.setting.kind
    return type(kind.inner) if isinstance(kind, OptionalValue) else type(kind)


def valid_text(key: Key) -> str:
    return _BY_KIND[_kind_type(key)][0]


def invalid_text(key: Key) -> str | None:
    return _BY_KIND[_kind_type(key)][1]


def covered() -> set[type]:
    return set(_BY_KIND)


def kinds_in_use() -> set[type]:
    return {_kind_type(key) for key in KEYS}
