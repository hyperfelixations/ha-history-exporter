"""The runtime configuration and the declaration of every configuration key.

Each field of a section carries its key metadata - kind, documentation,
environment variable, command-line option - so this module is the only place
a key is declared; :mod:`.registry` derives everything else from it. ``Config``
is immutable and holds typed, validated values: absolute paths, a ``ZoneInfo``,
a format set.

The access token is deliberately not part of ``Config``; it travels beside it,
so a configuration value can never carry the secret into a log line, a
manifest or a ``repr``. See internal dev doc § "Configuration registry".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..domain.formats import Format
from . import paths
from .validators import (
    Bool,
    Formats,
    HttpUrl,
    Integer,
    Kind,
    Names,
    OptionalValue,
    PathValue,
    Seconds,
    SecondsList,
    TomlValue,
    Zone,
)

#: The field-metadata key that holds a :class:`Setting`.
SETTING = "hhe_setting"


@dataclass(frozen=True)
class Setting:
    """How one configuration key is read, documented and overridden."""

    kind: Kind[Any]
    doc: str
    #: The environment variable, when the generic HHE_<SECTION>_<KEY> is clumsy.
    env: str | None = None
    #: The ``hhe export`` option that overrides the key for one run.
    cli: str | None = None
    #: Shown in the generated document for a key without a default.
    example: TomlValue | None = None


def setting(
    kind: Kind[Any],
    doc: str,
    *,
    default: Any = None,
    default_factory: Any = None,
    env: str | None = None,
    cli: str | None = None,
    example: TomlValue | None = None,
) -> Any:
    metadata = {SETTING: Setting(kind, doc, env, cli, example)}
    if default_factory is not None:
        return field(default_factory=default_factory, metadata=metadata)
    return field(default=default, metadata=metadata)


@dataclass(frozen=True)
class HomeAssistantSettings:
    url: str | None = setting(
        OptionalValue(HttpUrl()),
        "Home Assistant base URL.",
        env="HHE_URL",
        example="http://homeassistant.local:8123",
    )
    ca_bundle: Path | None = setting(
        OptionalValue(PathValue()),
        "PEM file with the certificate authority that signed the HTTPS certificate "
        "of Home Assistant; unset uses the system's trusted authorities.",
        example="home-assistant-ca.pem",
    )


@dataclass(frozen=True)
class ExportSettings:
    output_dir: Path = setting(
        PathValue(),
        "Directory that receives exports, metadata and logs.",
        default_factory=paths.default_output_dir,
        cli="--output-dir",
    )
    timezone: ZoneInfo | None = setting(
        OptionalValue(Zone()),
        "IANA time zone of the calendar days; unset takes the archive's time zone, "
        "and for a new archive the time zone of Home Assistant.",
        cli="--timezone",
        example="Europe/Berlin",
    )
    formats: frozenset[Format] = setting(
        Formats(),
        "Output formats: any of jsonl, csv, parquet; [] captures the entity snapshot only.",
        default=frozenset({Format.JSONL}),
        cli="--format",
    )


@dataclass(frozen=True)
class RequestSettings:
    """How hard HHE asks Home Assistant for history.

    The defaults are proven on an instance with about 1400 entities: 15
    entities per request and 0.7 s between requests keep it responsive. See
    internal dev doc § "Configuration registry".
    """

    batch_size: int = setting(
        Integer(1), "Entities per history request.", default=15, cli="--batch-size"
    )
    sleep_between_requests: float = setting(
        Seconds(),
        "Pause between history requests, in seconds.",
        default=0.7,
        cli="--sleep-between-requests",
    )
    sleep_between_days: float = setting(
        Seconds(),
        "Pause between exported days, in seconds.",
        default=5.0,
        cli="--sleep-between-days",
    )
    timeout: int = setting(
        Integer(1), "HTTP read timeout per request, in seconds.", default=120, cli="--timeout"
    )
    max_retries: int = setting(
        Integer(0),
        "Retries of a request after a timeout, a lost connection, HTTP 429 or 502-504.",
        default=3,
        cli="--max-retries",
    )
    backoff: tuple[float, ...] = setting(
        SecondsList(),
        "Wait times between retries, in seconds; the last one repeats.",
        default=(2.0, 5.0, 15.0),
    )


@dataclass(frozen=True)
class HistoryRequestSettings:
    """Options passed to the Home Assistant history endpoint.

    The first three reduce what Home Assistant returns; they default to off,
    so an export is complete unless a user asks for less.
    """

    minimal_response: bool = setting(
        Bool(), "Ask Home Assistant for a reduced history payload.", default=False
    )
    no_attributes: bool = setting(
        Bool(), "Ask Home Assistant to omit entity attributes.", default=False
    )
    significant_changes_only: bool = setting(
        Bool(), "Ask Home Assistant for significant changes only.", default=False
    )
    skip_initial_state: bool = setting(
        Bool(), "Omit the state in effect at midnight that opens each entity's day.", default=True
    )


@dataclass(frozen=True)
class EntitySettings:
    include_unknown: bool = setting(
        Bool(), "Request history for entities currently in state unknown.", default=True
    )
    include_unavailable: bool = setting(
        Bool(), "Request history for entities currently in state unavailable.", default=True
    )
    exclude_domains: tuple[str, ...] = setting(
        Names(), "Entity domains to exclude, for example update or button.", default=()
    )
    exclude_patterns: tuple[str, ...] = setting(
        Names(),
        "Case-sensitive glob patterns of entity IDs to exclude, for example sensor.*_debug.",
        default=(),
    )


@dataclass(frozen=True)
class RecorderSettings:
    purge_keep_days: int | None = setting(
        OptionalValue(Integer(1)),
        "Recorder retention of Home Assistant in days (purge_keep_days, 10 unless "
        "changed there); unset means unknown, and an empty day then fails.",
        example=10,
    )


@dataclass(frozen=True)
class StorageSettings:
    temp_dir: Path = setting(
        PathValue(),
        "Directory for the working files of a run.",
        default_factory=paths.default_temp_root,
    )
    locked_file_retries: int = setting(
        Integer(0), "Retries while another program locks an output file.", default=5
    )
    locked_file_retry_sleep: float = setting(
        Seconds(), "Pause between file-lock retries, in seconds.", default=2.0
    )


@dataclass(frozen=True)
class Config:
    """Everything a run needs, except the access token."""

    homeassistant: HomeAssistantSettings = field(default_factory=HomeAssistantSettings)
    export: ExportSettings = field(default_factory=ExportSettings)
    requests: RequestSettings = field(default_factory=RequestSettings)
    history_request: HistoryRequestSettings = field(default_factory=HistoryRequestSettings)
    entities: EntitySettings = field(default_factory=EntitySettings)
    recorder: RecorderSettings = field(default_factory=RecorderSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)

    @property
    def snapshot_only(self) -> bool:
        """True when no output format is enabled, so no history is requested."""
        return not self.export.formats

    def wants(self, fmt: Format) -> bool:
        return fmt in self.export.formats
