"""Platform locations for configuration, credentials, output and working files.

The configuration and the credentials live in the user configuration
directory; HHE never reads a configuration from the working directory. The
roaming application-data directory holds them on Windows, so a roaming profile
carries them along; export archives live in a plainly visible directory below
the home directory. ``HHE_CONFIG_DIR`` names another configuration directory.
See internal dev doc § "Configuration registry".
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Mapping
from pathlib import Path

import platformdirs

logger = logging.getLogger(__name__)

APP_NAME = "ha-history-exporter"
ENV_CONFIG_DIR = "HHE_CONFIG_DIR"

CONFIG_FILENAME = "config.toml"
CREDENTIALS_FILENAME = "credentials.toml"


def user_config_dir(environ: Mapping[str, str]) -> Path:
    """The directory of the configuration and credentials files; always absolute.

    platformdirs does not check the Windows known-folder lookup, so a failed
    lookup yields a relative path; it is replaced by a fallback.
    """
    override = environ.get(ENV_CONFIG_DIR, "").strip()
    if override:
        return Path(override).absolute()

    candidate = Path(platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=True))
    if candidate.is_absolute():
        return candidate
    fallback = _fallback_config_dir(environ)
    logger.warning(
        "The platform configuration directory resolved to %s, which is not absolute; "
        "using %s instead.",
        candidate,
        fallback,
    )
    return fallback


def _fallback_config_dir(environ: Mapping[str, str]) -> Path:
    for variable in ("APPDATA", "XDG_CONFIG_HOME"):
        raw = environ.get(variable, "").strip()
        if raw and Path(raw).is_absolute():
            return Path(raw) / APP_NAME
    return Path.home() / ".config" / APP_NAME


def user_config_file(environ: Mapping[str, str]) -> Path:
    return user_config_dir(environ) / CONFIG_FILENAME


def user_credentials_file(environ: Mapping[str, str]) -> Path:
    return user_config_dir(environ) / CREDENTIALS_FILENAME


def default_output_dir() -> Path:
    return Path.home() / "ha-history-exports"


def default_temp_root() -> Path:
    return Path(tempfile.gettempdir()) / APP_NAME
