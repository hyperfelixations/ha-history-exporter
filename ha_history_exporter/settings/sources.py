"""Configuration sources: files, environment variables, command-line options.

Each source produces a flat mapping of dotted key paths to :class:`Entry`
objects. The resolver merges them by precedence and keeps the origin so
``config list --origin`` and error messages can name the source.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..errors import ConfigError, Remedy
from . import schema
from .schema import (
    BY_ENV_VAR,
    BY_PATH,
    KEYS,
    KNOWN_SECTIONS,
    LEGACY_ENV_VARS,
    Key,
    KeyType,
)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Entry:
    """One configured value together with where it came from."""

    value: Any
    origin: str
    location: str | None = None


def default_entries() -> dict[str, Entry]:
    return {
        key.path: Entry(schema.default_value(key), "default") for key in KEYS
    }


# ── YAML files ────────────────────────────────────────────────────────────────

def _key_lines(node: yaml.Node, prefix: str, lines: dict[str, int]) -> None:
    if not isinstance(node, yaml.MappingNode):
        return
    for key_node, value_node in node.value:
        if not isinstance(key_node, yaml.ScalarNode):
            continue
        path = f"{prefix}.{key_node.value}" if prefix else str(key_node.value)
        lines[path] = key_node.start_mark.line + 1
        _key_lines(value_node, path, lines)


def _unknown_key(path: str, file: Path, line: int | None) -> ConfigError:
    where = f"{file}" if line is None else f"{file}, line {line}"
    suggestion = schema.suggest(path)
    details = f"'{path}' is not a configuration key HHE knows."
    if suggestion:
        details += f" The closest known key is '{suggestion}'."
    return ConfigError(
        f"Unknown configuration key '{path}' in {where}.",
        details=details,
        remedies=(
            Remedy(
                "List every supported key with its effective value:",
                "hhe config list",
            ),
        ),
        context={"config_file": str(file)},
    )


def _flatten(
    data: Mapping[str, Any],
    file: Path,
    lines: Mapping[str, int],
    prefix: str = "",
) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    for raw_key, value in data.items():
        key = str(raw_key)
        path = f"{prefix}.{key}" if prefix else key
        line = lines.get(path)
        location = f"{file}:{line}" if line else str(file)

        if path in BY_PATH:
            entries[path] = Entry(value, f"file:{file}", location)
            continue
        if not prefix and path in KNOWN_SECTIONS:
            if value is None:
                continue
            if not isinstance(value, dict):
                raise ConfigError(
                    f"Section '{path}' in {file} must be a mapping of settings.",
                    details=f"Found {type(value).__name__} instead of a section.",
                    context={"config_file": str(file)},
                )
            entries.update(_flatten(value, file, lines, path))
            continue
        raise _unknown_key(path, file, line)
    return entries


def file_entries(path: Path) -> dict[str, Entry]:
    """Read one YAML configuration file into entries.

    Raises ConfigError for unreadable files, invalid YAML, unknown keys, and
    malformed sections.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"Cannot read configuration file {path}: {exc}",
            context={"config_file": str(path)},
        ) from exc

    try:
        data = yaml.safe_load(text) or {}
        node = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"Invalid YAML in {path}: {exc}",
            details="The configuration file exists but could not be parsed as YAML.",
            context={"config_file": str(path)},
        ) from exc

    if not isinstance(data, dict):
        raise ConfigError(
            f"Invalid YAML in {path}: the top level must be a mapping of sections.",
            context={"config_file": str(path)},
        )

    lines: dict[str, int] = {}
    if node is not None:
        _key_lines(node, "", lines)
    return _flatten(data, path, lines)


# ── environment ───────────────────────────────────────────────────────────────

def parse_scalar(key: Key, raw: str, source: str) -> Any:
    value = raw.strip()
    try:
        if key.type is KeyType.BOOL:
            lowered = value.lower()
            if lowered in _TRUE:
                return True
            if lowered in _FALSE:
                return False
            raise ValueError("expected true or false")
        if key.type in (KeyType.INT, KeyType.OPTIONAL_INT):
            if key.type is KeyType.OPTIONAL_INT and value.lower() in {"", "null", "none"}:
                return None
            return int(value)
        if key.type is KeyType.FLOAT:
            return float(value)
        if key.type is KeyType.STR_LIST:
            return [item.strip() for item in value.split(",") if item.strip()]
        if key.type is KeyType.NUM_LIST:
            return [float(item) for item in value.split(",") if item.strip()]
        if key.type is KeyType.FORMAT_LIST:
            return schema.format_set(value, key.path)
    except ValueError as exc:
        raise ConfigError(
            f"{source} is not a valid value for {key.path}: {exc}.",
            details=f"{key.doc} Expected type: {key.type.value}.",
        ) from exc
    return value


def env_entries(environ: Mapping[str, str]) -> dict[str, Entry]:
    """Read every configured environment variable.

    The legacy names HA_URL and HA_TOKEN are read after the HHE_ spellings and
    therefore win when both are set: they are the ones already configured on
    existing machines, and silently preferring the newer name would change a
    working setup.
    """
    entries: dict[str, Entry] = {}
    for name, key in BY_ENV_VAR.items():
        if name in environ:
            entries[key.path] = Entry(
                parse_scalar(key, environ[name], name), f"env:{name}", name
            )
    for name, key_path in LEGACY_ENV_VARS.items():
        if name in environ:
            key = BY_PATH[key_path]
            entries[key_path] = Entry(
                parse_scalar(key, environ[name], name), f"env:{name}", name
            )
    return entries


# ── command line ──────────────────────────────────────────────────────────────

def cli_entries(overrides: Mapping[str, Any] | None) -> dict[str, Entry]:
    if not overrides:
        return {}
    unknown = sorted(set(overrides) - set(BY_PATH))
    if unknown:  # pragma: no cover - guards a programming error, not user input
        raise ConfigError(
            f"Unknown configuration key(s) from the command line: {unknown}."
        )
    return {path: Entry(value, "cli") for path, value in overrides.items()}
