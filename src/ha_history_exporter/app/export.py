"""Exporting: planning the requested days and capturing them under one session."""

from __future__ import annotations

import logging
import os
import socket
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..archive.layout import ArchiveLayout
from ..archive.metadata import append_run_log
from ..archive.session import ArchiveSession
from ..archive.store import load_manifest
from ..archive.workspace import new_run_id
from ..domain.clock import Clock, SystemClock
from ..domain.manifest import Manifest14, ManifestStatus
from ..domain.plan import DayAction, ExportPlan, build_plan
from ..domain.window import DayIdentity
from ..errors import ConfigError, Remedy
from ..formats import missing_requirements
from ..settings import Config
from ..time_utils import local_day_bounds, partial_day_bounds
from .capture import CaptureContext, HistoryClient, capture_day, run_log_entry

logger = logging.getLogger(__name__)


def plan_export(
    cfg: Config,
    *,
    requested_start: date,
    requested_end: date,
    today: date,
    tz: ZoneInfo,
    force: bool = False,
    partial_day: date | None = None,
) -> ExportPlan:
    """Decide every requested day from its manifest, the calendar and the retention."""
    layout = ArchiveLayout(Path(cfg.export.output_dir))

    def completed(day: date) -> str | None:
        start, end = local_day_bounds(day, tz)
        loaded = load_manifest(layout, DayIdentity.of(day, tz, start, end))
        if loaded is None or loaded.status is not ManifestStatus.OK:
            return None
        return f"manifest.json status=ok, {loaded.state_object_count} state objects"

    plan = build_plan(
        requested_start,
        requested_end,
        today=today,
        completed=completed,
        force=force,
        partial_day=partial_day,
        purge_keep_days=cfg.recorder.purge_keep_days,
    )
    for decision in plan.decisions:
        if decision.action is DayAction.SKIP_EXISTING:
            logger.info("Day %s: skipping - %s", decision.day, decision.reason)
    blocked = plan.days(DayAction.BLOCKED_RETENTION)
    if blocked:
        raise ConfigError(
            "Recorder retention blocks one or more requested days.",
            details=(
                "These days touch or exceed the configured retention boundary: "
                f"{', '.join(str(day) for day in blocked)}. HHE will not publish a "
                "potentially incomplete capture."
            ),
            remedies=(Remedy("Choose complete days wholly inside the Recorder retention window."),),
        )
    if cfg.recorder.purge_keep_days is None and plan.days_to_export:
        logger.warning(
            "Recorder retention is not configured. A non-empty validated response "
            "may be exported, but an empty response cannot be verified and will fail."
        )
    logger.info(
        "Export plan: %d day(s) to export (%d partial), %d skip (existing), %d skip (incomplete).",
        len(plan.days_to_export),
        len(plan.partial_days),
        len(plan.days(DayAction.SKIP_EXISTING)),
        len(plan.days(DayAction.SKIP_INCOMPLETE)),
    )
    return plan


def _missing_pyarrow() -> ConfigError:
    return ConfigError(
        "Parquet output is enabled but pyarrow is not installed.",
        details=(
            "Parquet needs pyarrow, a large platform-specific package that HHE "
            "keeps optional. Nothing was requested from Home Assistant."
        ),
        remedies=(
            Remedy(
                "Add it to an existing pipx installation:",
                "pipx inject ha-history-exporter pyarrow",
            ),
            Remedy("Or install HHE with the extra:", 'pip install "ha-history-exporter[parquet]"'),
            Remedy("Or turn Parquet off:", "hhe config set export.formats jsonl"),
        ),
    )


def run_export(
    cfg: Config,
    plan: ExportPlan,
    entity_ids: Sequence[str],
    entity_count_total: int,
    client: HistoryClient,
    tz: ZoneInfo,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> int:
    """Capture every day the plan exports; 0 when all succeeded, else 1.

    *now* ends the window of a partial day.
    """
    if cfg.snapshot_only:
        logger.info(
            "Snapshot-only configuration: no history formats are enabled, "
            "so no daily export or manifest will be created."
        )
        return 0
    if missing_requirements(cfg.export.formats):
        raise _missing_pyarrow()
    days = plan.days_to_export
    if not days:
        logger.info("Nothing to export.")
        return 0

    clock = clock or SystemClock()
    started = clock.now()
    run_id = new_run_id(started.astimezone(UTC))
    layout = ArchiveLayout(Path(cfg.export.output_dir))
    holder = (
        f"run_id: {run_id}\npid: {os.getpid()}\nhost: {socket.gethostname()}\n"
        f"started_at: {started.astimezone(UTC).isoformat()}\n"
    )
    partial_days = frozenset(plan.partial_days)
    any_failed = False
    with ArchiveSession(
        layout,
        cfg.resolved_temp_dir,
        run_id,
        holder=holder,
        retries=cfg.storage.locked_file_retries,
        retry_sleep=cfg.storage.locked_file_retry_sleep,
    ) as session:
        logger.debug("Run %s started.", run_id)
        ctx = CaptureContext(
            cfg=cfg,
            layout=layout,
            workspace=session.workspace,
            client=client,
            tz=tz,
            entity_ids=tuple(entity_ids),
            entity_count_total=entity_count_total,
            sleep=sleep,
            clock=clock,
        )
        for index, day in enumerate(days):
            partial = day in partial_days
            start, end = partial_day_bounds(day, tz, now) if partial else local_day_bounds(day, tz)
            logger.info(
                "== Day %d/%d: %s  [%s -> %s] ==",
                index + 1,
                len(days),
                day,
                start.replace(microsecond=0).isoformat(),
                end.replace(microsecond=0).isoformat(),
            )
            outcome = capture_day(ctx, day, start=start, end=end, partial=partial)
            any_failed |= outcome.manifest.status is ManifestStatus.FAILED
            _log_run(layout, outcome.manifest)
            if outcome.fatal is not None:
                raise outcome.fatal
            if index < len(days) - 1:
                logger.debug("Sleeping %.1f s between days ...", cfg.requests.sleep_between_days)
                sleep(cfg.requests.sleep_between_days)
    return 1 if any_failed else 0


def _log_run(layout: ArchiveLayout, manifest: Manifest14) -> None:
    try:
        append_run_log(layout, run_log_entry(manifest))
    except OSError as exc:
        logger.warning("Could not append to export_runs.jsonl (%s).", type(exc).__name__)
