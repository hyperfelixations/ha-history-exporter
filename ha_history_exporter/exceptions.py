"""Custom exceptions for the HA history exporter."""


class AuthError(Exception):
    """Raised on HTTP 401/403 — stop immediately, do not retry."""


class HAConnectionError(Exception):
    """Raised when the HA instance cannot be reached after retries."""


class HAAPIError(Exception):
    """Raised on unexpected HTTP errors or malformed API responses."""


class ConfigError(Exception):
    """Raised when the YAML configuration is invalid or incomplete."""
