"""The credentials file that holds the Home Assistant access token.

``credentials.toml`` lives beside the configuration file and holds only
``[homeassistant] token = "..."``. It is written atomically with owner-only
permissions (0600) on POSIX; on Windows it inherits the ACL of the user
profile, which is restricted to the account. No message ever quotes the
file's content. See internal dev doc § "Credentials".
"""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

from .. import fsio
from ..errors import ConfigError, Remedy
from .registry import TOKEN_KEY
from .sources import read_text, toml_error_position
from .toml_writer import render_credentials

OWNER_ONLY = 0o600

_SECTION, _NAME = TOKEN_KEY.split(".")


def _unusable(path: Path, problem: str) -> ConfigError:
    return ConfigError(
        f"The credentials file {problem}.",
        details="Its content is not shown because it holds the access token.",
        remedies=(
            Remedy(
                "Store the token again to rewrite the file (the input stays hidden):",
                "hhe config set homeassistant.token",
            ),
        ),
        context={"credentials_file": str(path)},
    )


def read_token(path: Path) -> str | None:
    """The stored token, or None when no file or no token is stored."""
    if not path.exists():
        return None
    try:
        text = read_text(path)
    except ConfigError:
        raise _unusable(path, "cannot be read") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        _, position = toml_error_position(exc, text)
        raise _unusable(path, f"is not valid TOML ({position})") from None
    section = data.get(_SECTION)
    if set(data) - {_SECTION} or (
        section is not None and (not isinstance(section, dict) or set(section) - {_NAME})
    ):
        raise _unusable(path, f'must contain only [{_SECTION}] with {_NAME} = "..."')
    if section is None:
        return None
    token = section.get(_NAME)
    if token is None:
        return None
    if not isinstance(token, str):
        raise _unusable(path, f"must store {TOKEN_KEY} as a string")
    return token.strip() or None


def write_token(path: Path, token: str) -> None:
    """Store *token*, readable by the owner only."""
    fsio.atomic_write(path, render_credentials(token).encode("utf-8"), mode=OWNER_ONLY)
    if os.name != "nt":
        path.chmod(OWNER_ONLY)


def clear_token(path: Path) -> bool:
    """Remove the credentials file; True when there was one."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def is_owner_only(path: Path) -> bool | None:
    """Whether only the owner can read *path*; None on Windows, where ACLs apply."""
    if os.name == "nt":
        return None
    return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
