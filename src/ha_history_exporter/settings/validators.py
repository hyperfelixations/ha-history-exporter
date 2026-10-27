"""Value kinds of configuration keys: parsing, validation and rendering.

Every key has exactly one kind. A kind takes a value from a TOML document
(native TOML types) or from text (an environment variable, a command-line
option, ``config set``), returns the typed runtime value, and renders it back
for a TOML document and for display. See internal dev doc § "Configuration
registry".
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain.formats import (
    FORMAT_ORDER,
    Format,
    FormatSetError,
    parse_format_set,
    render_format_set,
)

type TomlValue = str | int | float | bool | list[TomlValue]

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


class InvalidValue(Exception):
    """A value a kind rejects; the caller names the key and the source."""

    def __init__(self, problem: str, *, details: str | None = None) -> None:
        super().__init__(problem)
        self.problem = problem
        self.details = details


@dataclass(frozen=True)
class ParseContext:
    """What a value may refer to: relative paths and environment variables."""

    base_dir: Path
    environ: Mapping[str, str]


class Kind[T](Protocol):
    @property
    def type_name(self) -> str:
        """How the documentation names the expected value."""
        ...

    @property
    def echo(self) -> bool:
        """Whether an invalid value may be quoted back in an error message."""
        ...

    def from_toml(self, value: object, ctx: ParseContext) -> T: ...

    def from_text(self, raw: str, ctx: ParseContext) -> T: ...

    def to_toml(self, value: T) -> TomlValue: ...

    def display(self, value: T) -> str: ...


def _type_error(expected: str, value: object) -> InvalidValue:
    return InvalidValue(f"must be {expected}, not {_toml_type(value)}")


def _toml_type(value: object) -> str:
    match value:
        case bool():
            return "a boolean"
        case int():
            return "an integer"
        case float():
            return "a number"
        case str():
            return "a string"
        case list():
            return "a list"
        case dict():
            return "a table"
        case _:
            return type(value).__name__


class HttpUrl:
    """An http or https base URL without credentials, path, query or fragment.

    The value is never quoted back: a mistyped URL may carry a password.
    """

    type_name = "http(s) base URL"
    echo = False

    def from_toml(self, value: object, ctx: ParseContext) -> str:
        if not isinstance(value, str):
            raise _type_error("a string", value)
        return self.from_text(value, ctx)

    def from_text(self, raw: str, ctx: ParseContext) -> str:
        value = raw.strip()
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise self._invalid() from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise self._invalid()
        return value.rstrip("/")

    @staticmethod
    def _invalid() -> InvalidValue:
        return InvalidValue(
            "is not a valid Home Assistant base URL",
            details=(
                "Use an http:// or https:// base URL with a host and an optional port, "
                "for example http://homeassistant.local:8123. User information, paths, "
                "query strings and fragments are not accepted. The value is not shown."
            ),
        )

    def to_toml(self, value: str) -> TomlValue:
        return value

    def display(self, value: str) -> str:
        return value


class Zone:
    """An IANA time zone name."""

    type_name = "IANA time zone"
    echo = True

    def from_toml(self, value: object, ctx: ParseContext) -> ZoneInfo:
        if not isinstance(value, str):
            raise _type_error("a string", value)
        return self.from_text(value, ctx)

    def from_text(self, raw: str, ctx: ParseContext) -> ZoneInfo:
        name = raw.strip()
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise InvalidValue(
                "is not a known IANA time zone",
                details="Use a name such as Europe/Berlin, America/New_York or UTC.",
            ) from exc

    def to_toml(self, value: ZoneInfo) -> TomlValue:
        return value.key

    def display(self, value: ZoneInfo) -> str:
        return value.key


_ENV_REFERENCE = re.compile(r"\$\{(?P<braced>[^}]*)\}|\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)")
_WINDOWS_ENV_REFERENCE = re.compile(r"%(?P<name>[^%]+)%")


def expand_path(raw: str, ctx: ParseContext) -> Path:
    """An absolute, normalised path from user text.

    ``~`` stands for the home directory, ``$NAME`` and ``${NAME}`` (and on
    Windows ``%NAME%``) for environment variables; an undefined variable is an
    error rather than a literal part of the path. A relative result is taken
    relative to *ctx.base_dir*. Links are not resolved.
    """
    text = raw.strip()
    if not text:
        raise InvalidValue("must not be empty")

    def lookup(name: str) -> str:
        if not name or name not in ctx.environ:
            raise InvalidValue(
                f"refers to the undefined environment variable {name or '(empty)'}",
                details="Define the variable, or write the path out in full.",
            )
        return ctx.environ[name]

    text = _ENV_REFERENCE.sub(
        lambda match: lookup(match.group("braced") or match.group("plain")), text
    )
    if os.name == "nt":
        text = _WINDOWS_ENV_REFERENCE.sub(lambda match: lookup(match.group("name")), text)
    if text == "~" or text.startswith(("~/", "~\\")):
        text = str(Path.home()) + text[1:]
    elif text.startswith("~"):
        raise InvalidValue("may start with ~ only for the own home directory")
    path = Path(text)
    if not path.is_absolute():
        path = ctx.base_dir / path
    return Path(os.path.normpath(path))


class PathValue:
    type_name = "path"
    echo = True

    def from_toml(self, value: object, ctx: ParseContext) -> Path:
        if not isinstance(value, str):
            raise _type_error("a string", value)
        return expand_path(value, ctx)

    def from_text(self, raw: str, ctx: ParseContext) -> Path:
        return expand_path(raw, ctx)

    def to_toml(self, value: Path) -> TomlValue:
        return str(value)

    def display(self, value: Path) -> str:
        return str(value)


class Bool:
    type_name = "true or false"
    echo = True

    def from_toml(self, value: object, ctx: ParseContext) -> bool:
        if not isinstance(value, bool):
            raise _type_error("true or false", value)
        return value

    def from_text(self, raw: str, ctx: ParseContext) -> bool:
        lowered = raw.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise InvalidValue("must be true or false")

    def to_toml(self, value: bool) -> TomlValue:
        return value

    def display(self, value: bool) -> str:
        return "true" if value else "false"


@dataclass(frozen=True)
class Integer:
    minimum: int

    @property
    def type_name(self) -> str:
        return f"integer of at least {self.minimum}"

    echo = True

    def _check(self, value: int) -> int:
        if value < self.minimum:
            raise InvalidValue(f"must be an integer of at least {self.minimum}")
        return value

    def from_toml(self, value: object, ctx: ParseContext) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _type_error("an integer", value)
        return self._check(value)

    def from_text(self, raw: str, ctx: ParseContext) -> int:
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise InvalidValue(f"must be an integer of at least {self.minimum}") from exc
        return self._check(value)

    def to_toml(self, value: int) -> TomlValue:
        return value

    def display(self, value: int) -> str:
        return str(value)


class Seconds:
    """A finite, non-negative number of seconds."""

    type_name = "number of at least 0"
    echo = True

    @staticmethod
    def _check(value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise InvalidValue("must be a finite number of at least 0")
        return value

    def from_toml(self, value: object, ctx: ParseContext) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _type_error("a number", value)
        return self._check(float(value))

    def from_text(self, raw: str, ctx: ParseContext) -> float:
        try:
            value = float(raw.strip())
        except ValueError as exc:
            raise InvalidValue("must be a finite number of at least 0") from exc
        return self._check(value)

    def to_toml(self, value: float) -> TomlValue:
        return int(value) if value.is_integer() else value

    def display(self, value: float) -> str:
        return format(value, "g")


@dataclass(frozen=True)
class OptionalValue[T]:
    """A value that may be absent; absence is ``None`` and is never written."""

    inner: Kind[T]

    @property
    def type_name(self) -> str:
        return self.inner.type_name

    @property
    def echo(self) -> bool:
        return self.inner.echo

    def from_toml(self, value: object, ctx: ParseContext) -> T | None:
        return self.inner.from_toml(value, ctx)

    def from_text(self, raw: str, ctx: ParseContext) -> T | None:
        return None if not raw.strip() else self.inner.from_text(raw, ctx)

    def to_toml(self, value: T | None) -> TomlValue:
        if value is None:
            raise ValueError("an absent value has no TOML form")
        return self.inner.to_toml(value)

    def display(self, value: T | None) -> str:
        return "(not set)" if value is None else self.inner.display(value)


def _split(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


class Names:
    """A list of non-empty strings; as text, separated by commas."""

    type_name = "list of strings"
    echo = True

    def from_toml(self, value: object, ctx: ParseContext) -> tuple[str, ...]:
        if not isinstance(value, list):
            raise _type_error("a list of strings", value)
        items: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise InvalidValue("must be a list of non-empty strings")
            items.append(item.strip())
        return tuple(items)

    def from_text(self, raw: str, ctx: ParseContext) -> tuple[str, ...]:
        return tuple(_split(raw))

    def to_toml(self, value: tuple[str, ...]) -> TomlValue:
        return list(value)

    def display(self, value: tuple[str, ...]) -> str:
        return ",".join(value)


class SecondsList:
    """Wait times in seconds; as text, separated by commas."""

    type_name = "list of numbers of at least 0"
    echo = True

    def from_toml(self, value: object, ctx: ParseContext) -> tuple[float, ...]:
        if not isinstance(value, list):
            raise _type_error("a list of numbers", value)
        return tuple(Seconds().from_toml(item, ctx) for item in value)

    def from_text(self, raw: str, ctx: ParseContext) -> tuple[float, ...]:
        return tuple(Seconds().from_text(item, ctx) for item in _split(raw))

    def to_toml(self, value: tuple[float, ...]) -> TomlValue:
        return [Seconds().to_toml(item) for item in value]

    def display(self, value: tuple[float, ...]) -> str:
        return ",".join(Seconds().display(item) for item in value)


class Formats:
    """The complete set of output formats; empty means snapshot only."""

    type_name = "list of jsonl, csv, parquet"
    echo = True

    @staticmethod
    def _parse(value: str | Sequence[str]) -> frozenset[Format]:
        try:
            return parse_format_set(value)
        except FormatSetError as exc:
            raise InvalidValue(exc.problem.rstrip("."), details=exc.details) from exc

    def from_toml(self, value: object, ctx: ParseContext) -> frozenset[Format]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise InvalidValue(
                "must be a list of output formats",
                details=f"Valid formats: {', '.join(FORMAT_ORDER)}; [] for snapshot only.",
            )
        if "none" in value:
            raise InvalidValue(
                "must not contain none", details="An empty list [] means snapshot only."
            )
        return self._parse(value)

    def from_text(self, raw: str, ctx: ParseContext) -> frozenset[Format]:
        return self._parse(raw)

    def to_toml(self, value: frozenset[Format]) -> TomlValue:
        return [fmt.value for fmt in FORMAT_ORDER if fmt in value]

    def display(self, value: frozenset[Format]) -> str:
        return render_format_set(value)
