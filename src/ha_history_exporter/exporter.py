"""Core export orchestrator.

One run owns one working directory and one lock on the output directory; see
:mod:`ha_history_exporter.runtime.workspace`. For each day in the export plan:
  1. Stream state objects from HA history into the working directory, line by
     line — no in-memory accumulation.
  2. After all batches: validate, convert to Parquet if requested, validate
     again, then promote the enabled formats to their final paths.
  3. Write / update the day manifest.
  4. Append a line to the run log (metadata/export_runs.jsonl).

Recovery:
  - An interrupted run leaves its working directory behind; a later run removes
    it once it is older than the staleness threshold.
  - The manifest then has status != "ok", so the day is exported again.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, List
from zoneinfo import ZoneInfo

from . import manifest as mf
from . import writers
from .errors import (
    AuthError,
    ConfigError,
    ExportError,
    Remedy,
    safe_error_record,
)
from .ha_client import HomeAssistantClient
from .history_records import iter_normalized_history
from .planner import ExportPlan
from .runtime.transaction import DayTransaction
from .runtime.workspace import Workspace, open_workspace
from .settings import Config, Format
from .settings.model import FORMAT_ORDER
from .time_utils import (
    format_iso,
    local_day_bounds,
    partial_day_bounds,
    to_utc,
)
from .validators import ArtifactMetadata, validate_export_artifacts

logger = logging.getLogger(__name__)

#: Physical Parquet layout. Part of the frozen output contract: a fixture small
#: enough to fit one row group cannot show a change here, so it is pinned by
#: name. See internal dev doc, Ausgabedateien.
PARQUET_ROW_GROUP_SIZE = 200_000
PARQUET_COMPRESSION = "snappy"


def run_export(
    cfg: Config,
    plan: ExportPlan,
    entity_ids: List[str],
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
    if cfg.wants(Format.PARQUET):
        try:
            import pyarrow  # noqa: F401
        except ImportError as exc:
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
            ) from exc

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
    days: List[date],
    partial_days: frozenset[date],
    entity_ids: List[str],
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
    entity_ids: List[str],
    client: HomeAssistantClient,
    manifest: mf.DayManifest,
    tz: ZoneInfo,
    workspace: Workspace,
    sleep: Callable[[float], None] = time.sleep,
) -> _FinalizedDay:
    """Fetch one day, validate it, and publish the requested formats."""
    work_dir = workspace.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)

    # A format's value is its file suffix; the frozen output contract names the
    # files YYYY-MM-DD.{jsonl,csv,parquet}.
    temp = {fmt: work_dir / f"{day_str}.{fmt.value}" for fmt in FORMAT_ORDER}
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
        expected_rows=fetched.state_count,
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
    manifest.output_files = {
        fmt.value: finalized.written.get(fmt) for fmt in FORMAT_ORDER
    }
    manifest.artifacts = {
        fmt.value: asdict(finalized.artifacts[fmt])
        if cfg.wants(fmt)
        else None
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
    entities_with_history: set = field(default_factory=set)
    failed_batches: list = field(default_factory=list)
    retried: int = 0


@dataclass
class _FinalizedDay:
    """Validated artifacts and the formats published from them."""

    written: dict[Format, str]
    prepared: dict[Format, Path]
    artifacts: dict[Format, ArtifactMetadata]


def _fetch_day_rows(
    cfg: Config,
    temp: dict,
    start_dt: datetime,
    end_dt: datetime,
    entity_ids: List[str],
    client: HomeAssistantClient,
    manifest: mf.DayManifest,
    tz: ZoneInfo,
    sleep: Callable[[float], None],
) -> _FetchedDay:
    """Stream one day of history into the working directory.

    JSONL is always written: it is the intermediate every other format is
    derived from, whether or not it was requested as an output.
    """
    result = _FetchedDay()
    batch_list = list(_chunks(entity_ids, cfg.requests.batch_size))
    total_batches = len(batch_list)

    logger.info(
        "  %d entities -> %d batches of up to %d",
        len(entity_ids),
        total_batches,
        cfg.requests.batch_size,
    )

    csv_writer = (
        writers.CsvWriter(temp[Format.CSV])
        if cfg.wants(Format.CSV)
        else contextlib.nullcontext(None)
    )

    with writers.JsonlWriter(temp[Format.JSONL]) as jw, csv_writer as cw:
        for batch_idx, batch in enumerate(batch_list, start=1):
            logger.debug(
                "  Batch %d/%d (%d entities) ...", batch_idx, total_batches, len(batch)
            )

            try:
                retries_before = client.total_retries
                payload = client.get_history(
                    start_dt=start_dt,
                    end_dt=end_dt,
                    entity_ids=batch,
                    minimal_response=cfg.history_request.minimal_response,
                    no_attributes=cfg.history_request.no_attributes,
                    significant_changes_only=cfg.history_request.significant_changes_only,
                    skip_initial_state=cfg.history_request.skip_initial_state,
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
                logger.error(
                    "  Batch %d failed (%s).", batch_idx, type(exc).__name__
                )
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

            rows = iter_normalized_history(
                payload,
                requested_entity_ids=batch,
                start=start_dt,
                end=end_dt,
                settings=cfg.history_request,
                local_timezone=tz,
            )
            batch_state_count = 0
            for record in rows:
                enriched = record.as_json_dict()
                jw.write(enriched)
                if cw is not None:
                    cw.write(enriched)
                result.entities_with_history.add(record.entity_id)
                batch_state_count += 1

            result.state_count += batch_state_count

            if batch_idx < total_batches:
                sleep(cfg.requests.sleep_between_requests)

    return result


def _finalize_day(
    cfg: Config,
    day: date,
    temp: dict,
    expected_rows: int,
    requested_entity_ids: List[str],
    start_dt: datetime,
    end_dt: datetime,
    tz: ZoneInfo,
) -> _FinalizedDay:
    """Validate, derive, and publish; return the file name per written format.

    Parquet is derived from the already validated JSONL, so it can only ever
    contain data that passed its own check first. Nothing reaches a final path
    before every local validation has passed.
    """
    if cfg.wants(Format.PARQUET):
        logger.info("  Converting JSONL -> Parquet ...")
        writers.convert_jsonl_to_parquet(
            src=temp[Format.JSONL],
            dst=temp[Format.PARQUET],
            row_group_size=PARQUET_ROW_GROUP_SIZE,
            compression=PARQUET_COMPRESSION,
        )
    validation_paths = {Format.JSONL: temp[Format.JSONL]}
    validation_paths.update(
        {fmt: temp[fmt] for fmt in FORMAT_ORDER if cfg.wants(fmt)}
    )
    artifacts = validate_export_artifacts(
        validation_paths,
        expected_rows=expected_rows,
        requested_entity_ids=requested_entity_ids,
        start=start_dt,
        end=end_dt,
        settings=cfg.history_request,
        local_timezone=tz,
    )

    written: dict[Format, str] = {}
    prepared = {}
    for fmt in FORMAT_ORDER:
        if not cfg.wants(fmt):
            temp[fmt].unlink(missing_ok=True)
            continue
        final = cfg.layout.day_file(day, fmt.value)
        prepared[fmt] = temp[fmt]
        written[fmt] = final.name
    return _FinalizedDay(
        written=written,
        prepared=prepared,
        artifacts=artifacts,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _chunks(lst: list, n: int):
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
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning(
            "Could not append to export_runs.jsonl (%s).", type(exc).__name__
        )
