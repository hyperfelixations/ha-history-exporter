"""Reading and writing the user configuration document.

``config set`` must never bake a value that came from the environment or from
a project file into the user file. Everything here therefore works on the user
file's own keys only, and the document is regenerated from the key registry so
its comments and ordering stay in sync with the supported options.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

from ..errors import ConfigError, Remedy
from . import paths, schema, sources
from .schema import KeyStatus

HEADER = """\
# Home Assistant History Exporter configuration.
#
# Written by `hhe config set` and `hhe init`; safe to edit by hand.
# The access token is NOT stored here - it lives in credentials.yaml.
# Every key can also be set through an environment variable, for example
# HHE_EXPORT_OUTPUT_DIR, which takes precedence over this file.
"""


def user_config_path(config_dir: Path | None = None) -> Path:
    if config_dir is None:
        return paths.user_config_file()
    return Path(config_dir) / paths.CONFIG_FILENAME


def read_user_values(config_dir: Path | None = None) -> dict[str, Any]:
    """Return the keys this user file sets, by dotted path."""
    path = user_config_path(config_dir)
    if not path.is_file():
        return {}
    return {
        key_path: entry.value
        for key_path, entry in sources.file_entries(path).items()
    }


def write_user_values(
    values: Mapping[str, Any], config_dir: Path | None = None
) -> Path:
    """Regenerate the user configuration file from *values*."""
    path = user_config_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(render(values), encoding="utf-8")
    tmp.replace(path)
    return path


def render(values: Mapping[str, Any]) -> str:
    """Render a configuration document for the given dotted-path values."""
    lines = [HEADER]
    for section in schema.SECTIONS:
        keys = [
            key
            for key in schema.KEYS
            if key.section == section
            and key.status is not KeyStatus.SECRET
            and key.path in values
        ]
        if not keys:
            continue
        lines.append(f"{section}:")
        for key in keys:
            lines.append(f"  # {key.doc}")
            lines.append(f"  {key.name}: {_scalar(values[key.path])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _scalar(value: Any) -> str:
    """Render one value as inline YAML.

    ``safe_dump`` of a bare scalar appends an explicit document-end marker,
    which would terminate the surrounding document; drop the markers and keep
    the value itself.
    """
    dumped = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True)
    lines = [
        line for line in dumped.splitlines() if line.strip() not in ("...", "---")
    ]
    return " ".join(line.strip() for line in lines).strip()


def validate_document(path: Path) -> None:
    """Parse and validate one configuration file, raising ConfigError."""
    entries = sources.file_entries(path)
    for key_path, entry in entries.items():
        schema.BY_PATH[key_path].coerce(entry.value)


def parse_value(key_path: str, raw: str) -> Any:
    """Interpret a command-line value for *key_path* and validate it."""
    key = schema.BY_PATH.get(key_path)
    if key is None:
        suggestion = schema.suggest(key_path)
        details = f"'{key_path}' is not a configuration key HHE knows."
        if suggestion:
            details += f" The closest known key is '{suggestion}'."
        raise ConfigError(
            f"Unknown configuration key '{key_path}'.",
            details=details,
            remedies=(
                Remedy("List every supported key:", "hhe config list"),
            ),
        )
    return key.coerce(sources.parse_scalar(key, raw, "the given value"))
