"""Configuration sources: the TOML document, HHE_* variables, command-line options.

Each source yields typed values per key path together with their origin. A
value in the document resolves relative paths against the document's
directory, a variable or an option against the working directory. Errors name
the key and the source and quote the rejected value only when its kind allows
it. See internal dev doc § "Configuration registry".
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ConfigError, Remedy
from .registry import BY_ENV, BY_PATH, SECTIONS, TOKEN_KEY, Key, suggest
from .validators import InvalidValue, ParseContext, TomlValue

_POSITION = re.compile(r"\s*\(at line (?P<line>\d+), column (?P<column>\d+)\)$")
_AT_END = re.compile(r"\s*\(at end of document\)$")


@dataclass(frozen=True)
class Origin:
    """Where a value comes from: ``default``, ``file``, ``env`` or ``cli``."""

    source: str
    detail: str | None = None

    def __str__(self) -> str:
        return self.source if self.detail is None else f"{self.source}:{self.detail}"


DEFAULT = Origin("default")


@dataclass(frozen=True)
class Entry:
    value: Any
    origin: Origin


@dataclass(frozen=True)
class Position:
    line: int
    column: int | None = None

    def __str__(self) -> str:
        return f"line {self.line}" + (f", column {self.column}" if self.column else "")


def toml_error_position(exc: tomllib.TOMLDecodeError, text: str) -> tuple[str, Position]:
    """The parser's problem statement and where it occurred."""
    message = str(exc)
    if match := _POSITION.search(message):
        position = Position(int(match["line"]), int(match["column"]))
        return message[: match.start()], position
    if match := _AT_END.search(message):
        return message[: match.start()], Position(text.count("\n") + 1)
    return message, Position(text.count("\n") + 1)


def parse_toml(text: str, path: Path) -> dict[str, Any]:
    """Parse a TOML document; an error names the line and the column."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        problem, position = toml_error_position(exc, text)
        raise ConfigError(
            f"{path.name} is not valid TOML ({position}).",
            details=f"{problem}.",
            context={"config_file": str(path)},
        ) from None


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ConfigError(
            f"{path.name} is not UTF-8 text.", context={"config_file": str(path)}
        ) from None
    except OSError as exc:
        raise ConfigError(
            f"Cannot read {path.name} ({exc.strerror or type(exc).__name__}).",
            context={"config_file": str(path)},
        ) from None


def invalid(key: Key, source: str, error: InvalidValue, raw: object = None) -> ConfigError:
    """The error for a value of *key* from *source* that its kind rejects."""
    shown = ""
    if key.setting.kind.echo and isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
        shown = f" (got {raw!r})"
    return ConfigError(
        f"{key.path} from {source} {error.problem}{shown}.",
        details=error.details or f"{key.doc} Expected: {key.setting.kind.type_name}.",
        remedies=(
            Remedy(
                "Show every effective value and where it comes from:", "hhe config list --origin"
            ),
        ),
    )


def _locate(text: str, section: str, name: str | None) -> int | None:
    """The line that declares *section* or *section.name*; best effort, for messages."""
    current: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if header := re.fullmatch(r"\[\s*([^\]]+?)\s*\]\s*(#.*)?", stripped):
            current = header.group(1).strip().strip("\"'")
            if name is None and current == section:
                return number
            continue
        assigned = re.match(r"([A-Za-z0-9_.\-\"' ]+?)\s*=", stripped)
        if not assigned:
            continue
        dotted = assigned.group(1).replace('"', "").replace("'", "").replace(" ", "")
        full = dotted if current is None else f"{current}.{dotted}"
        if full == (section if name is None else f"{section}.{name}"):
            return number
    return None


def _unknown(path: Path, text: str, dotted: str, section: str, name: str | None) -> ConfigError:
    line = _locate(text, section, name)
    where = path.name if line is None else f"{path.name}, line {line}"
    details = f"'{dotted}' is not a configuration key."
    if (close := suggest(dotted)) is not None:
        details += f" The closest key is '{close}'."
    return ConfigError(
        f"Unknown configuration key '{dotted}' in {where}.",
        details=details,
        remedies=(Remedy("List every key with its effective value:", "hhe config list"),),
        context={"config_file": str(path)},
    )


def document_values(data: Mapping[str, Any], path: Path, text: str) -> dict[str, TomlValue]:
    """The key values a parsed document sets, checked for structure only."""
    values: dict[str, TomlValue] = {}
    for section, content in data.items():
        if section not in SECTIONS:
            raise _unknown(path, text, section, section, None)
        if not isinstance(content, dict):
            raise ConfigError(
                f"'{section}' in {path.name} must be a table: [{section}].",
                context={"config_file": str(path)},
            )
        for name, value in content.items():
            dotted = f"{section}.{name}"
            if dotted == TOKEN_KEY:
                raise ConfigError(
                    f"The access token must not be in {path.name}.",
                    details=(
                        "Configuration files are read, copied and shared; the token is "
                        "kept in the credentials file that only your account can read."
                    ),
                    remedies=(
                        Remedy(
                            "Remove it from the file and store it in the credentials file "
                            "(the input stays hidden):",
                            "hhe config set homeassistant.token",
                        ),
                    ),
                    context={"config_file": str(path)},
                )
            if dotted not in BY_PATH:
                raise _unknown(path, text, dotted, section, name)
            values[dotted] = value
    return values


def file_entries(path: Path, environ: Mapping[str, str]) -> dict[str, Entry]:
    """Typed entries of the configuration document at *path*."""
    text = read_text(path)
    return document_entries(text, path, environ)


def document_entries(text: str, path: Path, environ: Mapping[str, str]) -> dict[str, Entry]:
    values = document_values(parse_toml(text, path), path, text)
    ctx = ParseContext(base_dir=path.parent, environ=environ)
    entries = {}
    for dotted, value in values.items():
        key = BY_PATH[dotted]
        line = _locate(text, key.section, key.name)
        origin = Origin("file", f"{path}" if line is None else f"{path}:{line}")
        try:
            entries[dotted] = Entry(key.setting.kind.from_toml(value, ctx), origin)
        except InvalidValue as error:
            where = path.name if line is None else f"{path.name}, line {line}"
            raise invalid(key, where, error, value) from None
    return entries


def env_entries(environ: Mapping[str, str], cwd: Path) -> dict[str, Entry]:
    ctx = ParseContext(base_dir=cwd, environ=environ)
    entries = {}
    for name, key in BY_ENV.items():
        if name not in environ:
            continue
        raw = environ[name]
        try:
            entries[key.path] = Entry(key.setting.kind.from_text(raw, ctx), Origin("env", name))
        except InvalidValue as error:
            raise invalid(key, name, error, raw) from None
    return entries


def cli_entries(
    options: Mapping[str, str], environ: Mapping[str, str], cwd: Path
) -> dict[str, Entry]:
    """Entries of command-line options, keyed by key path, with their raw text."""
    ctx = ParseContext(base_dir=cwd, environ=environ)
    entries = {}
    for dotted, raw in options.items():
        key = BY_PATH[dotted]
        option = key.setting.cli or dotted
        try:
            entries[dotted] = Entry(key.setting.kind.from_text(raw, ctx), Origin("cli", option))
        except InvalidValue as error:
            raise invalid(key, option, error, raw) from None
    return entries
