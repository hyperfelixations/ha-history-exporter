"""Storing a setup: the configuration document and the token, together or not at all."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .. import fsio
from ..settings import secrets, user_file
from ..settings.validators import TomlValue


def write_setup(
    config_file: Path,
    values: Mapping[str, TomlValue],
    credentials_file: Path,
    token: str,
    *,
    environ: Mapping[str, str],
) -> None:
    """Replace both files; when the second write fails, the first is undone."""
    previous = config_file.read_bytes() if config_file.exists() else None
    user_file.write(config_file, values, environ=environ)
    try:
        secrets.write_token(credentials_file, token)
    except BaseException:
        if previous is None:
            config_file.unlink(missing_ok=True)
        else:
            fsio.atomic_write(config_file, previous)
        raise
