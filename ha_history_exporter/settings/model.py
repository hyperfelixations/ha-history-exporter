"""Runtime configuration consumed by the application layer.

``AppConfig`` is the shape the planner, exporter, and manifest have always
used. It is built by :mod:`ha_history_exporter.settings.resolver`; nothing
below the configuration layer knows where the values came from.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import List, Optional

from ..errors import ConfigError
from . import paths, schema


@dataclass
class HAConfig:
    url_env: str = "HA_URL"
    token_env: str = "HA_TOKEN"
    timezone: str = "Europe/Berlin"


@dataclass
class ExportConfig:
    output_dir: str = field(default_factory=lambda: str(paths.default_output_dir()))
    mode: str = "all_current_entities"
    include_current_day: bool = False
    resume: bool = True
    force: bool = False


@dataclass
class RequestsConfig:
    batch_size_entities: int = 5
    sleep_between_requests_seconds: float = 1.0
    sleep_between_days_seconds: float = 5.0
    request_timeout_seconds: int = 120
    max_retries: int = 3
    backoff_seconds: List[float] = field(default_factory=lambda: [2, 5, 15])


@dataclass
class FormatsConfig:
    jsonl: bool = True
    csv: bool = False
    parquet: bool = False


@dataclass
class StorageConfig:
    use_temp_dir: bool = True
    temp_dir: str = field(default_factory=lambda: str(paths.default_temp_root()))
    cloud_storage_retry_count: int = 5
    cloud_storage_retry_sleep_seconds: float = 2.0


@dataclass
class HistoryRequestConfig:
    minimal_response: bool = False
    no_attributes: bool = False
    significant_changes_only: bool = False


@dataclass
class RecorderConfig:
    export_long_term_statistics: bool = False
    expected_purge_keep_days: Optional[int] = None


@dataclass
class EntitySelectionConfig:
    source: str = "api_states_runtime"
    include_unknown: bool = True
    include_unavailable: bool = True
    include_deleted_from_previous_runs: bool = False
    optional_exclude_patterns: List[str] = field(default_factory=list)
    optional_exclude_domains: List[str] = field(default_factory=list)


@dataclass
class AppConfig:
    home_assistant: HAConfig = field(default_factory=HAConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    requests: RequestsConfig = field(default_factory=RequestsConfig)
    formats: FormatsConfig = field(default_factory=FormatsConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    history_request: HistoryRequestConfig = field(default_factory=HistoryRequestConfig)
    recorder: RecorderConfig = field(default_factory=RecorderConfig)
    entity_selection: EntitySelectionConfig = field(
        default_factory=EntitySelectionConfig
    )
    # Resolved at load time from the environment or the credentials file.
    ha_url: str = ""
    ha_token: str = ""

    @property
    def resolved_temp_dir(self) -> Path:
        """Expand %ENV_VARS% and return the temp directory path."""
        return Path(os.path.expandvars(self.storage.temp_dir))

    @property
    def daily_export_root(self) -> Path:
        """Root directory for daily export files: <output_dir>/exports/daily/"""
        return Path(self.export.output_dir) / "exports" / "daily"

    @property
    def metadata_dir(self) -> Path:
        return Path(self.export.output_dir) / "metadata"

    @property
    def logs_dir(self) -> Path:
        return Path(self.export.output_dir) / "logs"

    @property
    def snapshot_only(self) -> bool:
        """True when no history output format is enabled."""
        return not any((self.formats.jsonl, self.formats.csv, self.formats.parquet))

    def day_dir(self, day) -> Path:
        """Return the directory for a given date: .../YYYY/MM/

        Accepts both a datetime.date object and an ISO string ("2026-06-15").
        """
        d, _ = self._normalize_day(day)
        return self.daily_export_root / str(d.year) / f"{d.month:02d}"

    def day_file(self, day, suffix: str) -> Path:
        """Return the path for a day file, e.g. .../2026/06/2026-06-15.jsonl

        Accepts both a datetime.date object and an ISO string ("2026-06-15").
        """
        d, s = self._normalize_day(day)
        return self.day_dir(d) / f"{s}.{suffix}"

    @staticmethod
    def _normalize_day(day) -> tuple:
        """Return (date_obj, date_str) for any day input (date or ISO str)."""
        if isinstance(day, str):
            return date.fromisoformat(day), day
        return day, str(day)


def validate_config(cfg: AppConfig) -> None:
    """Re-check the numeric contract of an already built configuration."""
    schema.positive_int(
        cfg.requests.batch_size_entities, "requests.batch_size_entities"
    )
    schema.non_negative_number(
        cfg.requests.sleep_between_requests_seconds,
        "requests.sleep_between_requests_seconds",
    )
    schema.non_negative_number(
        cfg.requests.sleep_between_days_seconds,
        "requests.sleep_between_days_seconds",
    )
    schema.positive_int(
        cfg.requests.request_timeout_seconds, "requests.request_timeout_seconds"
    )
    schema.non_negative_int(cfg.requests.max_retries, "requests.max_retries")
    backoff = schema.non_negative_number_list(
        cfg.requests.backoff_seconds, "requests.backoff_seconds"
    )
    if cfg.requests.max_retries > 0 and not backoff:
        raise ConfigError(
            "requests.backoff_seconds must contain at least one value "
            "when requests.max_retries is greater than 0.",
            details=(
                "Retries are enabled but no wait time is configured, so the "
                "client would have no backoff schedule to follow."
            ),
        )
    schema.non_negative_int(
        cfg.storage.cloud_storage_retry_count, "storage.cloud_storage_retry_count"
    )
    schema.non_negative_number(
        cfg.storage.cloud_storage_retry_sleep_seconds,
        "storage.cloud_storage_retry_sleep_seconds",
    )
    schema.optional_positive_int(
        cfg.recorder.expected_purge_keep_days, "recorder.expected_purge_keep_days"
    )
