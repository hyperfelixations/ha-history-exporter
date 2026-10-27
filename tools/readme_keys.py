#!/usr/bin/env python3
"""Render the configuration key table of README.md from the key registry.

``python tools/readme_keys.py`` rewrites the table between the markers;
``--check`` only reports whether it is current. ``tests/repo`` runs the check.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ha_history_exporter.settings.registry import KEYS, TOKEN_ENV, TOKEN_KEY, Key
from ha_history_exporter.settings.toml_writer import encode_value

README = Path(__file__).resolve().parents[1] / "README.md"
START = "<!-- keys:start -->"
END = "<!-- keys:end -->"

#: Defaults that depend on the machine, as the README states them.
MACHINE_DEFAULTS = {
    "export.output_dir": "`ha-history-exports` in your home directory",
    "storage.temp_dir": "`ha-history-exporter` in the system's temporary directory",
}
TOKEN_DOC = "Long-lived access token of Home Assistant; kept in `credentials.toml`."  # noqa: S105


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _default(key: Key) -> str:
    if key.path in MACHINE_DEFAULTS:
        return MACHINE_DEFAULTS[key.path]
    default = key.default()
    if default is None:
        return "not set"
    return f"`{encode_value(key.setting.kind.to_toml(default))}`"


def render() -> str:
    """The table, header included, without the markers."""
    rows = [
        "| Key | Default | Environment variable | `hhe export` option | Meaning |",
        "|---|---|---|---|---|",
    ]
    for key in KEYS:
        option = f"`{key.setting.cli}`" if key.setting.cli else ""
        rows.append(
            f"| `{key.path}` | {_default(key)} | `{key.env_var}` | {option} | {_cell(key.doc)} |"
        )
        if key.path == "homeassistant.url":
            rows.append(f"| `{TOKEN_KEY}` | not set | `{TOKEN_ENV}` |  | {TOKEN_DOC} |")
    return "\n".join(rows)


def updated(text: str) -> str:
    """*text* with the table between the markers replaced by the current one."""
    head, start, rest = text.partition(START)
    _, end, tail = rest.partition(END)
    if not start or not end:
        raise ValueError(f"README.md needs the markers {START} and {END}.")
    return f"{head}{START}\n{render()}\n{END}{tail}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report only; change nothing.")
    args = parser.parse_args()
    text = README.read_text(encoding="utf-8")
    current = updated(text)
    if current == text:
        return 0
    if args.check:
        print("README.md: the key table differs from the registry.", file=sys.stderr)
        return 1
    README.write_bytes(current.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
