"""Changing the user configuration file: ``config set`` and ``config unset``.

A change renders the complete new document and resolves it - defaults plus
the document, nothing from the environment - before the file is replaced, so
the file never holds a value a run would reject. Values of untouched keys are
kept as written; a value given on the command line is stored the way a run
resolves it, a path therefore as an absolute path. See internal dev doc
§ "Configuration registry".
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .. import fsio
from ..errors import ConfigError, Remedy
from .registry import BY_PATH
from .resolver import configuration_from
from .sources import document_entries, document_values, invalid, parse_toml, read_text
from .toml_writer import render_config
from .validators import InvalidValue, ParseContext, TomlValue


def stored_values(path: Path) -> dict[str, TomlValue]:
    """The key values the document at *path* sets, as written; empty without a file."""
    if not path.exists():
        return {}
    text = read_text(path)
    return document_values(parse_toml(text, path), path, text)


def set_value(
    path: Path, key_path: str, raw: str, *, environ: Mapping[str, str], cwd: Path
) -> TomlValue:
    """Store *raw* for *key_path* and return the stored TOML value."""
    key = BY_PATH[key_path]
    try:
        value = key.setting.kind.from_text(raw, ParseContext(base_dir=cwd, environ=environ))
    except InvalidValue as error:
        raise invalid(key, "the command line", error, raw) from None
    if value is None:
        raise ConfigError(
            f"An empty value does not set {key_path}.",
            remedies=(Remedy("Remove the key instead:", f"hhe config unset {key_path}"),),
        )
    values = stored_values(path)
    stored = values[key_path] = key.setting.kind.to_toml(value)
    write(path, values, environ=environ)
    return stored


def unset_value(path: Path, key_path: str, *, environ: Mapping[str, str]) -> bool:
    """Remove *key_path* from the document; False when it was not set."""
    values = stored_values(path)
    if key_path not in values:
        return False
    del values[key_path]
    write(path, values, environ=environ)
    return True


def write(path: Path, values: Mapping[str, TomlValue], *, environ: Mapping[str, str]) -> None:
    """Replace the document with one setting *values*, after resolving it."""
    text = render_config(values)
    entries = document_entries(text, path, environ)
    configuration_from({dotted: entry.value for dotted, entry in entries.items()})
    fsio.atomic_write(path, text.encode("utf-8"))
