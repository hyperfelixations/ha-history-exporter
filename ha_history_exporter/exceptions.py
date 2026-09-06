"""Backwards-compatible import path for the error taxonomy.

The definitions live in :mod:`ha_history_exporter.errors`. This module keeps
``from ha_history_exporter.exceptions import AuthError`` working.
"""

from .errors import (
    AuthError,
    ConfigError,
    CredentialsError,
    ExportError,
    HAAPIError,
    HAConnectionError,
    HHEError,
    Remedy,
    UsageError,
    ValidationError,
)

__all__ = [
    "AuthError",
    "ConfigError",
    "CredentialsError",
    "ExportError",
    "HAAPIError",
    "HAConnectionError",
    "HHEError",
    "Remedy",
    "UsageError",
    "ValidationError",
]
