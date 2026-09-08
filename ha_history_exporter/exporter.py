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

import json
import logging
import time
from datetime import date, datetime
from typing import Callable, List
from zoneinfo import ZoneInfo

from . import manifest as mf
from . import writers
from .errors import AuthError, ConfigError, Remedy
from .ha_client import HomeAssistantClient
from .planner import ExportPlan
from .runtime.workspace import Workspace, open_workspace
from .settings import Config, Format
from .time_utils import format_iso, local_day_bounds, local_offset_str, to_utc
from .validators import validate_csv, validate_jsonl, validate_parquet

logger = logging.getLogger(__name__)


def run_export(
    cfg: Config,
    plan: ExportPlan,
    entity_ids: List[str],
    entity_count_total: int,
    client: HomeAssistantClient,
    tz: ZoneInfo,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
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
                        "hhe config set formats.parquet false",
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
            entity_ids=entity_ids,
            entity_count_total=entity_count_total,
            client=client,
            tz=tz,
            workspace=workspace,
            sleep=sleep,
        )
    finally:
        workspace.close()


def _run_days(
    cfg: Config,
    days: List[date],
    entity_ids: List[str],
    entity_count_total: int,
    client: HomeAssistantClient,
    tz: ZoneInfo,
    workspace: Workspace,
    sleep: Callable[[float], None],
) -> int:
    any_error = False

    for day_idx, day in enumerate(days):
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
            m.mark_finished(tz, status="ok")
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
    """Stream history for all entities on one day into JSONL (and CSV)."""
    work_dir = workspace.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)

    tmp_jsonl   = work_dir / f"{day_str}.jsonl"
    tmp_csv     = work_dir / f"{day_str}.csv"
    tmp_parquet = work_dir / f"{day_str}.parquet"

    final_jsonl   = cfg.layout.day_file(day, "jsonl")
    final_csv     = cfg.layout.day_file(day, "csv")
    final_parquet = cfg.layout.day_file(day, "parquet")

    # A retried day within the same run must not see the previous attempt.
    tmp_jsonl.unlink(missing_ok=True)
    tmp_csv.unlink(missing_ok=True)
    tmp_parquet.unlink(missing_ok=True)

    batches = _chunks(entity_ids, cfg.requests.batch_size)
    batch_list = list(batches)
    total_batches = len(batch_list)

    state_count = 0
    entities_with_history: set[str] = set()
    failed_batches: list[dict] = []
    retried_this_day = 0

    logger.info(
        "  %d entities -> %d batches of up to %d",
        len(entity_ids),
        total_batches,
        cfg.requests.batch_size,
    )

    # Stream into temp files — never hold the whole day in RAM.
    with writers.JsonlWriter(tmp_jsonl) as jw, \
         (writers.CsvWriter(tmp_csv) if cfg.wants(Format.CSV) else _NullWriter()) as cw:

        for batch_idx, batch in enumerate(batch_list, start=1):
            logger.debug(
                "  Batch %d/%d (%d entities) ...", batch_idx, total_batches, len(batch)
            )

            payload = None
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
                retried_this_day += client.total_retries - retries_before
                manifest.request_count += 1

            except AuthError:
                manifest.failed_request_count += 1
                raise
            except Exception as exc:
                logger.error("  Batch %d failed: %s", batch_idx, exc)
                failed_batches.append(
                    {"batch_index": batch_idx, "entities": batch, "error": str(exc)}
                )
                manifest.failed_request_count += 1
                # Continue with remaining batches — day will be marked failed at end.
                if batch_idx < total_batches:
                    sleep(cfg.requests.sleep_between_requests)
                continue

            # Flatten [[state,...],[state,...]] → [state, state, ...]
            rows = writers.flatten_payload(payload)

            # Write each row immediately — no accumulation.
            # Enrich with local_offset (e.g. '+02:00') so consumers know
            # how to convert the UTC timestamps to local time without
            # consulting the manifest. Computed per row so DST transition
            # days return the correct offset for each individual timestamp.
            for row in rows:
                enriched = {**row, "local_offset": local_offset_str(row.get("last_changed"), tz)}
                jw.write(enriched)
                cw.write(enriched)
                eid = row.get("entity_id")
                if eid:
                    entities_with_history.add(eid)

            state_count += len(rows)

            if batch_idx < total_batches:
                sleep(cfg.requests.sleep_between_requests)

    # Update client retries counter in manifest.
    manifest.retried_request_count = retried_this_day

    if failed_batches:
        manifest.failed_batches = failed_batches
        raise RuntimeError(
            f"{len(failed_batches)} batch(es) failed - see manifest for details."
        )

    # Validate JSONL and CSV before any finalisation.
    validate_jsonl(tmp_jsonl, expected_rows=state_count)
    if cfg.wants(Format.CSV):
        validate_csv(tmp_csv, expected_rows=state_count)

    # Parquet post-processing: convert the already-validated JSONL temp file
    # to Parquet.  Done AFTER JSONL validation so Parquet is derived from
    # confirmed-good data.  Runs locally (temp dir) before touching cloud-storage.
    if cfg.wants(Format.PARQUET):
        logger.info("  Converting JSONL -> Parquet ...")
        writers.convert_jsonl_to_parquet(
            src=tmp_jsonl,
            dst=tmp_parquet,
            row_group_size=200_000,
            compression="snappy",
        )
        validate_parquet(tmp_parquet, expected_rows=state_count)

    # Finalize only the configured output formats after all local validations
    # have passed. JSONL remains an internal streaming/intermediate format when
    # disabled as a final artifact.
    written_files: list[str] = []
    if cfg.wants(Format.JSONL):
        workspace.promote(
            tmp_jsonl, final_jsonl,
            cfg.storage.locked_file_retries,
            cfg.storage.locked_file_retry_sleep,
        )
        written_files.append(final_jsonl.name)
    if cfg.wants(Format.CSV):
        workspace.promote(
            tmp_csv, final_csv,
            cfg.storage.locked_file_retries,
            cfg.storage.locked_file_retry_sleep,
        )
        written_files.append(final_csv.name)
    else:
        tmp_csv.unlink(missing_ok=True)

    if cfg.wants(Format.PARQUET):
        workspace.promote(
            tmp_parquet, final_parquet,
            cfg.storage.locked_file_retries,
            cfg.storage.locked_file_retry_sleep,
        )
        written_files.append(final_parquet.name)
    else:
        tmp_parquet.unlink(missing_ok=True)

    if not cfg.wants(Format.JSONL):
        tmp_jsonl.unlink(missing_ok=True)

    # Populate manifest counters.
    zero_history = sorted(set(entity_ids) - entities_with_history)
    manifest.state_object_count = state_count
    manifest.entity_count_with_history = len(entities_with_history)
    manifest.entity_count_zero_history = len(zero_history)
    manifest.zero_history_entities = zero_history
    manifest.output_files = {
        "jsonl":   final_jsonl.name   if cfg.wants(Format.JSONL)   else None,
        "csv":     final_csv.name     if cfg.wants(Format.CSV)     else None,
        "parquet": final_parquet.name if cfg.wants(Format.PARQUET) else None,
    }

    if zero_history:
        logger.debug(
            "  %d entities had no history "
            "(excluded from recorder, no changes, or retention exceeded).",
            len(zero_history),
        )

    logger.info("  Written: %s", ", ".join(written_files))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


class _NullWriter:
    """Drop-in replacement for CsvWriter when CSV output is disabled."""
    def write(self, row: dict) -> None: pass
    def __enter__(self): return self
    def __exit__(self, *args): pass


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
