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
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, List
from zoneinfo import ZoneInfo

from . import manifest as mf
from . import writers
from .errors import AuthError, ConfigError, ExportError, Remedy
from .ha_client import HomeAssistantClient
from .planner import ExportPlan
from .runtime.workspace import Workspace, open_workspace
from .settings import Config, Format
from .settings.model import FORMAT_ORDER
from .time_utils import (
    format_iso,
    local_day_bounds,
    local_offset_str,
    partial_day_bounds,
    to_utc,
)
from .validators import validate_csv, validate_jsonl, validate_parquet

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

        # Warn if this day is likely outside Recorder retention.
        if cfg.recorder.purge_keep_days is not None:
            today = datetime.now(tz).date()
            days_ago = (today - day).days
            if days_ago > cfg.recorder.purge_keep_days:
                logger.warning(
                    "Day %s is %d days ago - likely outside Recorder retention "
                    "(%d days). Only raw REST history is queried; no Long-Term-Statistics.",
                    day_str,
                    days_ago,
                    cfg.recorder.purge_keep_days,
                )

        # Load or create the manifest.
        m = mf.load_or_create(
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
        }
        m.mark_started(tz)
        mf.save(m, cfg, cfg.storage.locked_file_retries, cfg.storage.locked_file_retry_sleep)

        try:
            _export_day(
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
            m.error = str(exc)
            raise  # fatal for the whole run
        except Exception as exc:
            logger.error("Day %s failed: %s", day_str, exc, exc_info=True)
            m.mark_finished(tz, status="failed")
            m.error = str(exc)
            any_error = True
        finally:
            mf.save(
                m,
                cfg,
                cfg.storage.locked_file_retries,
                cfg.storage.locked_file_retry_sleep,
            )
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
) -> None:
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
                "the day manifest with the entities it covered."
            ),
            remedies=(
                Remedy(
                    "Retry the day with a longer timeout and more retries:",
                    f"hhe export --date {day_str} --timeout 300 --max-retries 5",
                ),
            ),
            context={"day": day_str},
        )

    written = _finalize_day(
        cfg=cfg,
        day=day,
        temp=temp,
        expected_rows=fetched.state_count,
        workspace=workspace,
    )

    zero_history = sorted(set(entity_ids) - fetched.entities_with_history)
    manifest.state_object_count = fetched.state_count
    manifest.entity_count_with_history = len(fetched.entities_with_history)
    manifest.entity_count_zero_history = len(zero_history)
    manifest.zero_history_entities = zero_history
    manifest.output_files = {fmt.value: written.get(fmt) for fmt in FORMAT_ORDER}

    if zero_history:
        logger.debug(
            "  %d entities had no history "
            "(excluded from recorder, no changes, or retention exceeded).",
            len(zero_history),
        )

    logger.info("  Written: %s", ", ".join(written.values()))


@dataclass
class _FetchedDay:
    """What one day's history retrieval produced."""

    state_count: int = 0
    entities_with_history: set = field(default_factory=set)
    failed_batches: list = field(default_factory=list)
    retried: int = 0


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
                )
                result.retried += client.total_retries - retries_before
                manifest.request_count += 1

            except AuthError:
                manifest.failed_request_count += 1
                raise
            except Exception as exc:
                logger.error("  Batch %d failed: %s", batch_idx, exc)
                result.failed_batches.append(
                    {"batch_index": batch_idx, "entities": batch, "error": str(exc)}
                )
                manifest.failed_request_count += 1
                # Continue with remaining batches - day will be marked failed at end.
                if batch_idx < total_batches:
                    sleep(cfg.requests.sleep_between_requests)
                continue

            rows = writers.flatten_payload(payload)

            # local_offset is computed per row from last_changed, so a DST
            # transition day carries the correct offset for each timestamp.
            for row in rows:
                enriched = {
                    **row,
                    "local_offset": local_offset_str(row.get("last_changed"), tz),
                }
                jw.write(enriched)
                if cw is not None:
                    cw.write(enriched)
                eid = row.get("entity_id")
                if eid:
                    result.entities_with_history.add(eid)

            result.state_count += len(rows)

            if batch_idx < total_batches:
                sleep(cfg.requests.sleep_between_requests)

    return result


def _finalize_day(
    cfg: Config,
    day: date,
    temp: dict,
    expected_rows: int,
    workspace: Workspace,
) -> dict:
    """Validate, derive, and publish; return the file name per written format.

    Parquet is derived from the already validated JSONL, so it can only ever
    contain data that passed its own check first. Nothing reaches a final path
    before every local validation has passed.
    """
    validate_jsonl(temp[Format.JSONL], expected_rows)
    if cfg.wants(Format.CSV):
        validate_csv(temp[Format.CSV], expected_rows)

    if cfg.wants(Format.PARQUET):
        logger.info("  Converting JSONL -> Parquet ...")
        writers.convert_jsonl_to_parquet(
            src=temp[Format.JSONL],
            dst=temp[Format.PARQUET],
            row_group_size=PARQUET_ROW_GROUP_SIZE,
            compression=PARQUET_COMPRESSION,
        )
        validate_parquet(temp[Format.PARQUET], expected_rows)

    written: dict = {}
    for fmt in FORMAT_ORDER:
        if not cfg.wants(fmt):
            temp[fmt].unlink(missing_ok=True)
            continue
        final = cfg.layout.day_file(day, fmt.value)
        workspace.promote(
            temp[fmt],
            final,
            cfg.storage.locked_file_retries,
            cfg.storage.locked_file_retry_sleep,
        )
        written[fmt] = final.name
    return written


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
        logger.warning("Could not append to export_runs.jsonl: %s", exc)
