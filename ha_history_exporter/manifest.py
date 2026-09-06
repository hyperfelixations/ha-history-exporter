"""Per-day manifest: tracks export status, entity counts, and file paths.

Written atomically (temp → rename) so a crashed export never leaves a
partially-written manifest that looks complete.

Schema version 1.1:
  Added start_utc / end_utc, zero_history_entities, schema_version field.

Schema version 1.2:
  JSONL and CSV rows now include a local_offset field (e.g. '+02:00')
  computed per row from last_changed so consumers can convert UTC
  timestamps to local time without consulting the manifest.

Schema version 1.3:
  Parquet output added as post-processing step after JSONL validation.
  Parquet uses a fixed schema with last_changed/last_updated stored as
  timestamp[us, tz=UTC] and attributes as a JSON string column.
  CSV is disabled by default; Parquet is enabled by default.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from . import __version__ as SCRIPT_VERSION

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.3"


@dataclass
class DayManifest:
    # ── Identity ──────────────────────────────────────────────────────────────
    schema_version: str = SCHEMA_VERSION
    status: str = "pending"           # pending | ok | failed
    source: str = "home_assistant_rest_history"
    date: str = ""
    timezone: str = "Europe/Berlin"
    start_local: str = ""
    end_local: str = ""
    start_utc: str = ""
    end_utc: str = ""

    # ── Timing ────────────────────────────────────────────────────────────────
    export_started_at: Optional[str] = None
    export_finished_at: Optional[str] = None
    duration_seconds: Optional[float] = None

    # ── Entity counts ─────────────────────────────────────────────────────────
    entity_count_current: int = 0          # total from /api/states
    entity_count_requested: int = 0        # after optional excludes
    entity_count_with_history: int = 0
    entity_count_zero_history: int = 0

    # ── Data counts ───────────────────────────────────────────────────────────
    state_object_count: int = 0
    batch_size_entities: int = 5
    request_count: int = 0
    failed_request_count: int = 0
    retried_request_count: int = 0

    # ── History request options ───────────────────────────────────────────────
    history_request_options: dict = field(
        default_factory=lambda: {
            "minimal_response": False,
            "no_attributes": False,
            "significant_changes_only": False,
        }
    )

    # ── Output files ──────────────────────────────────────────────────────────
    output_files: dict = field(
        default_factory=lambda: {"jsonl": None, "csv": None, "parquet": None}
    )

    # ── Detail lists ──────────────────────────────────────────────────────────
    zero_history_entities: List[str] = field(default_factory=list)
    failed_batches: List[dict] = field(default_factory=list)

    # ── Misc ──────────────────────────────────────────────────────────────────
    skipped_reason: Optional[str] = None
    error: Optional[str] = None
    script_version: str = SCRIPT_VERSION

    # ── Private (not serialised) ──────────────────────────────────────────────
    _started_ts: Optional[float] = field(default=None, repr=False, compare=False)

    def mark_started(self, tz) -> None:
        """Start a clean export attempt while preserving day identity/config."""
        from .time_utils import format_iso

        now = datetime.now(tz)
        self.export_started_at = format_iso(now)
        self.export_finished_at = None
        self.duration_seconds = None
        self._started_ts = time.monotonic()
        self.status = "pending"
        self.entity_count_with_history = 0
        self.entity_count_zero_history = 0
        self.state_object_count = 0
        self.request_count = 0
        self.failed_request_count = 0
        self.retried_request_count = 0
        self.output_files = {"jsonl": None, "csv": None, "parquet": None}
        self.zero_history_entities = []
        self.failed_batches = []
        self.skipped_reason = None
        self.error = None

    def mark_finished(self, tz, status: str = "ok") -> None:
        from .time_utils import format_iso
        self.export_finished_at = format_iso(datetime.now(tz))
        if self._started_ts is not None:
            self.duration_seconds = round(time.monotonic() - self._started_ts, 1)
        self.status = status


def load_or_create(
    day: date,
    tz_name: str,
    start_local: str,
    end_local: str,
    start_utc: str,
    end_utc: str,
    cfg,  # AppConfig
) -> DayManifest:
    """Load existing manifest for *day* or return a fresh one."""
    path = cfg.day_file(day, "manifest.json")
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            known = set(DayManifest.__dataclass_fields__)
            m = DayManifest(**{k: v for k, v in data.items() if k in known})
            logger.debug(
                "Loaded manifest for %s (status=%s).", day, m.status
            )
            return m
        except Exception as exc:
            logger.warning(
                "Could not parse manifest for %s: %s — creating fresh.", day, exc
            )

    return DayManifest(
        date=str(day),
        timezone=tz_name,
        start_local=start_local,
        end_local=end_local,
        start_utc=start_utc,
        end_utc=end_utc,
    )


def save(manifest: DayManifest, cfg, cloud_storage_retry_count: int = 5, cloud_storage_retry_sleep: float = 2.0) -> None:
    """Write manifest atomically to its final path."""
    from .writers import atomic_replace

    path = cfg.day_file(manifest.date if isinstance(manifest.date, str) else str(manifest.date), "manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")

    # Don't include private fields (_started_ts) in output.
    data = {k: v for k, v in asdict(manifest).items() if not k.startswith("_")}
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    atomic_replace(tmp, path, cloud_storage_retry_count, cloud_storage_retry_sleep)
    logger.debug("Manifest saved for %s (status=%s).", manifest.date, manifest.status)


def is_complete(manifest: DayManifest) -> bool:
    """True if this day was exported successfully."""
    return manifest.status == "ok"
