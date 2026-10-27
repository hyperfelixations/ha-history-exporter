"""Per-day manifests: schema 1.4 written, schemata 1.1 to 1.4 read.

A manifest is the durable record that a day was captured. Reading is
fail-closed: anything outside the versioned contract is rejected, never
repaired. Schema 1.4 is parsed into :class:`Manifest14`; the legacy schemata
into :class:`LegacyManifest`, which carries only what those files state.
Serialisation reproduces the 1.4 document byte for byte: two-space
indentation, raw Unicode, the field order of :data:`FIELDS_1_4`, no trailing
newline. See internal dev doc § "Manifest contract".
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import PurePath
from typing import Any

from ..errors import ExportError, Remedy
from .formats import FORMAT_ORDER, Format
from .query import HistoryQuery
from .window import DayIdentity

SCHEMA_VERSION = "1.4"
LEGACY_SCHEMA_VERSIONS = frozenset({"1.1", "1.2", "1.3"})
SOURCE = "home_assistant_rest_history"


class ManifestStatus(StrEnum):
    PENDING = "pending"
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class RetentionStatus(StrEnum):
    KNOWN_SAFE = "known_safe"
    UNKNOWN = "unknown"
    PARTIAL_CURRENT = "partial_current"


#: Every field of a schema 1.4 manifest, in document order.
FIELDS_1_4 = (
    "schema_version",
    "status",
    "source",
    "date",
    "timezone",
    "start_local",
    "end_local",
    "start_utc",
    "end_utc",
    "export_started_at",
    "export_finished_at",
    "duration_seconds",
    "entity_count_current",
    "entity_count_requested",
    "entity_count_with_history",
    "entity_count_zero_history",
    "state_object_count",
    "batch_size_entities",
    "request_count",
    "failed_request_count",
    "retried_request_count",
    "history_request_options",
    "capture_profile",
    "retention",
    "output_files",
    "artifacts",
    "zero_history_entities",
    "failed_batches",
    "skipped_reason",
    "error",
    "error_code",
    "script_version",
)
_ONLY_1_4 = frozenset({"capture_profile", "retention", "artifacts", "error_code"})
_LEGACY_FIELDS = frozenset(FIELDS_1_4) - _ONLY_1_4
_COUNTS = (
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
_LEGACY_OPTIONS = ("minimal_response", "no_attributes", "significant_changes_only")
_SHA256_CHARACTERS = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class Artifact:
    """Physical and logical evidence for one published artifact."""

    filename: str
    size_bytes: int
    sha256: str
    row_count: int
    logical_sha256: str


@dataclass(frozen=True)
class FailedBatch:
    batch_index: int
    entity_count: int
    error_code: str
    error: str


@dataclass(frozen=True)
class Retention:
    status: RetentionStatus
    purge_keep_days: int | None


@dataclass(frozen=True)
class Manifest14:
    """A schema 1.4 manifest. ``output_files`` derives from ``artifacts``."""

    identity: DayIdentity
    status: ManifestStatus
    export_started_at: str | None
    export_finished_at: str | None
    duration_seconds: float | None
    entity_count_current: int
    entity_count_requested: int
    entity_count_with_history: int
    entity_count_zero_history: int
    state_object_count: int
    batch_size_entities: int
    request_count: int
    failed_request_count: int
    retried_request_count: int
    query: HistoryQuery
    retention: Retention
    artifacts: Mapping[Format, Artifact]
    zero_history_entities: tuple[str, ...]
    failed_batches: tuple[FailedBatch, ...]
    error: str | None
    error_code: str | None
    script_version: str
    skipped_reason: str | None = None
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    @property
    def output_files(self) -> dict[Format, str | None]:
        return {
            fmt: self.artifacts[fmt].filename if fmt in self.artifacts else None
            for fmt in FORMAT_ORDER
        }

    def document(self) -> dict[str, Any]:
        """The manifest as a JSON-ready mapping in document order."""
        identity = self.identity
        values: dict[str, Any] = {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "source": SOURCE,
            "date": identity.day.isoformat(),
            "timezone": identity.timezone,
            "start_local": identity.start_local,
            "end_local": identity.end_local,
            "start_utc": identity.start_utc,
            "end_utc": identity.end_utc,
            "export_started_at": self.export_started_at,
            "export_finished_at": self.export_finished_at,
            "duration_seconds": self.duration_seconds,
            **{name: getattr(self, name) for name in _COUNTS},
            "history_request_options": self.query.options(),
            "capture_profile": self.query.capture_profile(),
            "retention": {
                "status": self.retention.status.value,
                "purge_keep_days": self.retention.purge_keep_days,
            },
            "output_files": {fmt.value: name for fmt, name in self.output_files.items()},
            "artifacts": {
                fmt.value: asdict(self.artifacts[fmt]) if fmt in self.artifacts else None
                for fmt in FORMAT_ORDER
            },
            "zero_history_entities": list(self.zero_history_entities),
            "failed_batches": [asdict(batch) for batch in self.failed_batches],
            "skipped_reason": self.skipped_reason,
            "error": self.error,
            "error_code": self.error_code,
            "script_version": self.script_version,
        }
        return {name: values[name] for name in FIELDS_1_4}

    def encode(self) -> bytes:
        """The manifest file content; refuses a manifest its own parser would reject."""
        document = self.document()
        parse_manifest(document, self.identity)
        return json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class LegacyManifest:
    """A schema 1.1 to 1.3 manifest: read-only, carrying what the file states."""

    schema_version: str
    status: ManifestStatus
    output_files: Mapping[Format, str | None]
    state_object_count: int
    script_version: str


type StoredManifest = Manifest14 | LegacyManifest


def invalid_manifest(day: date, reason: str) -> ExportError:
    return ExportError(
        f"The manifest for {day} is invalid: {reason}.",
        details=(
            "HHE will not overwrite or trust an inconsistent manifest. The "
            "existing export remains untouched, including when --force is used."
        ),
        remedies=(Remedy("Inspect the manifest and restore or remove it deliberately."),),
        context={"day": str(day)},
    )


def decode_manifest(raw: bytes, expected: DayIdentity) -> StoredManifest:
    """Parse manifest file content; any defect is an :func:`invalid_manifest`."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise invalid_manifest(expected.day, "file is not valid JSON") from exc
    return parse_manifest(data, expected)


def parse_manifest(data: object, expected: DayIdentity) -> StoredManifest:
    """Parse a decoded manifest written for the day *expected* identifies."""
    return _Parser(data, expected).parse()


class _Parser:
    def __init__(self, data: object, expected: DayIdentity) -> None:
        self.expected = expected
        self.day = expected.day
        if not isinstance(data, dict):
            raise self.fail("top level must be an object")
        self.data: dict[str, Any] = data

    def fail(self, reason: str) -> ExportError:
        return invalid_manifest(self.day, reason)

    # ── shared fields ─────────────────────────────────────────────────────────

    def parse(self) -> StoredManifest:
        data = self.data
        version = data.get("schema_version")
        if version != SCHEMA_VERSION and version not in LEGACY_SCHEMA_VERSIONS:
            raise self.fail("schema version is not supported")
        legacy = version != SCHEMA_VERSION
        fields = _LEGACY_FIELDS if legacy else frozenset(FIELDS_1_4)
        if fields - set(data):
            raise self.fail("required fields are missing")
        if set(data) - fields:
            raise self.fail("unknown field is present")

        try:
            status = ManifestStatus(data["status"])
        except ValueError:
            raise self.fail("status is not supported") from None
        if data["source"] != SOURCE:
            raise self.fail("source is not supported")
        if data["date"] != self.day.isoformat():
            raise self.fail("date does not match the target day")
        if data["timezone"] != self.expected.timezone:
            raise self.fail("timezone does not match the export")
        self._boundaries(status)
        for name in ("export_started_at", "export_finished_at"):
            if data[name] is not None:
                self._aware(data[name], name)
        duration = data["duration_seconds"]
        if duration is not None and (
            isinstance(duration, bool) or not isinstance(duration, int | float) or duration < 0
        ):
            raise self.fail("duration_seconds must be non-negative or null")
        counts = {name: self._count(name) for name in _COUNTS}
        options = self._options(legacy)
        output_files = self._output_files()
        zero_history = data["zero_history_entities"]
        if not isinstance(zero_history, list) or any(
            not isinstance(entity, str) or not entity for entity in zero_history
        ):
            raise self.fail("zero_history_entities must be a string list")
        if not isinstance(data["failed_batches"], list) or any(
            not isinstance(batch, dict) for batch in data["failed_batches"]
        ):
            raise self.fail("failed_batches must be an object list")
        for name in ("skipped_reason", "error"):
            self._optional_text(name)
        script_version = data["script_version"]
        if not isinstance(script_version, str) or not script_version:
            raise self.fail("script_version must be a non-empty string")

        if legacy:
            return LegacyManifest(
                schema_version=version,
                status=status,
                output_files=output_files,
                state_object_count=counts["state_object_count"],
                script_version=script_version,
            )
        return self._manifest_1_4(status, counts, options, output_files, zero_history)

    def _aware(self, value: Any, name: str) -> datetime:
        if not isinstance(value, str) or not value:
            raise self.fail(f"{name} must be a timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise self.fail(f"{name} must be valid ISO 8601") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise self.fail(f"{name} needs a timezone")
        return parsed

    def _boundaries(self, status: ManifestStatus) -> None:
        data = self.data
        expected = self.expected
        parsed = {
            name: self._aware(data[name], name)
            for name in ("start_local", "end_local", "start_utc", "end_utc")
        }
        if parsed["start_local"] != parsed["start_utc"]:
            raise self.fail("start_local and start_utc disagree")
        if parsed["end_local"] != parsed["end_utc"]:
            raise self.fail("end_local and end_utc disagree")
        if data["start_local"] != expected.start_local or data["start_utc"] != expected.start_utc:
            raise self.fail("start boundary does not match the target day")
        if status is ManifestStatus.OK and (
            data["end_local"] != expected.end_local or data["end_utc"] != expected.end_utc
        ):
            raise self.fail("end boundary does not match a complete day")
        expected_end = self._aware(expected.end_utc, "expected end")
        if not parsed["start_utc"] < parsed["end_utc"] <= expected_end:
            raise self.fail("end boundary is outside the target day")

    def _count(self, name: str) -> int:
        value = self.data[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise self.fail(f"{name} must be a non-negative integer")
        if name == "batch_size_entities" and value == 0:
            raise self.fail("batch_size_entities must be greater than zero")
        return value

    def _options(self, legacy: bool) -> dict[str, bool]:
        options = self.data["history_request_options"]
        if not isinstance(options, dict):
            raise self.fail("history_request_options must be an object")
        names = set(_LEGACY_OPTIONS) if legacy else set(HistoryQuery().options())
        if set(options) != names:
            raise self.fail("history_request_options has the wrong fields")
        for name in names:
            if not isinstance(options[name], bool):
                raise self.fail(f"history_request_options.{name} must be boolean")
        return options

    def _basename(self, value: Any, suffix: str, name: str) -> str | None:
        if value is None:
            return None
        if (
            not isinstance(value, str)
            or not value
            or PurePath(value).name != value
            or "/" in value
            or "\\" in value
            or value != f"{self.day.isoformat()}.{suffix}"
        ):
            raise self.fail(f"{name} must contain safe day basenames")
        return value

    def _output_files(self) -> dict[Format, str | None]:
        files = self.data["output_files"]
        if not isinstance(files, dict) or set(files) != {fmt.value for fmt in FORMAT_ORDER}:
            raise self.fail("output_files has the wrong fields")
        return {
            fmt: self._basename(files[fmt.value], fmt.value, "output_files") for fmt in FORMAT_ORDER
        }

    def _optional_text(self, name: str) -> str | None:
        value = self.data[name]
        if value is not None and not isinstance(value, str):
            raise self.fail(f"{name} must be a string or null")
        return value

    # ── schema 1.4 ────────────────────────────────────────────────────────────

    def _manifest_1_4(
        self,
        status: ManifestStatus,
        counts: dict[str, int],
        options: dict[str, bool],
        output_files: dict[Format, str | None],
        zero_history: list[str],
    ) -> Manifest14:
        data = self.data
        query = HistoryQuery(**options)
        if data["capture_profile"] != query.capture_profile():
            raise self.fail("capture_profile contradicts history_request_options")
        retention = self._retention()
        artifacts = self._artifacts(output_files, counts["state_object_count"])
        failed_batches = self._failed_batches()
        error = self._optional_text("error")
        error_code = self._optional_text("error_code")

        if status in (ManifestStatus.OK, ManifestStatus.PARTIAL):
            if not artifacts:
                raise self.fail("successful manifest has no output artifacts")
            if error is not None or error_code is not None:
                raise self.fail("successful manifest contains an error")
            if failed_batches:
                raise self.fail("successful manifest contains failed batches")
            if (
                counts["entity_count_with_history"] + counts["entity_count_zero_history"]
                != counts["entity_count_requested"]
            ):
                raise self.fail("entity outcome counts are inconsistent")
            if (
                status is ManifestStatus.OK
                and counts["state_object_count"] == 0
                and retention.status is RetentionStatus.UNKNOWN
            ):
                raise self.fail("empty successful manifest has unknown retention")
        elif status is ManifestStatus.FAILED:
            if not error or not error_code:
                raise self.fail("failed manifest needs an error and error_code")
            if artifacts:
                raise self.fail("failed manifest cannot publish artifacts")

        return Manifest14(
            identity=self.expected,
            status=status,
            export_started_at=data["export_started_at"],
            export_finished_at=data["export_finished_at"],
            duration_seconds=data["duration_seconds"],
            **counts,
            query=query,
            retention=retention,
            artifacts=artifacts,
            zero_history_entities=tuple(zero_history),
            failed_batches=failed_batches,
            error=error,
            error_code=error_code,
            script_version=data["script_version"],
            skipped_reason=data["skipped_reason"],
        )

    def _retention(self) -> Retention:
        retention = self.data["retention"]
        if not isinstance(retention, dict) or set(retention) != {"status", "purge_keep_days"}:
            raise self.fail("retention has the wrong fields")
        try:
            status = RetentionStatus(retention["status"])
        except ValueError:
            raise self.fail("retention.status is not supported") from None
        keep_days = retention["purge_keep_days"]
        if keep_days is not None and (
            isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days <= 0
        ):
            raise self.fail("retention.purge_keep_days must be positive or null")
        if status is RetentionStatus.KNOWN_SAFE and keep_days is None:
            raise self.fail("known_safe retention needs purge_keep_days")
        if status is RetentionStatus.UNKNOWN and keep_days is not None:
            raise self.fail("unknown retention cannot declare purge_keep_days")
        return Retention(status, keep_days)

    def _artifacts(
        self, output_files: dict[Format, str | None], state_object_count: int
    ) -> dict[Format, Artifact]:
        raw = self.data["artifacts"]
        if not isinstance(raw, dict) or set(raw) != {fmt.value for fmt in FORMAT_ORDER}:
            raise self.fail("artifacts has the wrong fields")
        artifacts: dict[Format, Artifact] = {}
        for fmt in FORMAT_ORDER:
            entry = raw[fmt.value]
            if (entry is None) != (output_files[fmt] is None):
                raise self.fail(f"artifacts.{fmt.value} contradicts output_files")
            if entry is None:
                continue
            artifacts[fmt] = self._artifact(fmt, entry, output_files[fmt], state_object_count)
        if len({artifact.logical_sha256 for artifact in artifacts.values()}) > 1:
            raise self.fail("artifact logical digests differ")
        return artifacts

    def _artifact(
        self, fmt: Format, entry: Any, output_file: str | None, state_object_count: int
    ) -> Artifact:
        prefix = f"artifacts.{fmt.value}"
        if not isinstance(entry, dict) or set(entry) != set(Artifact.__dataclass_fields__):
            raise self.fail(f"{prefix} has the wrong fields")
        self._basename(entry["filename"], fmt.value, f"{prefix}.filename")
        for name in ("size_bytes", "row_count"):
            value = entry[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise self.fail(f"{prefix}.{name} must be non-negative")
        for name in ("sha256", "logical_sha256"):
            value = entry[name]
            if not isinstance(value, str) or len(value) != 64 or set(value) - _SHA256_CHARACTERS:
                raise self.fail(f"{prefix}.{name} must be SHA-256")
        if entry["row_count"] != state_object_count:
            raise self.fail(f"{prefix}.row_count is inconsistent")
        if output_file != entry["filename"]:
            raise self.fail(f"{prefix} contradicts output_files")
        return Artifact(**entry)

    def _failed_batches(self) -> tuple[FailedBatch, ...]:
        batches = []
        for batch in self.data["failed_batches"]:
            if set(batch) != set(FailedBatch.__dataclass_fields__):
                raise self.fail("failed_batches entry has the wrong fields")
            index, count = batch["batch_index"], batch["entity_count"]
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or index <= 0
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count <= 0
                or not isinstance(batch["error_code"], str)
                or not batch["error_code"]
                or not isinstance(batch["error"], str)
                or not batch["error"]
            ):
                raise self.fail("failed_batches entry has invalid values")
            batches.append(FailedBatch(**batch))
        return tuple(batches)
