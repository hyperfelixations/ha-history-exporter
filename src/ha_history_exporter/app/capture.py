"""Capturing one day: history into every requested format, then publication.

The day's history is streamed into one writer per requested format, every
artifact is read back and compared with the captured records, the manifest is
built from what the attempt established, and artifacts and manifest are
published as one day transaction. Files of formats that the previous manifest
declared but the new generation no longer contains are retired with it.

The first history request that fails for good fails the day. A failed day is
recorded by a ``failed`` manifest through the same transaction; it replaces
a ``failed`` or ``pending`` manifest and never an ``ok`` or ``partial`` one.
See internal dev doc § "Export run".
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, NoReturn
from zoneinfo import ZoneInfo

from .. import __version__
from ..archive.layout import ArchiveLayout
from ..archive.store import load_manifest
from ..archive.workspace import Workspace
from ..domain.clock import Clock
from ..domain.formats import FORMAT_ORDER, Format
from ..domain.manifest import (
    Artifact,
    FailedBatch,
    Manifest14,
    ManifestStatus,
    Retention,
    RetentionStatus,
    StoredManifest,
)
from ..domain.query import HistoryQuery
from ..domain.records import LogicalDigest
from ..domain.stored import RecordContract
from ..domain.window import DayIdentity, DayWindow, format_iso
from ..domain.wire import iter_normalized_history
from ..errors import (
    AuthError,
    ErrorRecord,
    ExportError,
    HAConnectionError,
    HHEError,
    InternalError,
    Remedy,
    TLSError,
    safe_error_record,
)
from ..formats import FORMATS
from ..formats.validation import CaptureSummary, validate_artifacts
from ..settings import Config
from ..source.protocol import HistoryFetchError, HistorySource
from .publish import publish_day

logger = logging.getLogger(__name__)

#: Failures after which no further request can succeed; they end the run.
RUN_ENDING = (AuthError, HAConnectionError, TLSError)

#: Statuses a failed attempt may replace.
_REPLACEABLE = frozenset({ManifestStatus.FAILED, ManifestStatus.PENDING})


@dataclass(frozen=True)
class CaptureContext:
    """Everything one run's day captures share."""

    cfg: Config
    layout: ArchiveLayout
    workspace: Workspace
    source: HistorySource
    tz: ZoneInfo
    entity_ids: Sequence[str]
    entity_count_total: int
    sleep: Callable[[float], None]
    clock: Clock

    @property
    def query(self) -> HistoryQuery:
        options = self.cfg.history_request
        return HistoryQuery(
            minimal_response=options.minimal_response,
            no_attributes=options.no_attributes,
            significant_changes_only=options.significant_changes_only,
            skip_initial_state=options.skip_initial_state,
        )

    @property
    def formats(self) -> tuple[Format, ...]:
        return tuple(fmt for fmt in FORMAT_ORDER if self.cfg.wants(fmt))


@dataclass
class _Progress:
    """What one attempt has established so far; becomes its manifest."""

    started_at: str
    started_monotonic: float
    request_count: int = 0
    failed_request_count: int = 0
    retried_request_count: int = 0
    state_object_count: int = 0
    entity_count_with_history: int = 0
    zero_history_entities: tuple[str, ...] = ()
    failed_batches: list[FailedBatch] = field(default_factory=list)
    artifacts: dict[Format, Artifact] = field(default_factory=dict)


@dataclass
class _Fetched:
    """What the history requests of one attempt delivered."""

    rows: int = 0
    logical_sha256: str = ""
    entities_with_history: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class DayOutcome:
    manifest: Manifest14
    #: An error that ends the run once this day is recorded.
    fatal: HHEError | None = None


def capture_day(ctx: CaptureContext, window: DayWindow) -> DayOutcome:
    """Capture and publish the day of *window*; a failure is recorded, not raised.

    An interruption is recorded as well and then propagates.
    """
    day, partial = window.day, window.partial
    identity = window.identity()
    existing = load_manifest(ctx.layout, identity)
    progress = _Progress(
        started_at=format_iso(ctx.clock.now().astimezone(ctx.tz)),
        started_monotonic=ctx.clock.monotonic(),
    )
    try:
        prepared = _capture(ctx, window, progress)
        manifest = _manifest(ctx, identity, progress, partial=partial)
        _publish(ctx, day, prepared, manifest, existing)
    except HHEError as exc:
        logger.error("Day %s failed: %s", day, exc.summary)
        failed = _manifest(ctx, identity, progress, partial, safe_error_record(exc))
        _save_failure(ctx, failed, existing)
        return DayOutcome(failed, exc if isinstance(exc, RUN_ENDING) else None)
    except Exception as exc:
        logger.exception("Day %s failed with an unexpected error.", day)
        failed = _manifest(ctx, identity, progress, partial, safe_error_record(exc))
        _save_failure(ctx, failed, existing)
        return DayOutcome(failed, InternalError.during(f"the export of {day}"))
    except KeyboardInterrupt as exc:
        logger.warning("Day %s was interrupted.", day)
        failed = _manifest(ctx, identity, progress, partial, safe_error_record(exc))
        _save_failure(ctx, failed, existing)
        raise
    logger.info(
        "Day %s done - %d state objects, %d/%d entities had history.",
        day,
        progress.state_object_count,
        progress.entity_count_with_history,
        len(ctx.entity_ids),
    )
    return DayOutcome(manifest)


def _manifest(
    ctx: CaptureContext,
    identity: DayIdentity,
    progress: _Progress,
    partial: bool,
    error: ErrorRecord | None = None,
) -> Manifest14:
    keep_days = ctx.cfg.recorder.purge_keep_days
    if partial:
        retention = Retention(RetentionStatus.PARTIAL_CURRENT, keep_days)
    elif keep_days is not None:
        retention = Retention(RetentionStatus.KNOWN_SAFE, keep_days)
    else:
        retention = Retention(RetentionStatus.UNKNOWN, None)
    if error is not None:
        status = ManifestStatus.FAILED
    else:
        status = ManifestStatus.PARTIAL if partial else ManifestStatus.OK
    return Manifest14(
        identity=identity,
        status=status,
        export_started_at=progress.started_at,
        export_finished_at=format_iso(ctx.clock.now().astimezone(ctx.tz)),
        duration_seconds=round(ctx.clock.monotonic() - progress.started_monotonic, 1),
        entity_count_current=ctx.entity_count_total,
        entity_count_requested=len(ctx.entity_ids),
        entity_count_with_history=progress.entity_count_with_history,
        entity_count_zero_history=len(progress.zero_history_entities),
        state_object_count=progress.state_object_count,
        batch_size_entities=ctx.cfg.requests.batch_size,
        request_count=progress.request_count,
        failed_request_count=progress.failed_request_count,
        retried_request_count=progress.retried_request_count,
        query=ctx.query,
        retention=retention,
        artifacts=progress.artifacts if error is None else {},
        zero_history_entities=progress.zero_history_entities,
        failed_batches=tuple(progress.failed_batches),
        error=None if error is None else error.summary,
        error_code=None if error is None else error.code,
        script_version=__version__,
    )


def _capture(ctx: CaptureContext, window: DayWindow, progress: _Progress) -> dict[Format, Path]:
    """Fetch the day into the working directory and validate every artifact."""
    day = window.day
    work_dir = ctx.workspace.work_dir
    temp = {fmt: work_dir / f"{day.isoformat()}.{fmt.value}" for fmt in ctx.formats}
    for path in temp.values():
        # A retried day within the same run must not see the previous attempt.
        path.unlink(missing_ok=True)

    fetched = _fetch(ctx, window, temp, progress)

    if fetched.rows == 0 and ctx.cfg.recorder.purge_keep_days is None:
        raise ExportError(
            f"Empty history for {day} cannot be verified without Recorder retention.",
            details=(
                "The response may mean there were no changes, or that Recorder data "
                "has already been purged. No daily artifact was published as complete."
            ),
            remedies=(
                Remedy(
                    "Configure the Home Assistant Recorder retention used by this instance:",
                    "hhe config set recorder.purge_keep_days 14",
                ),
            ),
            context={"day": str(day)},
        )

    progress.artifacts = validate_artifacts(
        temp,
        contract=RecordContract(requested_entity_ids=frozenset(ctx.entity_ids), window=window),
        expected=CaptureSummary(rows=fetched.rows, logical_sha256=fetched.logical_sha256),
    )
    progress.state_object_count = fetched.rows
    progress.entity_count_with_history = len(fetched.entities_with_history)
    progress.zero_history_entities = tuple(
        sorted(set(ctx.entity_ids) - fetched.entities_with_history)
    )
    return temp


def _fetch(
    ctx: CaptureContext,
    window: DayWindow,
    temp: dict[Format, Path],
    progress: _Progress,
) -> _Fetched:
    """Stream the day's history into every requested format at once."""
    fetched = _Fetched()
    query = ctx.query
    batches = list(_chunks(list(ctx.entity_ids), ctx.cfg.requests.batch_size))
    pause = ctx.cfg.requests.sleep_between_requests
    digest = LogicalDigest()
    logger.info(
        "  %d entities -> %d batches of up to %d",
        len(ctx.entity_ids),
        len(batches),
        ctx.cfg.requests.batch_size,
    )

    with contextlib.ExitStack() as stack:
        writers = [
            stack.enter_context(FORMATS[fmt].open_writer(path)) for fmt, path in temp.items()
        ]
        for index, batch in enumerate(batches, start=1):
            logger.debug("  Batch %d/%d (%d entities) ...", index, len(batches), len(batch))
            try:
                call = ctx.source.history(window, batch, query)
            except HistoryFetchError as failure:
                progress.retried_request_count += failure.stats.retries
                progress.failed_request_count += 1
                _batch_failed(progress, failure.error, window.day, index, batches)
            progress.retried_request_count += call.stats.retries
            progress.request_count += 1
            for record in iter_normalized_history(
                call.payload,
                requested_entity_ids=batch,
                window=window,
                query=query,
            ):
                for writer in writers:
                    writer.write(record)
                digest.add(record)
                fetched.entities_with_history.add(record.entity_id)
            if index < len(batches):
                ctx.sleep(pause)

    fetched.rows = digest.rows
    fetched.logical_sha256 = digest.hexdigest()
    return fetched


def _batch_failed(
    progress: _Progress, error: HHEError, day: date, index: int, batches: list[list[str]]
) -> NoReturn:
    """Record the failed batch and end the day; a rejected token is no batch's fault."""
    if isinstance(error, AuthError):
        raise error
    record = safe_error_record(error)
    progress.failed_batches.append(
        FailedBatch(
            batch_index=index,
            entity_count=len(batches[index - 1]),
            error_code=record.code,
            error=record.summary,
        )
    )
    if isinstance(error, RUN_ENDING):
        raise error
    raise ExportError(
        f"History batch {index} of {len(batches)} failed for {day}.",
        details=(
            "The day was not written. The failed batch is recorded in the day manifest "
            f"with its position and entity count: {error.summary}"
        ),
        remedies=(
            Remedy(
                "Retry the day with a longer timeout and more retries:",
                f"hhe export --date {day} --timeout 300 --max-retries 5",
            ),
        ),
        context={"day": str(day)},
    )


def _publish(
    ctx: CaptureContext,
    day: date,
    prepared: dict[Format, Path],
    manifest: Manifest14,
    existing: StoredManifest | None,
) -> None:
    retire = []
    if existing is not None:
        retire = [
            ctx.layout.day_file(day, fmt.value)
            for fmt, name in existing.output_files.items()
            if name is not None and fmt not in prepared
        ]
    publish_day(
        ctx.layout, ctx.workspace, day, prepared, manifest, retire=retire, storage=ctx.cfg.storage
    )


def _save_failure(
    ctx: CaptureContext, manifest: Manifest14, existing: StoredManifest | None
) -> None:
    """Record a failed attempt, unless the day keeps a successful manifest."""
    day = manifest.identity.day
    if existing is not None and existing.status not in _REPLACEABLE:
        logger.info("Day %s keeps its %s manifest.", day, existing.status.value)
        return
    try:
        publish_day(ctx.layout, ctx.workspace, day, {}, manifest, storage=ctx.cfg.storage)
    except (OSError, HHEError) as exc:
        logger.error("The failed attempt for %s could not be recorded: %s", day, exc)


def _chunks[T](items: list[T], size: int) -> Iterator[list[T]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def run_log_entry(manifest: Manifest14) -> dict[str, Any]:
    """The line ``metadata/export_runs.jsonl`` records for one day."""
    return {
        "day": manifest.identity.day.isoformat(),
        "status": manifest.status.value,
        "started_at": manifest.export_started_at,
        "finished_at": manifest.export_finished_at,
        "state_object_count": manifest.state_object_count,
        "entity_count_requested": manifest.entity_count_requested,
        "entity_count_with_history": manifest.entity_count_with_history,
        "duration_seconds": manifest.duration_seconds,
    }
