"""Storage for the Home Assistant access token.

The token never lives in the configuration file. It is written to a separate
credentials file in the user configuration directory, created with owner-only
permissions and replaced atomically.

On POSIX the file mode is enforced to 0600. On Windows the file inherits the
user profile ACL, which is already restricted to the account itself; HHE does
not manipulate ACLs, it reports the location so ``doctor`` can show it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import yaml

from ..errors import ConfigError, Remedy
from . import paths

_SECTION = "homeassistant"
_FIELD = "token"

OWNER_ONLY = 0o600


def credentials_path(config_dir: Path | None = None) -> Path:
    if config_dir is None:
        return paths.user_credentials_file()
    return Path(config_dir) / paths.CREDENTIALS_FILENAME


def read_token(config_dir: Path | None = None) -> str | None:
    """Return the stored token, or None when no credentials file exists."""
    path = credentials_path(config_dir)
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        # The parser error quotes the offending line, which for this file may
        # be the token itself — report the failure type only.
        raise ConfigError(
            f"Cannot read the credentials file ({type(exc).__name__}).",
            details=(
                "The file exists but could not be read or parsed. Its content "
                "is deliberately not shown because it holds a secret."
            ),
            remedies=(
                Remedy(
                    "Store the token again to rewrite the file:",
                    "ha-history-exporter config set homeassistant.token",
                ),
            ),
            context={"credentials_file": str(path)},
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(
            "The credentials file does not contain a mapping.",
            context={"credentials_file": str(path)},
        )
    token = (data.get(_SECTION) or {}).get(_FIELD)
    if token is None:
        return None
    if not isinstance(token, str):
        raise ConfigError(
            f"{_SECTION}.{_FIELD} in the credentials file must be a string.",
            context={"credentials_file": str(path)},
        )
    token = token.strip()
    return token or None


def write_token(token: str, config_dir: Path | None = None) -> Path:
    """Store *token* with owner-only permissions and return the file path."""
    path = credentials_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)

    document = yaml.safe_dump({_SECTION: {_FIELD: token}}, sort_keys=True)
    descriptor = os.open(
        tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, OWNER_ONLY
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(document)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    os.replace(tmp, path)
    _harden(path)
    return path


def clear_token(config_dir: Path | None = None) -> bool:
    """Remove the stored token. Returns True when a file was removed."""
    path = credentials_path(config_dir)
    if not path.exists():
        return False
    path.unlink()
    return True


def _harden(path: Path) -> None:
    if os.name == "nt":  # ACLs are inherited from the user profile.
        return
    try:
        os.chmod(path, OWNER_ONLY)
    except OSError:  # pragma: no cover - unusual filesystem
        pass


def is_owner_only(path: Path) -> bool | None:
    """True/False on POSIX, None on Windows where the check does not apply."""
    if os.name == "nt":
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    return mode & 0o077 == 0
