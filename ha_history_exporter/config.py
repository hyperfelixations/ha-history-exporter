"""Backwards-compatible import path for the configuration layer.

The implementation lives in :mod:`ha_history_exporter.settings`. This module
keeps ``from ha_history_exporter.config import AppConfig, load_config`` working.
"""

from .errors import ConfigError
from .settings import (
    AppConfig,
    EntitySelectionConfig,
    ExportConfig,
    FormatsConfig,
    HAConfig,
    HistoryRequestConfig,
    RecorderConfig,
    RequestsConfig,
    StorageConfig,
    load_config,
    load_settings,
    validate_config,
)
from .settings.paths import default_output_dir

__all__ = [
    "AppConfig",
    "ConfigError",
    "EntitySelectionConfig",
    "ExportConfig",
    "FormatsConfig",
    "HAConfig",
    "HistoryRequestConfig",
    "RecorderConfig",
    "RequestsConfig",
    "StorageConfig",
    "default_output_dir",
    "load_config",
    "load_settings",
    "validate_config",
]
