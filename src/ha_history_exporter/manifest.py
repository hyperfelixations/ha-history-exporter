"""Versioned, fail-closed per-day export manifests."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__ as SCRIPT_VERSION
from .errors import ExportError, Remedy
from .settings.model import FORMAT_ORDER, HistoryRequestSettings, RequestSettings

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.4"
LEGACY_SCHEMA_VERSIONS = frozenset({"1.1", "1.2", "1.3"})
SUPPORTED_SCHEMA_VERSIONS = LEGACY_SCHEMA_VERSIONS | {SCHEMA_VERSION}
STATUSES = frozenset({"pending", "ok", "partial", "failed"})


def capture_profile(settings: HistoryRequestSettings) -> dict[str, str]:
    return {
        "response": "minimal" if settings.minimal_response else "full",
        "attributes": (
            "omitted"
            if settings.no_attributes
            else "initial_only"
            if settings.minimal_response
            else "full"
        ),
        "state_changes": (
            "significant_only" if settings.significant_changes_only else "all"
        ),
        "initial_state": "omitted" if settings.skip_initial_state else "included",
    }


@dataclass
class DayManifest:
    schema_version: str = SCHEMA_VERSION
    status: str = "pending"
    source: str = "home_assistant_rest_history"
    date: str = ""
    timezone: str = "Europe/Berlin"
    start_local: str = ""
    end_local: str = ""
    start_utc: str = ""
    end_utc: str = ""

    export_started_at: str | None = None
    export_finished_at: str | None = None
    duration_seconds: float | None = None

    entity_count_current: int = 0
    entity_count_requested: int = 0
    entity_count_with_history: int = 0
    entity_count_zero_history: int = 0

    state_object_count: int = 0
    batch_size_entities: int = RequestSettings().batch_size
    request_count: int = 0
    failed_request_count: int = 0
    retried_request_count: int = 0

    history_request_options: dict[str, bool] = field(
        default_factory=lambda: {
            name: getattr(HistoryRequestSettings(), name)
            for name in (
                "minimal_response",
                "no_attributes",
                "significant_changes_only",
                "skip_initial_state",
            )
        }
    )
    capture_profile: dict[str, str] = field(
        default_factory=lambda: capture_profile(HistoryRequestSettings())
    )
    retention: dict[str, Any] = field(
        default_factory=lambda: {"status": "unknown", "purge_keep_days": None}
    )

    output_files: dict[str, str | None] = field(
        default_factory=lambda: {fmt.value: None for fmt in FORMAT_ORDER}
    )
    artifacts: dict[str, dict[str, Any] | None] = field(
        default_factory=lambda: {fmt.value: None for fmt in FORMAT_ORDER}
    )

    zero_history_entities: list[str] = field(default_factory=list)
    failed_batches: list[dict[str, Any]] = field(default_factory=list)

    skipped_reason: str | None = None
    error: str | None = None
    error_code: str | None = None
    script_version: str = SCRIPT_VERSION

    _started_ts: float | None = field(default=None, repr=False, compare=False)

    def mark_started(self, tz) -> None:
        from .time_utils import format_iso

        self.export_started_at = format_iso(datetime.now(tz))
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
        self.output_files = {fmt.value: None for fmt in FORMAT_ORDER}
        self.artifacts = {fmt.value: None for fmt in FORMAT_ORDER}
        self.zero_history_entities = []
        self.failed_batches = []
        self.skipped_reason = None
        self.error = None
        self.error_code = None

    def mark_finished(self, tz, status: str = "ok") -> None:
        from .time_utils import format_iso

        self.export_finished_at = format_iso(datetime.now(tz))
        if self._started_ts is not None:
            self.duration_seconds = round(time.monotonic() - self._started_ts, 1)
        self.status = status


_PUBLIC_FIELDS = frozenset(
    name for name in DayManifest.__dataclass_fields__ if not name.startswith("_")
)
_V14_ONLY_FIELDS = frozenset(
    {"capture_profile", "retention", "artifacts", "error_code"}
)
_LEGACY_FIELDS = _PUBLIC_FIELDS - _V14_ONLY_FIELDS
_COUNT_FIELDS = (
    "entity_count_current",
    "entity_count_requested",
    "entity_count_with_history",
    "entity_count_zero_history",
    "state_object_count",
    "batch_size_entities",
    "request_count",
    "failed_request_count",
    "retried_request_count",
)


def _invalid(day: date, reason: str) -> ExportError:
    return ExportError(
        f"The manifest for {day} is invalid: {reason}.",
        details=(
            "HHE will not overwrite or trust an inconsistent manifest. The "
            "existing export remains untouched, including when --force is used."
        ),
        remedies=(
            Remedy("Inspect the manifest and restore or remove it deliberately."),
        ),
        context={"day": str(day)},
    )


def _aware_datetime(value: Any, field_name: str, day: date) -> datetime:
    if not isinstance(value, str) or not value:
        raise _invalid(day, f"{field_name} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _invalid(day, f"{field_name} must be valid ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _invalid(day, f"{field_name} needs a timezone")
    return parsed


def _non_negative_int(data: dict[str, Any], name: str, day: date) -> int:
    value = data[name]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid(day, f"{name} must be a non-negative integer")
    if name == "batch_size_entities" and value == 0:
        raise _invalid(day, "batch_size_entities must be greater than zero")
    return value


def _optional_text(value: Any, name: str, day: date) -> None:
    if value is not None and not isinstance(value, str):
        raise _invalid(day, f"{name} must be a string or null")


def _basename(value: Any, name: str, suffix: str, day: date) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or not value
        or Path(value).name != value
        or "/" in value
        or "\\" in value
        or value != f"{day}.{suffix}"
    ):
        raise _invalid(day, f"{name} must contain safe day basenames")


def _validate_options(options: Any, schema_version: str, day: date) -> None:
    if not isinstance(options, dict):
        raise _invalid(day, "history_request_options must be an object")
    names = {
        "minimal_response",
        "no_attributes",
        "significant_changes_only",
    }
    if schema_version == SCHEMA_VERSION:
        names.add("skip_initial_state")
    if set(options) != names:
        raise _invalid(day, "history_request_options has the wrong fields")
    for name in names:
        if not isinstance(options[name], bool):
            raise _invalid(day, f"history_request_options.{name} must be boolean")


def _validate_v14(data: dict[str, Any], day: date) -> None:
    profile = data["capture_profile"]
    expected_profile = capture_profile(
        HistoryRequestSettings(**data["history_request_options"])
    )
    if profile != expected_profile:
        raise _invalid(day, "capture_profile contradicts history_request_options")

    retention = data["retention"]
    if not isinstance(retention, dict) or set(retention) != {
        "status",
        "purge_keep_days",
    }:
        raise _invalid(day, "retention has the wrong fields")
    if retention["status"] not in {"known_safe", "unknown", "partial_current"}:
        raise _invalid(day, "retention.status is not supported")
    keep_days = retention["purge_keep_days"]
    if keep_days is not None and (
        isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days <= 0
    ):
        raise _invalid(day, "retention.purge_keep_days must be positive or null")
    if retention["status"] == "known_safe" and keep_days is None:
        raise _invalid(day, "known_safe retention needs purge_keep_days")
    if retention["status"] == "unknown" and keep_days is not None:
        raise _invalid(day, "unknown retention cannot declare purge_keep_days")
    artifacts = data["artifacts"]
    format_names = {fmt.value for fmt in FORMAT_ORDER}
    if not isinstance(artifacts, dict) or set(artifacts) != format_names:
        raise _invalid(day, "artifacts has the wrong fields")
    logical_digests: set[str] = set()
    for suffix, artifact in artifacts.items():
        if artifact is None:
            continue
        expected_fields = {
            "filename",
            "size_bytes",
            "sha256",
            "row_count",
            "logical_sha256",
        }
        if not isinstance(artifact, dict) or set(artifact) != expected_fields:
            raise _invalid(day, f"artifacts.{suffix} has the wrong fields")
        _basename(artifact["filename"], f"artifacts.{suffix}.filename", suffix, day)
        for name in ("size_bytes", "row_count"):
            value = artifact[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise _invalid(day, f"artifacts.{suffix}.{name} must be non-negative")
        for name in ("sha256", "logical_sha256"):
            value = artifact[name]
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise _invalid(day, f"artifacts.{suffix}.{name} must be SHA-256")
        if artifact["row_count"] != data["state_object_count"]:
            raise _invalid(day, f"artifacts.{suffix}.row_count is inconsistent")
        if data["output_files"][suffix] != artifact["filename"]:
            raise _invalid(day, f"artifacts.{suffix} contradicts output_files")
        logical_digests.add(artifact["logical_sha256"])
    if len(logical_digests) > 1:
        raise _invalid(day, "artifact logical digests differ")
    for suffix in format_names:
        if (artifacts[suffix] is None) != (data["output_files"][suffix] is None):
            raise _invalid(day, f"artifacts.{suffix} contradicts output_files")

    for batch in data["failed_batches"]:
        if set(batch) != {"batch_index", "entity_count", "error_code", "error"}:
            raise _invalid(day, "failed_batches entry has the wrong fields")
        if (
            isinstance(batch["batch_index"], bool)
            or not isinstance(batch["batch_index"], int)
            or batch["batch_index"] <= 0
            or isinstance(batch["entity_count"], bool)
            or not isinstance(batch["entity_count"], int)
            or batch["entity_count"] <= 0
            or not isinstance(batch["error_code"], str)
            or not batch["error_code"]
            or not isinstance(batch["error"], str)
            or not batch["error"]
        ):
            raise _invalid(day, "failed_batches entry has invalid values")

    status = data["status"]
    if status in {"ok", "partial"}:
        if not any(data["output_files"].values()):
            raise _invalid(day, "successful manifest has no output artifacts")
        if data["error"] is not None or data["error_code"] is not None:
            raise _invalid(day, "successful manifest contains an error")
        if data["failed_batches"]:
            raise _invalid(day, "successful manifest contains failed batches")
        if (
            data["entity_count_with_history"] + data["entity_count_zero_history"]
            != data["entity_count_requested"]
        ):
            raise _invalid(day, "entity outcome counts are inconsistent")
        if status == "ok" and data["state_object_count"] == 0 and retention["status"] == "unknown":
            raise _invalid(day, "empty successful manifest has unknown retention")
    elif status == "failed":
        if (
            not isinstance(data["error"], str)
            or not data["error"]
            or not isinstance(data["error_code"], str)
            or not data["error_code"]
        ):
            raise _invalid(day, "failed manifest needs an error and error_code")
        if any(data["output_files"].values()) or any(artifacts.values()):
            raise _invalid(day, "failed manifest cannot publish artifacts")
    _optional_text(data["error_code"], "error_code", day)


def _validate_data(
    data: Any,
    *,
    expected_day: date,
    expected_timezone: str,
    expected_start_local: str,
    expected_end_local: str,
    expected_start_utc: str,
    expected_end_utc: str,
) -> DayManifest:
    if not isinstance(data, dict):
        raise _invalid(expected_day, "top level must be an object")
    schema_version = data.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise _invalid(expected_day, "schema version is not supported")
    fields = _PUBLIC_FIELDS if schema_version == SCHEMA_VERSION else _LEGACY_FIELDS
    missing = fields - set(data)
    unknown = set(data) - fields
    if missing:
        raise _invalid(expected_day, "required fields are missing")
    if unknown:
        raise _invalid(expected_day, "unknown field is present")

    if data["status"] not in STATUSES:
        raise _invalid(expected_day, "status is not supported")
    if data["source"] != "home_assistant_rest_history":
        raise _invalid(expected_day, "source is not supported")
    if data["date"] != str(expected_day):
        raise _invalid(expected_day, "date does not match the target day")
    if data["timezone"] != expected_timezone:
        raise _invalid(expected_day, "timezone does not match the export")

    parsed = {
        name: _aware_datetime(data[name], name, expected_day)
        for name in ("start_local", "end_local", "start_utc", "end_utc")
    }
    if parsed["start_local"].astimezone(timezone.utc) != parsed["start_utc"]:
        raise _invalid(expected_day, "start_local and start_utc disagree")
    if parsed["end_local"].astimezone(timezone.utc) != parsed["end_utc"]:
        raise _invalid(expected_day, "end_local and end_utc disagree")
    if data["start_local"] != expected_start_local or data["start_utc"] != expected_start_utc:
        raise _invalid(expected_day, "start boundary does not match the target day")
    if data["status"] == "ok" and (
        data["end_local"] != expected_end_local or data["end_utc"] != expected_end_utc
    ):
        raise _invalid(expected_day, "end boundary does not match a complete day")
    expected_end = _aware_datetime(expected_end_utc, "expected end", expected_day)
    if not parsed["start_utc"] < parsed["end_utc"] <= expected_end:
        raise _invalid(expected_day, "end boundary is outside the target day")

    for name in ("export_started_at", "export_finished_at"):
        if data[name] is not None:
            _aware_datetime(data[name], name, expected_day)
    duration = data["duration_seconds"]
    if duration is not None and (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or duration < 0
    ):
        raise _invalid(expected_day, "duration_seconds must be non-negative or null")
    for name in _COUNT_FIELDS:
        _non_negative_int(data, name, expected_day)

    _validate_options(data["history_request_options"], schema_version, expected_day)
    format_names = {fmt.value for fmt in FORMAT_ORDER}
    output_files = data["output_files"]
    if not isinstance(output_files, dict) or set(output_files) != format_names:
        raise _invalid(expected_day, "output_files has the wrong fields")
    for suffix, value in output_files.items():
        _basename(value, "output_files", suffix, expected_day)

    zero_entities = data["zero_history_entities"]
    if not isinstance(zero_entities, list) or any(
        not isinstance(entity, str) or not entity for entity in zero_entities
    ):
        raise _invalid(expected_day, "zero_history_entities must be a string list")
    if not isinstance(data["failed_batches"], list) or any(
        not isinstance(batch, dict) for batch in data["failed_batches"]
    ):
        raise _invalid(expected_day, "failed_batches must be an object list")
    for name in ("skipped_reason", "error"):
        _optional_text(data[name], name, expected_day)
    if not isinstance(data["script_version"], str) or not data["script_version"]:
        raise _invalid(expected_day, "script_version must be a non-empty string")
    if schema_version == SCHEMA_VERSION:
        _validate_v14(data, expected_day)

    return DayManifest(**data)


def create(
    day: date,
    tz_name: str,
    start_local: str,
    end_local: str,
    start_utc: str,
    end_utc: str,
    cfg,
) -> DayManifest:
    """Build a fresh schema 1.4 manifest from the active request."""
    settings = cfg.history_request
    return DayManifest(
        date=str(day),
        timezone=tz_name,
        start_local=start_local,
        end_local=end_local,
        start_utc=start_utc,
        end_utc=end_utc,
        history_request_options={
            name: getattr(settings, name)
            for name in (
                "minimal_response",
                "no_attributes",
                "significant_changes_only",
                "skip_initial_state",
            )
        },
        capture_profile=capture_profile(settings),
        retention={
            # ``create`` is reached for complete days only after the planner's
            # retention gate. Partial-day callers replace this with
            # ``partial_current`` before publication.
            "status": (
                "known_safe"
                if cfg.recorder.purge_keep_days is not None
                else "unknown"
            ),
            "purge_keep_days": cfg.recorder.purge_keep_days,
        },
    )


def load_existing(
    day: date,
    tz_name: str,
    start_local: str,
    end_local: str,
    start_utc: str,
    end_utc: str,
    cfg,
) -> DayManifest | None:
    """Load and validate an existing manifest, returning ``None`` if absent."""
    path = cfg.layout.day_file(day, "manifest.json")
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except json.JSONDecodeError as exc:
        raise _invalid(day, "file is not valid JSON") from exc
    except OSError as exc:
        raise ExportError(
            f"Cannot read the manifest for {day}.",
            details=(
                "The manifest is the durable record for this day, so HHE will "
                "not treat an unreadable file as a missing export."
            ),
            remedies=(Remedy("Check the export directory permissions.", "hhe doctor"),),
            context={"day": str(day)},
        ) from exc

    return _validate_data(
        data,
        expected_day=day,
        expected_timezone=tz_name,
        expected_start_local=start_local,
        expected_end_local=end_local,
        expected_start_utc=start_utc,
        expected_end_utc=end_utc,
    )


def load_or_create(
    day: date,
    tz_name: str,
    start_local: str,
    end_local: str,
    start_utc: str,
    end_utc: str,
    cfg,
) -> DayManifest:
    """Load a valid existing manifest, or build a fresh schema 1.4 one."""
    existing = load_existing(
        day,
        tz_name,
        start_local,
        end_local,
        start_utc,
        end_utc,
        cfg,
    )
    return existing or create(
        day,
        tz_name,
        start_local,
        end_local,
        start_utc,
        end_utc,
        cfg,
    )


def write_file(manifest: DayManifest, path: Path) -> None:
    """Validate and durably write a manifest to a non-publication path."""
    try:
        target_day = date.fromisoformat(manifest.date)
    except (TypeError, ValueError) as exc:
        raise _invalid(date.min, "date is not valid ISO 8601") from exc
    data = {key: value for key, value in asdict(manifest).items() if not key.startswith("_")}
    _validate_data(
        data,
        expected_day=target_day,
        expected_timezone=manifest.timezone,
        expected_start_local=manifest.start_local,
        expected_end_local=manifest.end_local,
        expected_start_utc=manifest.start_utc,
        expected_end_utc=manifest.end_utc,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())


def save(
    manifest: DayManifest,
    cfg,
    locked_file_retries: int = 5,
    locked_file_retry_sleep: float = 2.0,
) -> None:
    """Validate and atomically write a manifest to its final path."""
    from .writers import atomic_replace

    try:
        target_day = date.fromisoformat(manifest.date)
    except (TypeError, ValueError) as exc:
        raise _invalid(date.min, "date is not valid ISO 8601") from exc
    path = cfg.layout.day_file(target_day, "manifest.json")
    tmp = path.with_suffix(".json.tmp")
    write_file(manifest, tmp)
    atomic_replace(tmp, path, locked_file_retries, locked_file_retry_sleep)
    logger.debug("Manifest saved for %s (status=%s).", manifest.date, manifest.status)


def is_complete(manifest: DayManifest) -> bool:
    return manifest.status == "ok"
