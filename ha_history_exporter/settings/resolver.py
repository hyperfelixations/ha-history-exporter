"""Merge every configuration source into one immutable configuration.

Precedence, highest first:

    1. command-line options
    2. environment variables
    3. the file named by --config, or else the user configuration file
    4. built-in defaults

There is no discovery in the working directory. A tool whose behaviour depends
on the directory it was started from cannot be reasoned about, and the previous
two-file arrangement was indistinguishable to a user.

The access token follows its own, deliberately narrower rule: it comes from the
environment or from the credentials file, never from a configuration file, and
it is returned beside the configuration rather than inside it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..errors import ConfigError, CredentialsError, Remedy
from . import paths, schema, secrets, sources
from .model import (
    Config,
    EntitySettings,
    ExportSettings,
    HistoryRequestSettings,
    HomeAssistantSettings,
    RecorderSettings,
    RequestSettings,
    StorageSettings,
)
from .schema import KeyStatus
from .sources import Entry

TOKEN_KEY = "homeassistant.token"  # noqa: S105 - key path, not a secret
URL_KEY = "homeassistant.url"


@dataclass(frozen=True)
class ResolvedSettings:
    """The runtime configuration, the token, and where each value came from."""

    config: Config
    token: str = ""
    origins: Mapping[str, str] = field(default_factory=dict)
    values: Mapping[str, Any] = field(default_factory=dict)
    config_file: Path | None = None
    credentials_file: Path | None = None

    def origin(self, key_path: str) -> str:
        return self.origins.get(key_path, "default")


def discover_config_file(explicit: Path | None = None) -> Path | None:
    """The configuration file to read, or None when there is none.

    An explicit path replaces the user file entirely; asking for a specific
    file must never silently mix in another one.
    """
    if explicit is not None:
        return Path(explicit)
    user_file = paths.user_config_file()
    return user_file if user_file.is_file() else None


def resolve(
    *,
    explicit_config: Path | str | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    require_credentials: bool = True,
) -> ResolvedSettings:
    """Build the runtime configuration from every source."""
    environ = os.environ if environ is None else environ

    explicit = Path(explicit_config) if explicit_config is not None else None
    if explicit is not None and not explicit.is_file():
        raise _missing_config_file(explicit)

    config_file = discover_config_file(explicit)

    entries: dict[str, Entry] = sources.default_entries()
    if config_file is not None:
        file_values = sources.file_entries(config_file)
        _reject_token_in_file(file_values, config_file)
        entries.update(file_values)
    entries.update(sources.env_entries(environ))
    entries.update(sources.cli_entries(cli_overrides))

    values: dict[str, Any] = {}
    origins: dict[str, str] = {}
    for key in schema.KEYS:
        entry = entries[key.path]
        values[key.path] = key.coerce(entry.value)
        origins[key.path] = entry.origin

    token = _resolve_token(values, origins, environ)
    config = _build_config(values)
    _check_across_keys(config)

    if require_credentials:
        if not config.homeassistant.url:
            raise _missing_url()
        if not token:
            raise _missing_token()

    return ResolvedSettings(
        config=config,
        token=token,
        origins=origins,
        values=values,
        config_file=config_file,
        credentials_file=secrets.credentials_path(),
    )


def _build_config(values: Mapping[str, Any]) -> Config:
    return Config(
        homeassistant=HomeAssistantSettings(
            url=str(values[URL_KEY]).strip().rstrip("/"),
        ),
        export=ExportSettings(
            output_dir=values["export.output_dir"],
            timezone=values["export.timezone"],
            formats=values["export.formats"],
        ),
        requests=RequestSettings(
            batch_size=values["requests.batch_size"],
            sleep_between_requests=values["requests.sleep_between_requests"],
            sleep_between_days=values["requests.sleep_between_days"],
            timeout=values["requests.timeout"],
            max_retries=values["requests.max_retries"],
            backoff=values["requests.backoff"],
        ),
        history_request=HistoryRequestSettings(
            minimal_response=values["history_request.minimal_response"],
            no_attributes=values["history_request.no_attributes"],
            significant_changes_only=values[
                "history_request.significant_changes_only"
            ],
        ),
        entities=EntitySettings(
            include_unknown=values["entities.include_unknown"],
            include_unavailable=values["entities.include_unavailable"],
            exclude_domains=values["entities.exclude_domains"],
            exclude_patterns=values["entities.exclude_patterns"],
        ),
        recorder=RecorderSettings(
            purge_keep_days=values["recorder.purge_keep_days"],
        ),
        storage=StorageSettings(
            temp_dir=values["storage.temp_dir"],
            locked_file_retries=values["storage.locked_file_retries"],
            locked_file_retry_sleep=values["storage.locked_file_retry_sleep"],
        ),
    )


def _check_across_keys(config: Config) -> None:
    """Rules that span more than one key and cannot live in the registry."""
    if config.requests.max_retries > 0 and not config.requests.backoff:
        raise ConfigError(
            "requests.backoff must contain at least one wait time when "
            "requests.max_retries is greater than 0.",
            details=(
                "Retries are enabled but no wait time is configured, so the "
                "client would have no backoff schedule to follow."
            ),
            remedies=(
                Remedy(
                    "Give the retries a schedule:",
                    "hhe config set requests.backoff 2,5,15",
                ),
                Remedy(
                    "Or turn retries off:",
                    "hhe config set requests.max_retries 0",
                ),
            ),
        )


def _resolve_token(
    values: dict[str, Any],
    origins: dict[str, str],
    environ: Mapping[str, str],
) -> str:
    """Take the token from the environment, else from the credentials file."""
    token = str(values.get(TOKEN_KEY) or "").strip()
    if token and origins.get(TOKEN_KEY, "default").startswith("env:"):
        values[TOKEN_KEY] = "<set>"
        return token

    stored = secrets.read_token()
    if stored:
        origins[TOKEN_KEY] = f"file:{secrets.credentials_path()}"
        values[TOKEN_KEY] = "<set>"
        return stored

    origins[TOKEN_KEY] = "default"
    values[TOKEN_KEY] = "<not set>"
    return ""


def _reject_token_in_file(entries: Mapping[str, Entry], path: Path) -> None:
    """A configuration file is world-readable in spirit; a token is not."""
    for key_path, entry in entries.items():
        key = schema.BY_PATH.get(key_path)
        if key is not None and key.status is KeyStatus.SECRET:
            raise ConfigError(
                f"The access token must not be stored in {path.name}.",
                details=(
                    "Configuration files are meant to be readable, copied, and "
                    "shared. HHE keeps the token in a separate credentials file "
                    "that only your account can read."
                ),
                remedies=(
                    Remedy(
                        "Remove the key from the file and store the token "
                        "properly (the input stays hidden):",
                        "hhe config set homeassistant.token",
                    ),
                ),
                context={"config_file": str(entry.location or path)},
            )


def _missing_config_file(path: Path) -> ConfigError:
    return ConfigError(
        f"Config file not found: {path}",
        details=(
            "HHE was asked to read this configuration file, but no file exists "
            "at that location."
        ),
        remedies=(
            Remedy("Create the user configuration with the guided setup:", "hhe init"),
            Remedy(
                "Or point at a configuration file that exists:",
                "hhe export --config <path>",
            ),
        ),
        context={"config_file": str(path)},
    )


def _missing_url() -> CredentialsError:
    return CredentialsError(
        "No Home Assistant URL configured.",
        details=(
            "HHE needs the base URL of your Home Assistant instance. Any "
            "address this machine can reach works: a local hostname, an IP "
            "address, a Tailscale address, or an external HTTPS endpoint."
        ),
        remedies=(
            Remedy("Run the guided setup:", "hhe init"),
            Remedy(
                "Or store the URL directly:",
                "hhe config set homeassistant.url http://homeassistant.local:8123",
            ),
            Remedy(
                "Or set it for the current shell session only:",
                '$env:HHE_URL = "http://homeassistant.local:8123"',
            ),
        ),
        context={"config_file": str(paths.user_config_file())},
    )


def _missing_token() -> CredentialsError:
    return CredentialsError(
        "No Home Assistant access token configured.",
        details=(
            "HHE authenticates with a long-lived access token. Create one in "
            "Home Assistant under Profile, Security, Long-Lived Access Tokens. "
            "The token is stored in a separate credentials file that only your "
            "account can read, and is never written to a log, a manifest, or "
            "an export."
        ),
        remedies=(
            Remedy("Run the guided setup:", "hhe init"),
            Remedy(
                "Or store the token now (the input stays hidden):",
                "hhe config set homeassistant.token",
            ),
            Remedy(
                "Or set it for the current shell session only:",
                '$env:HHE_TOKEN = "<your-token>"',
            ),
        ),
        context={"credentials_file": str(paths.user_credentials_file())},
    )
