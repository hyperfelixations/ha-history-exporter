"""Merging every configuration source into one immutable configuration.

Precedence, highest first: command-line options, HHE_* environment
variables, the configuration file, built-in defaults. The configuration file
is the one named by ``--config`` or else the user file; nothing is read from
the working directory. The token comes from ``HHE_TOKEN`` or from the
credentials file and stays outside ``Config``. Everything the resolution
depends on - the environment, the working directory - is passed in. See
internal dev doc § "Configuration registry".
"""

from __future__ import annotations

import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ConfigError, CredentialsError, Remedy, env_assignment
from . import paths, secrets, sources
from .model import Config
from .registry import KEYS, TOKEN_ENV, TOKEN_KEY, build_config, unknown_environment
from .sources import DEFAULT, Entry, Origin


@dataclass(frozen=True)
class Connection:
    """How to reach Home Assistant."""

    url: str
    token: str = field(repr=False)
    ca_bundle: Path | None = None


@dataclass(frozen=True)
class ResolvedSettings:
    config: Config
    origins: Mapping[str, Origin]
    config_file: Path | None
    credentials_file: Path
    #: HHE_* variables that set nothing, typically misspelt key names.
    unknown_environment: tuple[str, ...] = ()
    token: str | None = field(default=None, repr=False)
    token_origin: Origin | None = None

    def origin(self, key_path: str) -> Origin:
        if key_path == TOKEN_KEY:
            return self.token_origin or DEFAULT
        return self.origins.get(key_path, DEFAULT)

    def connection(self) -> Connection:
        """The URL and the token; an error that says how to set what is missing."""
        if not self.config.homeassistant.url:
            raise missing_url(self.config_file or paths.CONFIG_FILENAME)
        if not self.token:
            raise missing_token(self.credentials_file)
        return Connection(
            self.config.homeassistant.url, self.token, self.config.homeassistant.ca_bundle
        )


def resolve(
    *,
    environ: Mapping[str, str],
    cwd: Path,
    explicit_config: Path | None = None,
    cli: Mapping[str, str] | None = None,
) -> ResolvedSettings:
    """The configuration of one invocation.

    *cli* maps key paths to the raw text of command-line options.
    """
    config_file = configuration_file(environ, cwd, explicit_config)
    entries: dict[str, Entry] = {key.path: Entry(key.default(), DEFAULT) for key in KEYS}
    if config_file is not None:
        entries.update(sources.file_entries(config_file, environ))
    entries.update(sources.env_entries(environ, cwd))
    entries.update(sources.cli_entries(cli or {}, environ, cwd))

    config = build_config({path: entry.value for path, entry in entries.items()})
    check_config(config)

    credentials_file = paths.user_credentials_file(environ)
    token, token_origin = _token(environ, credentials_file)
    return ResolvedSettings(
        config=config,
        origins={path: entry.origin for path, entry in entries.items()},
        config_file=config_file,
        credentials_file=credentials_file,
        unknown_environment=unknown_environment(environ),
        token=token,
        token_origin=token_origin,
    )


def configuration_from(values: Mapping[str, Any]) -> Config:
    """``Config`` from defaults overridden by *values*, checked across keys."""
    merged = {key.path: key.default() for key in KEYS} | dict(values)
    config = build_config(merged)
    check_config(config)
    return config


def configuration_file(environ: Mapping[str, str], cwd: Path, explicit: Path | None) -> Path | None:
    """The configuration file to read, or None when the user file does not exist.

    A named file must exist. A user file that cannot be examined is an error,
    not an absent file: falling back to defaults would export elsewhere.
    """
    if explicit is not None:
        path = explicit if explicit.is_absolute() else cwd / explicit
        if not path.is_file():
            raise ConfigError(
                f"Configuration file not found: {path}",
                remedies=(
                    Remedy("Create the user configuration with the guided setup:", "hhe init"),
                ),
                context={"config_file": str(path)},
            )
        return path
    path = paths.user_config_file(environ)
    try:
        mode = path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise ConfigError(
            f"Cannot examine the configuration file ({exc.strerror or type(exc).__name__}).",
            details=(
                "HHE cannot tell whether the file holds settings; continuing with "
                "defaults could export to another directory."
            ),
            remedies=(
                Remedy("Check the permissions of the configuration directory:", "hhe config path"),
            ),
            context={"config_file": str(path)},
        ) from None
    if not stat.S_ISREG(mode):
        raise ConfigError(
            "The configuration file is not a regular file.",
            context={"config_file": str(path)},
        )
    return path


def check_config(config: Config) -> None:
    """Rules that span more than one key."""
    if config.requests.max_retries > 0 and not config.requests.backoff:
        raise ConfigError(
            "requests.backoff needs at least one wait time while requests.max_retries is above 0.",
            remedies=(
                Remedy("Give the retries a schedule:", "hhe config set requests.backoff 2,5,15"),
                Remedy("Or turn retries off:", "hhe config set requests.max_retries 0"),
            ),
        )


def _token(environ: Mapping[str, str], credentials_file: Path) -> tuple[str | None, Origin | None]:
    if value := environ.get(TOKEN_ENV, "").strip():
        return value, Origin("env", TOKEN_ENV)
    stored = secrets.read_token(credentials_file)
    if stored:
        return stored, Origin("file", str(credentials_file))
    return None, None


def missing_url(config_file: Path | str) -> CredentialsError:
    return CredentialsError(
        "No Home Assistant URL is configured.",
        details=(
            "HHE needs the base URL of Home Assistant: a local host name, an IP "
            "address, a Tailscale address or an external HTTPS address."
        ),
        remedies=(
            Remedy("Run the guided setup:", "hhe init"),
            Remedy(
                "Or store the URL:",
                "hhe config set homeassistant.url http://homeassistant.local:8123",
            ),
            Remedy(
                "Or set it for this shell session:",
                env_assignment("HHE_URL", "http://homeassistant.local:8123"),
            ),
        ),
        context={"config_file": str(config_file)},
    )


def missing_token(credentials_file: Path) -> CredentialsError:
    return CredentialsError(
        "No Home Assistant access token is configured.",
        details=(
            "Create a long-lived access token in Home Assistant under Profile, "
            "Security. HHE keeps it in a credentials file that only your account "
            "can read and never writes it to a log, a manifest or an export."
        ),
        remedies=(
            Remedy("Run the guided setup:", "hhe init"),
            Remedy(
                "Or store the token (the input stays hidden):",
                "hhe config set homeassistant.token",
            ),
            Remedy("Or set it for this shell session:", env_assignment(TOKEN_ENV, "<token>")),
        ),
        context={"credentials_file": str(credentials_file)},
    )
