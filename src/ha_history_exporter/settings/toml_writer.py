"""Rendering the configuration and credentials documents as TOML.

The configuration document lists every key of the registry: a key the user
set is a real entry, every other one a comment with its default or an
example. The file therefore documents itself. Values are TOML strings,
integers, floats, booleans or arrays of those. See internal dev doc
§ "Configuration registry".
"""

from __future__ import annotations

import math
import textwrap
from collections.abc import Mapping

from .registry import KEYS, SECTIONS, TOKEN_KEY, Key
from .validators import TomlValue

CONFIG_HEADER = """\
# Home Assistant History Exporter configuration.
#
# Written by `hhe init` and `hhe config set`, which rewrite the whole file;
# editing it by hand is fine. A relative path is relative to this file.
# Commented-out lines show the default or an example. The access token is
# kept in credentials.toml. Environment variables and command-line options
# override this file; `hhe config list --origin` shows what is in effect.
"""

CREDENTIALS_HEADER = """\
# Home Assistant History Exporter credentials. Keep this file private.
"""

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def encode_string(value: str) -> str:
    """A TOML string; a literal string where that avoids escaped backslashes."""
    if "\\" in value and "'" not in value and not any(_is_control(char) for char in value):
        return f"'{value}'"
    escaped = []
    for char in value:
        if char in _ESCAPES:
            escaped.append(_ESCAPES[char])
        elif _is_control(char):
            escaped.append(f"\\u{ord(char):04X}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def _is_control(char: str) -> bool:
    return ord(char) < 0x20 or ord(char) == 0x7F


def encode_value(value: TomlValue) -> str:
    match value:
        case bool():
            return "true" if value else "false"
        case int():
            return str(value)
        case float():
            if not math.isfinite(value):
                raise ValueError("TOML configuration values are finite")
            return repr(value)
        case str():
            return encode_string(value)
        case list():
            return "[" + ", ".join(encode_value(item) for item in value) + "]"


def render_config(values: Mapping[str, TomlValue]) -> str:
    """The configuration document with *values*, keyed by key path, set."""
    unknown = set(values) - {key.path for key in KEYS}
    if unknown:
        raise ValueError(f"not configuration keys: {sorted(unknown)}")
    blocks = [CONFIG_HEADER.rstrip()]
    for section in SECTIONS:
        lines = [f"[{section}]"]
        for key in (key for key in KEYS if key.section == section):
            lines.extend(f"# {line}" for line in textwrap.wrap(key.doc, width=76))
            lines.append(_entry(key, values))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _entry(key: Key, values: Mapping[str, TomlValue]) -> str:
    if key.path in values:
        return f"{key.name} = {encode_value(values[key.path])}"
    default = key.default()
    if default is None:
        shown = key.setting.example
        if shown is None:
            raise ValueError(f"{key.path} needs an example")
        return f"# {key.name} = {encode_value(shown)}"
    return f"# {key.name} = {encode_value(key.setting.kind.to_toml(default))}"


def render_credentials(token: str) -> str:
    section, name = TOKEN_KEY.split(".")
    return f"{CREDENTIALS_HEADER}\n[{section}]\n{name} = {encode_string(token)}\n"
