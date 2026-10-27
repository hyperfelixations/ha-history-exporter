"""Error taxonomy for everything HHE reports to the user.

Every error carries a one-line summary, optional details, zero or more
remedies (a description plus a copy-pasteable command), and a context mapping
restricted to registered keys. Exit codes and persisted error codes are part
of the contract; see internal dev doc § "Errors and exit codes".

Nothing in an error may contain a token, a URL query string, or an unverified
response body.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum

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


def env_assignment(name: str, value: str) -> str:
    """The shell command that sets *name* for the current session on this platform."""
    if os.name == "nt":
        return f'$env:{name} = "{value}"'
    return f'export {name}="{value}"'


def stdin_from_file(placeholder: str, command: str) -> str:
    """*command* reading standard input from the file *placeholder* on this platform."""
    if os.name == "nt":
        return f"Get-Content {placeholder} | {command}"
    return f"{command} < {placeholder}"


class ExitCode(IntEnum):
    """The process exit codes; see internal dev doc § "Errors and exit codes"."""

    OK = 0
    #: A day failed, or the run could not complete for a reason that is not one below.
    FAILED = 1
    USAGE = 2
    AUTHENTICATION = 3
    #: Home Assistant unreachable, unavailable, answering unusably, or TLS failed.
    HOME_ASSISTANT = 4
    INTERNAL = 70
    INTERRUPTED = 130


class ErrorCode(StrEnum):
    """Error codes persisted in day manifests; the values are a contract."""

    USAGE = "usage_error"
    CONFIGURATION = "configuration_error"
    CREDENTIALS = "credentials_error"
    AUTHENTICATION = "authentication_error"
    CONNECTION = "home_assistant_connection_error"
    API = "home_assistant_api_error"
    TLS = "tls_error"
    VALIDATION = "validation_error"
    EXPORT = "export_error"
    UNEXPECTED = "unexpected_error"
    INTERRUPTED = "interrupted"


class HHEError(Exception):
    """Base class for every user-facing HHE error."""

    exit_code: ExitCode = ExitCode.INTERNAL
    code: ErrorCode = ErrorCode.UNEXPECTED

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

    exit_code = ExitCode.USAGE
    code = ErrorCode.USAGE


class ConfigError(HHEError):
    """An invalid configuration file, value or combination of values."""

    exit_code = ExitCode.USAGE
    code = ErrorCode.CONFIGURATION


class CredentialsError(ConfigError):
    """No usable Home Assistant URL or access token is configured."""

    code = ErrorCode.CREDENTIALS


class AuthError(HHEError):
    """Home Assistant rejected the token (HTTP 401/403); never retried."""

    exit_code = ExitCode.AUTHENTICATION
    code = ErrorCode.AUTHENTICATION


class HAConnectionError(HHEError):
    """Home Assistant stayed unreachable or unavailable after every retry."""

    exit_code = ExitCode.HOME_ASSISTANT
    code = ErrorCode.CONNECTION


class HAAPIError(HHEError):
    """Home Assistant answered, but not with what the request needs."""

    exit_code = ExitCode.HOME_ASSISTANT
    code = ErrorCode.API


class TLSError(HHEError):
    """The HTTPS connection to Home Assistant could not be verified; never retried."""

    exit_code = ExitCode.HOME_ASSISTANT
    code = ErrorCode.TLS


class ValidationError(HHEError):
    """Data that breaks the record contract: from Home Assistant or a stored file."""

    exit_code = ExitCode.FAILED
    code = ErrorCode.VALIDATION


class ExportError(HHEError):
    """An export step that cannot proceed."""

    exit_code = ExitCode.FAILED
    code = ErrorCode.EXPORT


@dataclass(frozen=True)
class ErrorRecord:
    """The deliberately small error representation safe to persist."""

    code: ErrorCode
    summary: str


def safe_error_record(
    exc: BaseException,
    *,
    unexpected_summary: str = "Unexpected internal error.",
) -> ErrorRecord:
    """Return stable metadata without persisting arbitrary exception text."""
    if isinstance(exc, HHEError):
        return ErrorRecord(code=exc.code, summary=exc.summary)
    if isinstance(exc, KeyboardInterrupt):
        return ErrorRecord(code=ErrorCode.INTERRUPTED, summary="The run was interrupted.")
    return ErrorRecord(code=ErrorCode.UNEXPECTED, summary=unexpected_summary)
