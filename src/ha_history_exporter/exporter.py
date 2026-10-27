"""Core export orchestrator.

One run owns one working directory and one lock on the output directory; see
:mod:`ha_history_exporter.runtime.workspace`. For each day in the export plan:
  1. Stream the day's history into every requested format at once.
  2. Read every artifact back and prove it holds exactly the captured records.
  3. Build the day manifest and publish artifacts and manifest as one
     crash-recoverable day transaction, the manifest last.
  4. Append a line to the run log (metadata/export_runs.jsonl).

An interrupted publication is rolled back before a new run starts, so an
earlier successful day generation remains byte-for-byte intact.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__, fsio
from .archive.store import load_manifest
from .domain.formats import FORMAT_ORDER, Format
from .domain.manifest import (
    Artifact,
    FailedBatch,
    Manifest14,
    ManifestStatus,
    Retention,
    RetentionStatus,
    StoredManifest,
)
from .domain.query import HistoryQuery
from .domain.records import LogicalDigest
from .domain.stored import RecordContract
from .domain.window import DayIdentity, format_iso
from .domain.wire import iter_normalized_history
from .errors import (
    AuthError,
    ConfigError,
    ErrorRecord,
    ExportError,
    Remedy,
    safe_error_record,
)
from .formats import FORMATS, missing_requirements
from .formats.validation import CaptureSummary, validate_artifacts
from .ha_client import HomeAssistantClient
from .planner import ExportPlan
from .runtime.transaction import DayTransaction
from .runtime.workspace import Workspace, open_workspace
from .settings import Config
from .time_utils import local_day_bounds, partial_day_bounds

logger = logging.getLogger(__name__)


def run_export(
    cfg: Config,
    plan: ExportPlan,
    entity_ids: list[str],
    entity_count_total: int,
    client: HomeAssistantClient,
    tz: ZoneInfo,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
) -> int:
    """Execute the export plan.

    Args:
        cfg:               Loaded application config.
        plan:              Pre-built export plan.
        entity_ids:        Entity IDs to export (from /api/states, optional-filtered).
        entity_count_total: Total entities in HA (before optional excludes).
        client:            Authenticated HA REST client.
        tz:                Local timezone.
        dry_run:           If True, only print the plan — make no history calls.
        sleep:             Pause function; injectable so tests stay instant.
        now:               Clock for the partial-day window; injectable so the
                           cut-off is deterministic in tests.

    Returns:
        0 on full success, 1 if any day had errors.
    """
    if dry_run:
        return 0  # Plan was already printed by caller

    if cfg.snapshot_only:
        logger.info(
            "Snapshot-only configuration: no history formats are enabled, "
            "so no daily export or manifest will be created."
        )
        return 0

    # Fail fast: verify pyarrow is available before touching HA or writing files.
    if missing_requirements(cfg.export.formats):
        raise ConfigError(
            "Parquet output is enabled but pyarrow is not installed.",
            details=(
                "Parquet needs pyarrow, a large platform-specific package "
                "that HHE keeps optional. Nothing was requested from Home "
                "Assistant."
            ),
            remedies=(
                Remedy(
                    "Add it to an existing pipx installation:",
                    "pipx inject ha-history-exporter pyarrow",
                ),
                Remedy(
                    "Or install HHE with the extra:",
                    'pip install "ha-history-exporter[parquet]"',
                ),
                Remedy(
                    "Or turn Parquet off:",
                    "hhe config set export.formats jsonl",
                ),
            ),
        )

    days = plan.days_to_export

    if not days:
        logger.info("Nothing to export.")
        return 0

    workspace = open_workspace(cfg)
    logger.debug("Run %s started.", workspace.run_id)
    try:
        return _run_days(
            cfg=cfg,
            days=days,
            partial_days=frozenset(plan.partial_days),
            entity_ids=entity_ids,
            entity_count_total=entity_count_total,
            client=client,
            tz=tz,
            workspace=workspace,
            sleep=sleep,
            now=now,
        )
    finally:
        workspace.close()


@dataclass
class _DayProgress:
    """What one day's attempt has established so far; becomes its manifest."""

    identity: DayIdentity
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


def _manifest(
    cfg: Config,
    progress: _DayProgress,
    *,
    status: ManifestStatus,
    partial: bool,
    entity_count_total: int,
    entity_count_requested: int,
    tz: ZoneInfo,
    error: ErrorRecord | None = None,
) -> Manifest14:
    keep_days = cfg.recorder.purge_keep_days
    if partial:
        retention = Retention(RetentionStatus.PARTIAL_CURRENT, keep_days)
    elif keep_days is not None:
        retention = Retention(RetentionStatus.KNOWN_SAFE, keep_days)
    else:
        retention = Retention(RetentionStatus.UNKNOWN, None)
    return Manifest14(
        identity=progress.identity,
        status=status,
        export_started_at=progress.started_at,
        export_finished_at=format_iso(datetime.now(tz)),
        duration_seconds=round(time.monotonic() - progress.started_monotonic, 1),
        entity_count_current=entity_count_total,
        entity_count_requested=entity_count_requested,
        entity_count_with_history=progress.entity_count_with_history,
        entity_count_zero_history=len(progress.zero_history_entities),
        state_object_count=progress.state_object_count,
        batch_size_entities=cfg.requests.batch_size,
        request_count=progress.request_count,
        failed_request_count=progress.failed_request_count,
        retried_request_count=progress.retried_request_count,
        query=_query(cfg),
        retention=retention,
        artifacts=progress.artifacts if error is None else {},
        zero_history_entities=progress.zero_history_entities,
        failed_batches=tuple(progress.failed_batches),
        error=None if error is None else error.summary,
        error_code=None if error is None else error.code,
        script_version=__version__,
    )


def _run_days(
    cfg: Config,
    days: list[date],
    partial_days: frozenset[date],
    entity_ids: list[str],
    entity_count_total: int,
    client: HomeAssistantClient,
    tz: ZoneInfo,
    workspace: Workspace,
    sleep: Callable[[float], None],
    now: datetime | None = None,
) -> int:
    any_error = False

    for day_idx, day in enumerate(days):
        is_partial = day in partial_days
        if is_partial:
            start_dt, end_dt = partial_day_bounds(day, tz, now)
        else:
            start_dt, end_dt = local_day_bounds(day, tz)
        day_str = str(day)
        identity = DayIdentity.of(day, tz, start_dt, end_dt)

        logger.info(
            "== Day %d/%d: %s  [%s -> %s] ==",
            day_idx + 1,
            len(days),
            day_str,
            identity.start_local,
            identity.end_local,
        )

        # The previous manifest stays untouched until the new generation commits.
        existing_manifest = load_manifest(cfg.layout, identity)
        progress = _DayProgress(
            identity=identity,
            started_at=format_iso(datetime.now(tz)),
            started_monotonic=time.monotonic(),
        )

        build = functools.partial(
            _manifest,
            cfg,
            progress,
            partial=is_partial,
            entity_count_total=entity_count_total,
            entity_count_requested=len(entity_ids),
            tz=tz,
        )
        manifest: Manifest14 | None = None
        try:
            finalized = _export_day(
                cfg=cfg,
                day=day,
                day_str=day_str,
                start_dt=start_dt,
                end_dt=end_dt,
                entity_ids=entity_ids,
                client=client,
                progress=progress,
                tz=tz,
                workspace=workspace,
                sleep=sleep,
            )
            manifest = build(status=ManifestStatus.PARTIAL if is_partial else ManifestStatus.OK)
            manifest_temp = workspace.work_dir / f"{day_str}.manifest.json"
            fsio.atomic_write(manifest_temp, manifest.encode())
            DayTransaction(
                workspace=workspace,
                day=day_str,
                artifacts=[
                    (finalized[fmt], cfg.layout.day_file(day, fmt.value))
                    for fmt in FORMAT_ORDER
                    if fmt in finalized
                ],
                manifest=(manifest_temp, cfg.layout.day_file(day, "manifest.json")),
                locked_file_retries=cfg.storage.locked_file_retries,
                locked_file_retry_sleep=cfg.storage.locked_file_retry_sleep,
            ).commit()
            logger.info(
                "Day %s done - %d state objects, %d/%d entities had history.",
                day_str,
                progress.state_object_count,
                progress.entity_count_with_history,
                len(entity_ids),
            )

        except AuthError as exc:
            logger.error("Day %s aborted due to an authentication error.", day_str)
            manifest = build(status=ManifestStatus.FAILED, error=safe_error_record(exc))
            _save_failure(cfg, day, manifest, existing_manifest)
            raise  # fatal for the whole run
        except Exception as exc:
            logger.error("Day %s failed (%s).", day_str, type(exc).__name__)
            manifest = build(status=ManifestStatus.FAILED, error=safe_error_record(exc))
            _save_failure(cfg, day, manifest, existing_manifest)
            any_error = True
        finally:
            if manifest is not None:
                _append_run_log(cfg, day_str, manifest)

        if day_idx < len(days) - 1:
            logger.debug(
                "Sleeping %.1f s between days ...",
                cfg.requests.sleep_between_days,
            )
            sleep(cfg.requests.sleep_between_days)

    return 1 if any_error else 0


def _save_failure(
    cfg: Config, day: date, manifest: Manifest14, existing: StoredManifest | None
) -> None:
    """Record a failed attempt, unless the day already has a manifest to keep."""
    if existing is not None:
        return
    fsio.atomic_write(
        cfg.layout.day_file(day, "manifest.json"),
        manifest.encode(),
        retries=cfg.storage.locked_file_retries,
        retry_sleep=cfg.storage.locked_file_retry_sleep,
    )


# ── Day-level export ──────────────────────────────────────────────────────────


def _export_day(
    cfg: Config,
    day: date,
    day_str: str,
    start_dt: datetime,
    end_dt: datetime,
    entity_ids: list[str],
    client: HomeAssistantClient,
    progress: _DayProgress,
    tz: ZoneInfo,
    workspace: Workspace,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[Format, Path]:
    """Fetch one day into the working directory and validate every artifact.

    Returns the prepared artifact of every requested format.
    """
    work_dir = workspace.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)

    # A format's value is its file suffix: YYYY-MM-DD.{jsonl,csv,parquet}.
    temp = {fmt: work_dir / f"{day_str}.{fmt.value}" for fmt in FORMAT_ORDER if cfg.wants(fmt)}
    for path in temp.values():
        # A retried day within the same run must not see the previous attempt.
        path.unlink(missing_ok=True)

    fetched = _fetch_day_rows(
        cfg=cfg,
        temp=temp,
        start_dt=start_dt,
        end_dt=end_dt,
        entity_ids=entity_ids,
        client=client,
        progress=progress,
        tz=tz,
        sleep=sleep,
    )

    progress.retried_request_count = fetched.retried
    if fetched.failed_batches:
        progress.failed_batches = fetched.failed_batches
        raise ExportError(
            f"{len(fetched.failed_batches)} batch(es) failed for {day_str}.",
            details=(
                "The day was not written. Every failed batch is recorded in "
                "the day manifest with its position and entity count."
            ),
            remedies=(
                Remedy(
                    "Retry the day with a longer timeout and more retries:",
                    f"hhe export --date {day_str} --timeout 300 --max-retries 5",
                ),
            ),
            context={"day": day_str},
        )

    if fetched.state_count == 0 and cfg.recorder.purge_keep_days is None:
        raise ExportError(
            f"Empty history for {day_str} cannot be verified without Recorder retention.",
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
            context={"day": day_str},
        )

    progress.artifacts = validate_artifacts(
        temp,
        contract=RecordContract(
            requested_entity_ids=frozenset(entity_ids),
            start=start_dt.astimezone(UTC),
            end=end_dt.astimezone(UTC),
            allow_start_state=not cfg.history_request.skip_initial_state,
            timezone=tz,
        ),
        expected=CaptureSummary(rows=fetched.state_count, logical_sha256=fetched.logical_sha256),
    )

    zero_history = sorted(set(entity_ids) - fetched.entities_with_history)
    progress.state_object_count = fetched.state_count
    progress.entity_count_with_history = len(fetched.entities_with_history)
    progress.zero_history_entities = tuple(zero_history)

    if zero_history:
        logger.debug(
            "  %d entities had no history "
            "(excluded from recorder, no changes, or retention exceeded).",
            len(zero_history),
        )

    return temp


@dataclass
class _FetchedDay:
    """What one day's history retrieval produced."""

    state_count: int = 0
    logical_sha256: str = ""
    entities_with_history: set[str] = field(default_factory=set)
    failed_batches: list[FailedBatch] = field(default_factory=list)
    retried: int = 0


def _query(cfg: Config) -> HistoryQuery:
    options = cfg.history_request
    return HistoryQuery(
        minimal_response=options.minimal_response,
        no_attributes=options.no_attributes,
        significant_changes_only=options.significant_changes_only,
        skip_initial_state=options.skip_initial_state,
    )


def _fetch_day_rows(
    cfg: Config,
    temp: dict[Format, Path],
    start_dt: datetime,
    end_dt: datetime,
    entity_ids: list[str],
    client: HomeAssistantClient,
    progress: _DayProgress,
    tz: ZoneInfo,
    sleep: Callable[[float], None],
) -> _FetchedDay:
    """Stream one day of history into every requested format at once."""
    result = _FetchedDay()
    query = _query(cfg)
    digest = LogicalDigest()
    batch_list = list(_chunks(entity_ids, cfg.requests.batch_size))
    total_batches = len(batch_list)

    logger.info(
        "  %d entities -> %d batches of up to %d",
        len(entity_ids),
        total_batches,
        cfg.requests.batch_size,
    )

    with contextlib.ExitStack() as stack:
        writers = [
            stack.enter_context(FORMATS[fmt].open_writer(path)) for fmt, path in temp.items()
        ]
        for batch_idx, batch in enumerate(batch_list, start=1):
            logger.debug("  Batch %d/%d (%d entities) ...", batch_idx, total_batches, len(batch))

            try:
                retries_before = client.total_retries
                payload = client.get_history(
                    start_dt=start_dt,
                    end_dt=end_dt,
                    entity_ids=batch,
                    **query.options(),
                )
                result.retried += client.total_retries - retries_before
                progress.request_count += 1

            except AuthError:
                progress.failed_request_count += 1
                raise
            except Exception as exc:
                error = safe_error_record(
                    exc,
                    unexpected_summary="Unexpected error while fetching this batch.",
                )
                logger.error("  Batch %d failed (%s).", batch_idx, type(exc).__name__)
                result.failed_batches.append(
                    FailedBatch(
                        batch_index=batch_idx,
                        entity_count=len(batch),
                        error_code=error.code,
                        error=error.summary,
                    )
                )
                progress.failed_request_count += 1
                # Continue with remaining batches - day will be marked failed at end.
                if batch_idx < total_batches:
                    sleep(cfg.requests.sleep_between_requests)
                continue

            for record in iter_normalized_history(
                payload,
                requested_entity_ids=batch,
                start=start_dt,
                end=end_dt,
                query=query,
                timezone=tz,
            ):
                for writer in writers:
                    writer.write(record)
                digest.add(record)
                result.entities_with_history.add(record.entity_id)

            if batch_idx < total_batches:
                sleep(cfg.requests.sleep_between_requests)

    result.state_count = digest.rows
    result.logical_sha256 = digest.hexdigest()
    return result


# ── Helpers ───────────────────────────────────────────────────────────────────


def _chunks[T](lst: list[T], n: int) -> Iterator[list[T]]:
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def _append_run_log(cfg: Config, day_str: str, manifest: Manifest14) -> None:
    """Append one line to metadata/export_runs.jsonl."""
    entry = {
        "day": day_str,
        "status": manifest.status.value,
        "started_at": manifest.export_started_at,
        "finished_at": manifest.export_finished_at,
        "state_object_count": manifest.state_object_count,
        "entity_count_requested": manifest.entity_count_requested,
        "entity_count_with_history": manifest.entity_count_with_history,
        "duration_seconds": manifest.duration_seconds,
    }
    try:
        fsio.append_durable(
            cfg.layout.metadata_dir / "export_runs.jsonl",
            (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"),
        )
    except OSError as exc:
        logger.warning("Could not append to export_runs.jsonl (%s).", type(exc).__name__)
