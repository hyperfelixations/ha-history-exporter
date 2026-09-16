"""Platform-specific locations for configuration, credentials, and output.

There is exactly one configuration file, in the user configuration directory.
HHE never picks one up from the working directory: a tool whose behaviour
depends on where it happens to be started is a tool nobody can reason about.
To use a different file, name it with ``--config``.

Windows uses the roaming application-data directory for configuration so a
roaming profile carries it along; large export archives deliberately do not
live there but in a plainly visible directory below the user's home.

Setting HHE_CONFIG_DIR overrides the configuration directory. The test suite
relies on it so no test ever touches a real user profile.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import platformdirs

logger = logging.getLogger(__name__)

APP_NAME = "ha-history-exporter"
ENV_CONFIG_DIR = "HHE_CONFIG_DIR"

CONFIG_FILENAME = "config.yaml"
CREDENTIALS_FILENAME = "credentials.yaml"


def user_config_dir() -> Path:
    """Directory holding the user-level configuration and credentials.

    The result is always absolute. platformdirs does not check the return
    value of the Windows known-folder call, so a failed lookup yields an empty
    string and, after normalisation, a path relative to the working directory.
    See internal dev doc, Einrichtung.
    """
    override = os.environ.get(ENV_CONFIG_DIR, "").strip()
    if override:
        return Path(override)

    candidate = Path(platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=True))
    if candidate.is_absolute():
        return candidate

    fallback = _fallback_config_dir()
    logger.warning(
        "The platform configuration directory resolved to %s, which is not "
        "absolute; using %s instead.",
        candidate,
        fallback,
    )
    return fallback


def _fallback_config_dir() -> Path:
    """Where configuration goes when the platform lookup gives no usable answer."""
    for variable in ("APPDATA", "XDG_CONFIG_HOME"):
        raw = os.environ.get(variable, "").strip()
        if raw and Path(raw).is_absolute():
            return Path(raw) / APP_NAME
    return Path.home() / ".config" / APP_NAME


def user_config_file() -> Path:
    return user_config_dir() / CONFIG_FILENAME


def user_credentials_file() -> Path:
    return user_config_dir() / CREDENTIALS_FILENAME


def default_output_dir() -> Path:
    """Where exports go when nothing is configured."""
    return Path.home() / "ha-history-exports"


def default_temp_root() -> Path:
    """Platform-neutral working directory for temporary export files."""
    return Path(tempfile.gettempdir()) / APP_NAME
