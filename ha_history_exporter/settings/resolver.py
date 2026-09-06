"""Merge every configuration source into one runtime configuration.

Precedence, highest first:

    1. command-line options
    2. environment variables
    3. the file named by --config      (suppresses 4 and 5)
    4. a configuration file in the working directory
    5. the user configuration file
    6. built-in defaults

Credentials follow their own, deliberately narrower rule: the environment
variable named by ``home_assistant.url_env`` / ``token_env`` wins over any
file, the token is only ever read from the credentials file, and neither value
is ever written to a log, a manifest, or an error message.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..errors import ConfigError, CredentialsError, Remedy
from . import paths, schema, secrets, sources
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
from .sources import Entry


@dataclass(frozen=True)
class ResolvedSettings:
    """The runtime configuration plus where each value came from."""

    config: AppConfig
    origins: Mapping[str, str] = field(default_factory=dict)
    values: Mapping[str, Any] = field(default_factory=dict)
    config_files: tuple[Path, ...] = ()
    credentials_file: Path | None = None

    def origin(self, key_path: str) -> str:
        return self.origins.get(key_path, "default")


def discover_config_files(
    explicit: Path | None = None, cwd: Path | None = None
) -> list[Path]:
    """Configuration files to read, lowest precedence first.

    An explicit path replaces discovery entirely: asking for a specific file
    must never silently mix in another one.
    """
    if explicit is not None:
        return [Path(explicit)]

    found: list[Path] = []
    user_file = paths.user_config_file()
    if user_file.is_file():
        found.append(user_file)
    for candidate in paths.project_config_candidates(cwd):
        if candidate.is_file():
            found.append(candidate)
            break
    return found


def resolve(
    *,
    explicit_config: Path | str | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
    require_credentials: bool = True,
) -> ResolvedSettings:
    """Build the runtime configuration from every source."""
    environ = os.environ if environ is None else environ

    explicit = Path(explicit_config) if explicit_config is not None else None
    if explicit is not None and not explicit.is_file():
        raise ConfigError(
            f"Config file not found: {explicit}",
            details=(
                "HHE was asked to read this configuration file, but no file "
                "exists at that location."
            ),
            remedies=(
                Remedy(
                    "Create the user configuration with the guided setup:",
                    "ha-history-exporter init",
                ),
                Remedy(
                    "Or point at a configuration file that exists:",
                    "ha-history-exporter --config <path> --date yesterday",
                ),
            ),
            context={"config_file": str(explicit)},
        )

    config_files = discover_config_files(explicit, cwd)

    entries: dict[str, Entry] = sources.default_entries()
    for path in config_files:
        entries.update(sources.file_entries(path))
    entries.update(sources.env_entries(environ))
    entries.update(sources.cli_entries(cli_overrides))

    values: dict[str, Any] = {}
    origins: dict[str, str] = {}
    for key in schema.KEYS:
        entry = entries[key.path]
        values[key.path] = key.coerce(entry.value)
        origins[key.path] = entry.origin

    cfg = _build_config(values)
    _resolve_credentials(
        cfg, values, origins, environ, require_credentials=require_credentials
    )
    validate_config(cfg)

    return ResolvedSettings(
        config=cfg,
        origins=origins,
        values=values,
        config_files=tuple(config_files),
        credentials_file=secrets.credentials_path(),
    )


def _build_config(values: Mapping[str, Any]) -> AppConfig:
    cfg = AppConfig()
    cfg.home_assistant = HAConfig(
        url_env=values["home_assistant.url_env"],
        token_env=values["home_assistant.token_env"],
        timezone=values["home_assistant.timezone"],
    )
    cfg.export = ExportConfig(
        output_dir=values["export.output_dir"],
        mode=values["export.mode"],
        include_current_day=values["export.include_current_day"],
        resume=values["export.resume"],
        force=values["export.force"],
    )
    cfg.requests = RequestsConfig(
        batch_size_entities=values["requests.batch_size_entities"],
        sleep_between_requests_seconds=values[
            "requests.sleep_between_requests_seconds"
        ],
        sleep_between_days_seconds=values["requests.sleep_between_days_seconds"],
        request_timeout_seconds=values["requests.request_timeout_seconds"],
        max_retries=values["requests.max_retries"],
        backoff_seconds=values["requests.backoff_seconds"],
    )
    cfg.formats = FormatsConfig(
        jsonl=values["formats.jsonl"],
        csv=values["formats.csv"],
        parquet=values["formats.parquet"],
    )
    cfg.storage = StorageConfig(
        use_temp_dir=values["storage.use_temp_dir"],
        temp_dir=values["storage.temp_dir"],
        cloud_storage_retry_count=values["storage.cloud_storage_retry_count"],
        cloud_storage_retry_sleep_seconds=values["storage.cloud_storage_retry_sleep_seconds"],
    )
    cfg.history_request = HistoryRequestConfig(
        minimal_response=values["history_request.minimal_response"],
        no_attributes=values["history_request.no_attributes"],
        significant_changes_only=values["history_request.significant_changes_only"],
    )
    cfg.recorder = RecorderConfig(
        export_long_term_statistics=values["recorder.export_long_term_statistics"],
        expected_purge_keep_days=values["recorder.expected_purge_keep_days"],
    )
    cfg.entity_selection = EntitySelectionConfig(
        source=values["entity_selection.source"],
        include_unknown=values["entity_selection.include_unknown"],
        include_unavailable=values["entity_selection.include_unavailable"],
        include_deleted_from_previous_runs=values[
            "entity_selection.include_deleted_from_previous_runs"
        ],
        optional_exclude_patterns=values[
            "entity_selection.optional_exclude_patterns"
        ],
        optional_exclude_domains=values["entity_selection.optional_exclude_domains"],
    )
    return cfg


def _resolve_credentials(
    cfg: AppConfig,
    values: dict[str, Any],
    origins: dict[str, str],
    environ: Mapping[str, str],
    *,
    require_credentials: bool,
) -> None:
    url_env = cfg.home_assistant.url_env
    token_env = cfg.home_assistant.token_env

    url = environ.get(url_env, "").strip()
    if url:
        origins["homeassistant.url"] = f"env:{url_env}"
    else:
        url = str(values.get("homeassistant.url") or "").strip()

    token = environ.get(token_env, "").strip()
    if token:
        origins["homeassistant.token"] = f"env:{token_env}"
    else:
        stored = secrets.read_token()
        if stored:
            token = stored
            origins["homeassistant.token"] = f"file:{secrets.credentials_path()}"

    cfg.ha_url = url.rstrip("/")
    cfg.ha_token = token
    values["homeassistant.url"] = cfg.ha_url
    values["homeassistant.token"] = "<set>" if token else "<not set>"

    if not require_credentials:
        return
    if not cfg.ha_url:
        raise _missing_url(url_env)
    if not cfg.ha_token:
        raise _missing_token(token_env)


def _missing_url(url_env: str) -> CredentialsError:
    return CredentialsError(
        "No Home Assistant URL configured.",
        details=(
            "HHE needs the base URL of your Home Assistant instance. Any "
            "address this machine can reach works: a local hostname, an IP "
            "address, a Tailscale address, or an external HTTPS endpoint."
        ),
        remedies=(
            Remedy("Run the guided setup:", "ha-history-exporter init"),
            Remedy(
                "Or store the URL directly:",
                "ha-history-exporter config set homeassistant.url "
                "http://homeassistant.local:8123",
            ),
            Remedy(
                "Or set it for the current shell session only:",
                f'$env:{url_env} = "http://homeassistant.local:8123"',
            ),
        ),
        context={"config_file": str(paths.user_config_file())},
    )


def _missing_token(token_env: str) -> CredentialsError:
    return CredentialsError(
        "No Home Assistant access token configured.",
        details=(
            "HHE authenticates with a long-lived access token. Create one in "
            "Home Assistant under Profile, Security, Long-Lived Access "
            "Tokens. The token is stored in a separate credentials file that "
            "only your account can read, and is never written to a log, a "
            "manifest, or an export."
        ),
        remedies=(
            Remedy("Run the guided setup:", "ha-history-exporter init"),
            Remedy(
                "Or store the token now (the input stays hidden):",
                "ha-history-exporter config set homeassistant.token",
            ),
            Remedy(
                "Or set it for the current shell session only:",
                f'$env:{token_env} = "<your-token>"',
            ),
        ),
        context={"credentials_file": str(paths.user_credentials_file())},
    )
