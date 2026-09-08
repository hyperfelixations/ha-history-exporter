"""The runtime configuration consumed by the application layer.

``Config`` is immutable. It is built once, by
:mod:`ha_history_exporter.settings.resolver`, from every configuration source;
nothing below the configuration layer knows where a value came from, and no
layer can change one afterwards.

The access token is deliberately *not* part of this object. It travels beside
it, so a configuration value can never carry a secret into a log line, a
manifest, or a ``repr``. See internal dev doc, Zugangsdaten.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..layout import ExportLayout


class Format(str, Enum):
    """An output format for exported history."""

    JSONL = "jsonl"
    CSV = "csv"
    PARQUET = "parquet"


#: Formats in their canonical order, used wherever a set is rendered for a user.
FORMAT_ORDER: tuple[Format, ...] = (Format.JSONL, Format.CSV, Format.PARQUET)


def format_list(formats: frozenset[Format]) -> str:
    """Render a format set the way it is written on the command line."""
    return ",".join(fmt.value for fmt in FORMAT_ORDER if fmt in formats) or "none"


@dataclass(frozen=True)
class HomeAssistantSettings:
    url: str = ""


@dataclass(frozen=True)
class ExportSettings:
    output_dir: str = ""
    timezone: str = "Europe/Berlin"
    formats: frozenset[Format] = frozenset({Format.JSONL})


@dataclass(frozen=True)
class RequestSettings:
    batch_size: int = 5
    sleep_between_requests: float = 1.0
    sleep_between_days: float = 5.0
    timeout: int = 120
    max_retries: int = 3
    backoff: tuple[float, ...] = (2.0, 5.0, 15.0)


@dataclass(frozen=True)
class HistoryRequestSettings:
    """Options passed straight through to the Home Assistant history endpoint.

    All three reduce what Home Assistant returns. They default to off, so the
    export is complete unless a user deliberately asks for less.
    """

    minimal_response: bool = False
    no_attributes: bool = False
    significant_changes_only: bool = False


@dataclass(frozen=True)
class EntitySettings:
    include_unknown: bool = True
    include_unavailable: bool = True
    exclude_domains: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecorderSettings:
    purge_keep_days: int | None = None


@dataclass(frozen=True)
class StorageSettings:
    temp_dir: str = ""
    locked_file_retries: int = 5
    locked_file_retry_sleep: float = 2.0


@dataclass(frozen=True)
class Config:
    """Everything one export run needs, except the access token."""

    homeassistant: HomeAssistantSettings = HomeAssistantSettings()
    export: ExportSettings = ExportSettings()
    requests: RequestSettings = RequestSettings()
    history_request: HistoryRequestSettings = HistoryRequestSettings()
    entities: EntitySettings = EntitySettings()
    recorder: RecorderSettings = RecorderSettings()
    storage: StorageSettings = StorageSettings()

    @property
    def layout(self) -> ExportLayout:
        """Every output path below the configured output directory.

        The layout is a contract of its own; see
        :mod:`ha_history_exporter.layout`.
        """
        return ExportLayout(Path(self.export.output_dir))

    @property
    def resolved_temp_dir(self) -> Path:
        """The working directory root, with %ENV_VARS% expanded."""
        return Path(os.path.expandvars(self.storage.temp_dir))

    @property
    def snapshot_only(self) -> bool:
        """True when no output format is enabled, so no history is requested."""
        return not self.export.formats

    def wants(self, fmt: Format) -> bool:
        return fmt in self.export.formats
