"""Platform-specific locations for configuration, credentials, and output.

Windows uses the roaming application-data directory for configuration so a
roaming profile carries it along; large export archives deliberately do not
live there but in a plainly visible directory below the user's home.

Setting HHE_CONFIG_DIR overrides the configuration directory. The test suite
relies on it so no test ever touches a real user profile.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import platformdirs

APP_NAME = "ha-history-exporter"
ENV_CONFIG_DIR = "HHE_CONFIG_DIR"

CONFIG_FILENAME = "config.yaml"
CREDENTIALS_FILENAME = "credentials.yaml"
PROJECT_CONFIG_FILENAME = "ha-history-exporter.yaml"
LEGACY_PROJECT_CONFIG_FILENAME = "export_config.yaml"


def user_config_dir() -> Path:
    """Directory holding the user-level configuration and credentials."""
    override = os.environ.get(ENV_CONFIG_DIR, "").strip()
    if override:
        return Path(override)
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=True))


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


def project_config_candidates(cwd: Path | None = None) -> list[Path]:
    """Configuration files discovered in the working directory, most specific first.

    The legacy name keeps a cloned checkout working without any change.
    """
    base = Path.cwd() if cwd is None else Path(cwd)
    return [
        base / PROJECT_CONFIG_FILENAME,
        base / LEGACY_PROJECT_CONFIG_FILENAME,
    ]
