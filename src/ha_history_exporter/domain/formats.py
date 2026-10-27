"""Output formats and the format-set notation ``jsonl,csv,parquet`` / ``none``."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class Format(StrEnum):
    """An output format; its value is the file suffix."""

    JSONL = "jsonl"
    CSV = "csv"
    PARQUET = "parquet"


#: The canonical order wherever formats are listed or rendered.
FORMAT_ORDER: tuple[Format, ...] = (Format.JSONL, Format.CSV, Format.PARQUET)

#: The notation for the empty set: snapshot-only, no history request.
NONE = "none"


class FormatSetError(ValueError):
    """A format-set value that cannot be interpreted."""

    def __init__(self, problem: str, details: str) -> None:
        super().__init__(problem)
        self.problem = problem
        self.details = details


def _valid_names() -> str:
    return ", ".join(fmt.value for fmt in FORMAT_ORDER)


def parse_format_set(value: str | Iterable[str]) -> frozenset[Format]:
    """Interpret ``"jsonl,parquet"``, ``["jsonl", "csv"]`` or ``"none"``.

    Names are case-insensitive. ``none`` and an empty selection both mean the
    empty set; ``none`` cannot be combined with a format.
    """
    if isinstance(value, str):
        names = [part.strip().lower() for part in value.split(",") if part.strip()]
    else:
        names = []
        for item in value:
            if not isinstance(item, str):
                raise FormatSetError(
                    "must list output formats as strings.",
                    f"Valid formats: {_valid_names()}, or '{NONE}'.",
                )
            names.append(item.strip().lower())

    if NONE in names:
        if len(names) > 1:
            raise FormatSetError(
                f"cannot combine '{NONE}' with another format.",
                f"'{NONE}' means snapshot-only: no history request and no daily manifest.",
            )
        return frozenset()

    selected: set[Format] = set()
    for name in names:
        try:
            selected.add(Format(name))
        except ValueError:
            raise FormatSetError(
                f"does not know the output format {name!r}.",
                f"Valid formats: {_valid_names()}, or '{NONE}'.",
            ) from None
    return frozenset(selected)


def render_format_set(formats: Iterable[Format]) -> str:
    """The notation a user types for *formats*, in canonical order."""
    chosen = set(formats)
    return ",".join(fmt.value for fmt in FORMAT_ORDER if fmt in chosen) or NONE
