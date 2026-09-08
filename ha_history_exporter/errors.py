"""Error taxonomy for everything HHE reports to the user.

Every error carries a one-line summary, optional details, zero or more
remedies (a description plus a copy-pasteable command), and a context mapping
restricted to registered keys. The exit code is part of the CLI contract; see
internal dev doc, section "Fehlerbild und Exit-Codes".

Nothing in an error may contain a token, a URL query string, or an unverified
response body.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

#: Context keys the console renderer is allowed to print. Anything else is
#: dropped, so an accidental secret can never reach the terminal through the
#: context mapping.
CONTEXT_LABELS: dict[str, str] = {
    "config_file": "Configuration file",
    "credentials_file": "Credentials file",
    "output_dir": "Output directory",
    "temp_dir": "Temporary directory",
    "log_file": "Log file",
    "url": "Home Assistant URL",
    "endpoint": "Endpoint",
    "status": "HTTP status",
    "day": "Day",
    "documentation": "Documentation",
}


@dataclass(frozen=True)
class Remedy:
    """One concrete way forward, optionally with a command to run."""

    description: str
    command: str | None = None


class HHEError(Exception):
    """Base class for every user-facing HHE error."""

    exit_code: int = 1

    def __init__(
        self,
        summary: str,
        *,
        details: str | None = None,
        remedies: Sequence[Remedy] = (),
        context: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(summary)
        self.summary = summary
        self.details = details
        self.remedies: tuple[Remedy, ...] = tuple(remedies)
        self.context: dict[str, str] = dict(context or {})


class UsageError(HHEError):
    """Invalid, missing, or contradictory command-line arguments."""

    exit_code = 2


class ConfigError(HHEError):
    """Invalid configuration file, schema, type, or value range."""

    exit_code = 2


class CredentialsError(ConfigError):
    """No usable Home Assistant URL or access token is configured."""

    exit_code = 2


class AuthError(HHEError):
    """Home Assistant rejected the token (HTTP 401/403) — never retried."""


class HAConnectionError(HHEError):
    """Home Assistant stayed unreachable after every retry."""


class HAAPIError(HHEError):
    """Unexpected response structure or undecodable payload."""


class ValidationError(HHEError):
    """An output file failed validation before finalization."""


class ExportError(HHEError):
    """One or more days could not be exported."""
