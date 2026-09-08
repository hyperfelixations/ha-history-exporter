"""Configuration layer: paths, schema, sources, resolution, secrets."""

from __future__ import annotations

from .model import (
    FORMAT_ORDER,
    Config,
    EntitySettings,
    ExportSettings,
    Format,
    HistoryRequestSettings,
    HomeAssistantSettings,
    RecorderSettings,
    RequestSettings,
    StorageSettings,
    format_list,
)
from .resolver import ResolvedSettings, discover_config_file, resolve

__all__ = [
    "FORMAT_ORDER",
    "Config",
    "EntitySettings",
    "ExportSettings",
    "Format",
    "HistoryRequestSettings",
    "HomeAssistantSettings",
    "RecorderSettings",
    "RequestSettings",
    "ResolvedSettings",
    "StorageSettings",
    "discover_config_file",
    "format_list",
    "load_settings",
    "resolve",
]


#: Resolve every configuration source; see :func:`resolver.resolve`.
load_settings = resolve
