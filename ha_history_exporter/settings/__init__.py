"""Configuration layer: paths, schema, sources, resolution, secrets."""

from __future__ import annotations

from pathlib import Path

from .model import (
    AppConfig,
    EntitySelectionConfig,
    ExportConfig,
    FormatsConfig,
    HAConfig,
    HistoryRequestConfig,
    RecorderConfig,
    RequestsConfig,
    StorageConfig,
    validate_config,
)
from .resolver import ResolvedSettings, discover_config_files, resolve

__all__ = [
    "AppConfig",
    "EntitySelectionConfig",
    "ExportConfig",
    "FormatsConfig",
    "HAConfig",
    "HistoryRequestConfig",
    "RecorderConfig",
    "RequestsConfig",
    "ResolvedSettings",
    "StorageConfig",
    "discover_config_files",
    "load_config",
    "load_settings",
    "resolve",
    "validate_config",
]


def load_settings(**kwargs) -> ResolvedSettings:
    """Resolve every configuration source; see :func:`resolver.resolve`."""
    return resolve(**kwargs)


def load_config(path: str | Path) -> AppConfig:
    """Load one explicit configuration file.

    Backwards-compatible entry point: the file must exist and credentials must
    be available, exactly as before the layered resolver was introduced.
    """
    return resolve(explicit_config=Path(path)).config
