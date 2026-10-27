"""Core export orchestrator.

One run owns one working directory and one lock on the output directory; see
:mod:`ha_history_exporter.runtime.workspace`. For each day in the export plan:
  1. Stream state objects from HA history into the working directory, line by
     line — no in-memory accumulation.
  2. Derive requested formats from JSONL and validate every prepared artifact.
  3. Prepare the successful manifest in the working directory.
  4. Publish all artifacts and the manifest as one crash-recoverable day
     transaction, making the manifest visible last.
  5. Append a line to the run log (metadata/export_runs.jsonl).

Recovery:
  - An interrupted run leaves its working directory behind; a later run removes
    it once it is older than the staleness threshold.
  - An interrupted publication is rolled back before a new run starts, so an
    earlier successful day generation remains byte-for-byte intact.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import manifest as mf
from .domain.formats import FORMAT_ORDER, Format
from .domain.query import HistoryQuery
from .domain.records import LogicalDigest
from .domain.stored import RecordContract
from .domain.wire import iter_normalized_history
from .errors import (
    AuthError,
    ConfigError,
    ExportError,
    Remedy,
    safe_error_record,
)
from .formats import FORMATS, missing_requirements
from .formats.validation import ArtifactMetadata, CaptureSummary, validate_artifacts
from .ha_client import HomeAssistantClient
from .planner import ExportPlan
from .runtime.transaction import DayTransaction
from .runtime.workspace import Workspace, open_workspace
from .settings import Config
from .time_utils import (
    format_iso,
    local_day_bounds,
    partial_day_bounds,
    to_utc,
)

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

        logger.info(
            "== Day %d/%d: %s  [%s -> %s] ==",
            day_idx + 1,
            len(days),
            day_str,
            format_iso(start_dt),
            format_iso(end_dt),
        )

        existing_manifest = mf.load_existing(
            day=day,
            tz_name=cfg.export.timezone,
            start_local=format_iso(start_dt),
            end_local=format_iso(end_dt),
            start_utc=format_iso(to_utc(start_dt)),
            end_utc=format_iso(to_utc(end_dt)),
            cfg=cfg,
        )
        # Create a new attempt from the active request. The previous manifest
        # stays untouched until the whole new generation commits.
        m = mf.create(
            day=day,
            tz_name=cfg.export.timezone,
            start_local=format_iso(start_dt),
            end_local=format_iso(end_dt),
            start_utc=format_iso(to_utc(start_dt)),
            end_utc=format_iso(to_utc(end_dt)),
            cfg=cfg,
        )
        m.entity_count_current = entity_count_total
        m.entity_count_requested = len(entity_ids)
        m.batch_size_entities = cfg.requests.batch_size
        m.history_request_options = {
            "minimal_response": cfg.history_request.minimal_response,
            "no_attributes": cfg.history_request.no_attributes,
            "significant_changes_only": cfg.history_request.significant_changes_only,
            "skip_initial_state": cfg.history_request.skip_initial_state,
        }
        m.capture_profile = mf.capture_profile(cfg.history_request)
        m.retention = {
            "status": (
                "partial_current"
                if is_partial
                else "known_safe"
                if cfg.recorder.purge_keep_days is not None
                else "unknown"
            ),
            "purge_keep_days": cfg.recorder.purge_keep_days,
        }
        m.mark_started(tz)

        try:
            finalized = _export_day(
                cfg=cfg,
                day=day,
                day_str=day_str,
                start_dt=start_dt,
                end_dt=end_dt,
                entity_ids=entity_ids,
                client=client,
                manifest=m,
                tz=tz,
                workspace=workspace,
                sleep=sleep,
            )
            m.mark_finished(tz, status="partial" if is_partial else "ok")
            manifest_temp = workspace.work_dir / f"{day_str}.manifest.json"
            mf.write_file(m, manifest_temp)
            DayTransaction(
                workspace=workspace,
                day=day_str,
                artifacts=[
                    (
                        finalized.prepared[fmt],
                        cfg.layout.day_file(day, fmt.value),
                    )
                    for fmt in FORMAT_ORDER
                    if fmt in finalized.prepared
                ],
                manifest=(manifest_temp, cfg.layout.day_file(day, "manifest.json")),
                locked_file_retries=cfg.storage.locked_file_retries,
                locked_file_retry_sleep=cfg.storage.locked_file_retry_sleep,
            ).commit()
            logger.info(
                "Day %s done - %d state objects, %d/%d entities had history.",
                day_str,
                m.state_object_count,
                m.entity_count_with_history,
                len(entity_ids),
            )

        except AuthError as exc:
            logger.error("Day %s aborted due to an authentication error.", day_str)
            m.mark_finished(tz, status="failed")
            error = safe_error_record(exc)
            m.error_code = error.code
            m.error = error.summary
            m.output_files = {fmt.value: None for fmt in FORMAT_ORDER}
            m.artifacts = {fmt.value: None for fmt in FORMAT_ORDER}
            if existing_manifest is None:
                mf.save(
                    m,
                    cfg,
                    cfg.storage.locked_file_retries,
                    cfg.storage.locked_file_retry_sleep,
                )
            raise  # fatal for the whole run
        except Exception as exc:
            error = safe_error_record(exc)
            logger.error("Day %s failed (%s).", day_str, type(exc).__name__)
            m.mark_finished(tz, status="failed")
            m.error_code = error.code
            m.error = error.summary
            m.output_files = {fmt.value: None for fmt in FORMAT_ORDER}
            m.artifacts = {fmt.value: None for fmt in FORMAT_ORDER}
            if existing_manifest is None:
                mf.save(
                    m,
                    cfg,
                    cfg.storage.locked_file_retries,
                    cfg.storage.locked_file_retry_sleep,
                )
            any_error = True
        finally:
            _append_run_log(cfg, day_str, m)

        if day_idx < len(days) - 1:
            logger.debug(
                "Sleeping %.1f s between days ...",
                cfg.requests.sleep_between_days,
            )
            sleep(cfg.requests.sleep_between_days)

    return 1 if any_error else 0


# ── Day-level export ──────────────────────────────────────────────────────────


def _export_day(
    cfg: Config,
    day: date,
    day_str: str,
    start_dt: datetime,
    end_dt: datetime,
    entity_ids: list[str],
    client: HomeAssistantClient,
    manifest: mf.DayManifest,
    tz: ZoneInfo,
    workspace: Workspace,
    sleep: Callable[[float], None] = time.sleep,
) -> _FinalizedDay:
    """Fetch one day into the working directory and validate every artifact."""
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
        manifest=manifest,
        tz=tz,
        sleep=sleep,
    )

    manifest.retried_request_count = fetched.retried
    if fetched.failed_batches:
        manifest.failed_batches = fetched.failed_batches
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

    finalized = _finalize_day(
        cfg=cfg,
        day=day,
        temp=temp,
        fetched=fetched,
        requested_entity_ids=entity_ids,
        start_dt=start_dt,
        end_dt=end_dt,
        tz=tz,
    )

    zero_history = sorted(set(entity_ids) - fetched.entities_with_history)
    manifest.state_object_count = fetched.state_count
    manifest.entity_count_with_history = len(fetched.entities_with_history)
    manifest.entity_count_zero_history = len(zero_history)
    manifest.zero_history_entities = zero_history
    manifest.output_files = {fmt.value: finalized.written.get(fmt) for fmt in FORMAT_ORDER}
    manifest.artifacts = {
        fmt.value: asdict(finalized.artifacts[fmt]) if fmt in finalized.artifacts else None
        for fmt in FORMAT_ORDER
    }

    if zero_history:
        logger.debug(
            "  %d entities had no history "
            "(excluded from recorder, no changes, or retention exceeded).",
            len(zero_history),
        )

    return finalized


@dataclass
class _FetchedDay:
    """What one day's history retrieval produced."""

    state_count: int = 0
    logical_sha256: str = ""
    entities_with_history: set[str] = field(default_factory=set)
    failed_batches: list[dict[str, object]] = field(default_factory=list)
    retried: int = 0


@dataclass
class _FinalizedDay:
    """Validated artifacts and the formats published from them."""

    written: dict[Format, str]
    prepared: dict[Format, Path]
    artifacts: dict[Format, ArtifactMetadata]


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
    manifest: mf.DayManifest,
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
                manifest.request_count += 1

            except AuthError:
                manifest.failed_request_count += 1
                raise
            except Exception as exc:
                error = safe_error_record(
                    exc,
                    unexpected_summary="Unexpected error while fetching this batch.",
                )
                logger.error("  Batch %d failed (%s).", batch_idx, type(exc).__name__)
                result.failed_batches.append(
                    {
                        "batch_index": batch_idx,
                        "entity_count": len(batch),
                        "error_code": error.code,
                        "error": error.summary,
                    }
                )
                manifest.failed_request_count += 1
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


def _finalize_day(
    cfg: Config,
    day: date,
    temp: dict[Format, Path],
    fetched: _FetchedDay,
    requested_entity_ids: list[str],
    start_dt: datetime,
    end_dt: datetime,
    tz: ZoneInfo,
) -> _FinalizedDay:
    """Prove that every prepared artifact holds exactly the captured records.

    Nothing reaches a final path before this read-back has passed.
    """
    artifacts = validate_artifacts(
        temp,
        contract=RecordContract(
            requested_entity_ids=frozenset(requested_entity_ids),
            start=start_dt.astimezone(UTC),
            end=end_dt.astimezone(UTC),
            allow_start_state=not cfg.history_request.skip_initial_state,
            timezone=tz,
        ),
        expected=CaptureSummary(rows=fetched.state_count, logical_sha256=fetched.logical_sha256),
    )
    return _FinalizedDay(
        written={fmt: cfg.layout.day_file(day, fmt.value).name for fmt in temp},
        prepared=dict(temp),
        artifacts=artifacts,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _chunks[T](lst: list[T], n: int) -> Iterator[list[T]]:
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def _append_run_log(cfg: Config, day_str: str, m: mf.DayManifest) -> None:
    """Append one line to metadata/export_runs.jsonl."""
    try:
        cfg.layout.metadata_dir.mkdir(parents=True, exist_ok=True)
        log_path = cfg.layout.metadata_dir / "export_runs.jsonl"
        entry = {
            "day": day_str,
            "status": m.status,
            "started_at": m.export_started_at,
            "finished_at": m.export_finished_at,
            "state_object_count": m.state_object_count,
            "entity_count_requested": m.entity_count_requested,
            "entity_count_with_history": m.entity_count_with_history,
            "duration_seconds": m.duration_seconds,
        }
        with log_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("Could not append to export_runs.jsonl (%s).", type(exc).__name__)
