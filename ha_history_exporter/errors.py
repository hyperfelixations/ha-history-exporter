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
    code: str = "internal_error"

    def __init__(
        self,
        summary: str,
        *,
        details: str | None = None,
        remedies: Sequence[Remedy] = (),
        context: Mapping[str, str] | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(summary)
        self.summary = summary
        self.details = details
        self.remedies: tuple[Remedy, ...] = tuple(remedies)
        self.context: dict[str, str] = dict(context or {})
        self.code = code or type(self).code


class UsageError(HHEError):
    """Invalid, missing, or contradictory command-line arguments."""

    exit_code = 2
    code = "usage_error"


class ConfigError(HHEError):
    """Invalid configuration file, schema, type, or value range."""

    exit_code = 2
    code = "configuration_error"


class CredentialsError(ConfigError):
    """No usable Home Assistant URL or access token is configured."""

    exit_code = 2
    code = "credentials_error"


class AuthError(HHEError):
    """Home Assistant rejected the token (HTTP 401/403) — never retried."""

    code = "authentication_error"


class HAConnectionError(HHEError):
    """Home Assistant stayed unreachable after every retry."""

    code = "home_assistant_connection_error"


class HAAPIError(HHEError):
    """Unexpected response structure or undecodable payload."""

    code = "home_assistant_api_error"


class ValidationError(HHEError):
    """An output file failed validation before finalization."""

    code = "validation_error"


class ExportError(HHEError):
    """One or more days could not be exported."""

    code = "export_error"


@dataclass(frozen=True)
class ErrorRecord:
    """The deliberately small error representation safe to persist."""

    code: str
    summary: str


def safe_error_record(
    exc: BaseException,
    *,
    unexpected_summary: str = "Unexpected internal error.",
) -> ErrorRecord:
    """Return stable metadata without persisting arbitrary exception text."""
    if isinstance(exc, HHEError):
        return ErrorRecord(code=exc.code, summary=exc.summary)
    return ErrorRecord(code="unexpected_error", summary=unexpected_summary)
