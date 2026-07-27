"""Configuration loading and validation.

Reads a YAML file; sensitive values (URL, token) come exclusively from
environment variables — they are never written to files or logs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from .exceptions import ConfigError

_DEFAULT_OUTPUT_DIR = (
    r"./data"
)


# ── Sub-sections ─────────────────────────────────────────────────────────────

@dataclass
class HAConfig:
    url_env: str = "HA_URL"
    token_env: str = "HA_TOKEN"
    timezone: str = "Europe/Berlin"


@dataclass
class ExportConfig:
    output_dir: str = _DEFAULT_OUTPUT_DIR
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
    backoff_seconds: List[int] = field(default_factory=lambda: [2, 5, 15])


@dataclass
class FormatsConfig:
    jsonl: bool = True
    csv: bool = False
    parquet: bool = True


@dataclass
class StorageConfig:
    use_temp_dir: bool = True
    temp_dir: str = r"%LOCALAPPDATA%\ha_history_export_tmp"
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
    entity_selection: EntitySelectionConfig = field(default_factory=EntitySelectionConfig)
    # Resolved at load time from environment variables
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
        from datetime import date
        if isinstance(day, str):
            return date.fromisoformat(day), day
        return day, str(day)


# ── Loader ────────────────────────────────────────────────────────────────────

def load_config(path: str | Path) -> AppConfig:
    """Load, validate, and return an AppConfig.

    Environment variables referenced by url_env / token_env are resolved here.
    The token is held in memory only — it never appears in logs or files.
    """
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    cfg = AppConfig()

    # ── home_assistant ────────────────────────────────────────────────────────
    ha = raw.get("home_assistant", {})
    cfg.home_assistant = HAConfig(
        url_env=ha.get("url_env", "HA_URL"),
        token_env=ha.get("token_env", "HA_TOKEN"),
        timezone=ha.get("timezone", "Europe/Berlin"),
    )

    # ── export ────────────────────────────────────────────────────────────────
    ex = raw.get("export", {})
    cfg.export = ExportConfig(
        output_dir=ex.get("output_dir", _DEFAULT_OUTPUT_DIR),
        mode=ex.get("mode", "all_current_entities"),
        include_current_day=bool(ex.get("include_current_day", False)),
        resume=bool(ex.get("resume", True)),
        force=bool(ex.get("force", False)),
    )

    # ── requests ──────────────────────────────────────────────────────────────
    rq = raw.get("requests", {})
    cfg.requests = RequestsConfig(
        batch_size_entities=int(rq.get("batch_size_entities", 5)),
        sleep_between_requests_seconds=float(rq.get("sleep_between_requests_seconds", 1.0)),
        sleep_between_days_seconds=float(rq.get("sleep_between_days_seconds", 5.0)),
        request_timeout_seconds=int(rq.get("request_timeout_seconds", 120)),
        max_retries=int(rq.get("max_retries", 3)),
        backoff_seconds=list(rq.get("backoff_seconds", [2, 5, 15])),
    )

    # ── formats ───────────────────────────────────────────────────────────────
    fm = raw.get("formats", {})
    cfg.formats = FormatsConfig(
        jsonl=bool(fm.get("jsonl", True)),
        csv=bool(fm.get("csv", True)),
        parquet=bool(fm.get("parquet", False)),
    )

    # ── storage ───────────────────────────────────────────────────────────────
    st = raw.get("storage", {})
    cfg.storage = StorageConfig(
        use_temp_dir=bool(st.get("use_temp_dir", True)),
        temp_dir=st.get("temp_dir", r"%LOCALAPPDATA%\ha_history_export_tmp"),
        cloud_storage_retry_count=int(st.get("cloud_storage_retry_count", 5)),
        cloud_storage_retry_sleep_seconds=float(st.get("cloud_storage_retry_sleep_seconds", 2.0)),
    )

    # ── history_request ───────────────────────────────────────────────────────
    hr = raw.get("history_request", {})
    cfg.history_request = HistoryRequestConfig(
        minimal_response=bool(hr.get("minimal_response", False)),
        no_attributes=bool(hr.get("no_attributes", False)),
        significant_changes_only=bool(hr.get("significant_changes_only", False)),
    )

    # ── recorder ──────────────────────────────────────────────────────────────
    rc = raw.get("recorder", {})
    cfg.recorder = RecorderConfig(
        export_long_term_statistics=bool(rc.get("export_long_term_statistics", False)),
        expected_purge_keep_days=rc.get("expected_purge_keep_days"),
    )

    # ── entity_selection ─────────────────────────────────────────────────────
    es = raw.get("entity_selection", {})
    cfg.entity_selection = EntitySelectionConfig(
        source=es.get("source", "api_states_runtime"),
        include_unknown=bool(es.get("include_unknown", True)),
        include_unavailable=bool(es.get("include_unavailable", True)),
        include_deleted_from_previous_runs=bool(
            es.get("include_deleted_from_previous_runs", False)
        ),
        optional_exclude_patterns=list(es.get("optional_exclude_patterns", [])),
        optional_exclude_domains=list(es.get("optional_exclude_domains", [])),
    )

    # ── resolve env vars ──────────────────────────────────────────────────────
    ha_url = os.environ.get(cfg.home_assistant.url_env, "").strip()
    ha_token = os.environ.get(cfg.home_assistant.token_env, "").strip()

    if not ha_url:
        raise ConfigError(
            f"Environment variable '{cfg.home_assistant.url_env}' is not set.\n"
            "  Set it to your Home Assistant URL (local or external), e.g.:\n"
            f"    $env:{cfg.home_assistant.url_env} = \"http://homeassistant.local:8123\"\n"
            "  Or for a Tailscale / external address:\n"
            f"    $env:{cfg.home_assistant.url_env} = \"http://100.x.x.x:8123\""
        )
    if not ha_token:
        raise ConfigError(
            f"Environment variable '{cfg.home_assistant.token_env}' is not set.\n"
            "  Create a Long-Lived Access Token in HA → Profile → "
            "Long-Lived Access Tokens, then:\n"
            f"    $env:{cfg.home_assistant.token_env} = \"<your-token>\""
        )

    cfg.ha_url = ha_url.rstrip("/")
    cfg.ha_token = ha_token

    return cfg
