"""The export run: from a day selection to published days.

1. The run starts: the clock is read once (:class:`RunContext`).
2. Without asking Home Assistant, the archive's zone is known when the
   archive has a descriptor or days, or ``export.timezone`` is set; if nothing
   is to be exported then, the run ends here.
3. Home Assistant is asked for its API, its configuration and the current
   states; the archive's zone follows (:func:`descriptor.determine`).
4. A dry run plans and reports; it writes nothing.
5. Otherwise the archive session opens (lock, recovery, descriptor), the log
   file of the run starts, the entity snapshot is written, the plan is made
   under the lock, and every day is captured and published.

A rejected token, an unavailable Home Assistant, a TLS failure or a defect
ends the run after the running day is recorded as failed; any other failure
fails its day only. See internal dev doc § "Export run".
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from ..archive import descriptor
from ..archive.descriptor import ArchiveZone
from ..archive.layout import ArchiveLayout
from ..archive.metadata import append_run_log, write_snapshot
from ..archive.session import ArchiveSession
from ..archive.store import load_manifest
from ..domain.clock import Clock, SystemClock
from ..domain.manifest import Manifest14, ManifestStatus
from ..domain.plan import DayAction, DaySelection, ExportPlan, build_plan
from ..domain.snapshot import EntitySelection, select_entities, snapshot_document
from ..domain.window import DayWindow, format_iso
from ..errors import ConfigError, Remedy
from ..formats import missing_requirements
from ..settings import Config
from ..source.protocol import HistorySource, StateObject
from ..source.transport import plaintext_warning
from .capture import CaptureContext, capture_day, run_log_entry
from .context import RunContext
from .results import DayResult, EntitySummary, ExportResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExportRequest:
    selection: DaySelection = field(default_factory=DaySelection)
    force: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class ExportHooks:
    """Where the command line follows a run."""

    #: Called once the archive is locked: the run's log file from now on.
    attach_log: Callable[[Path], None] = lambda _path: None
    #: Called with the plan before the first day is exported.
    planned: Callable[[ExportPlan, EntitySummary, ArchiveZone], None] = (
        lambda _plan, _entities, _zone: None
    )


def run_export(
    request: ExportRequest,
    cfg: Config,
    connect: Callable[[], HistorySource],
    *,
    clock: Clock | None = None,
    sleep: Callable[[float], None] = time.sleep,
    hooks: ExportHooks | None = None,
) -> ExportResult:
    clock = clock or SystemClock()
    hooks = hooks or ExportHooks()
    run = RunContext.start(clock)
    layout = ArchiveLayout(cfg.export.output_dir)
    if not cfg.snapshot_only and missing_requirements(cfg.export.formats):
        raise _missing_pyarrow()
    recorded = descriptor.load(layout)

    if not request.dry_run and not cfg.snapshot_only:
        known = descriptor.known(layout, recorded, configured=cfg.export.timezone)
        if known is not None:
            preview = plan_export(
                cfg, layout, request, zone=known.zone, today=run.today(known.zone)
            )
            if not preview.days_to_export:
                _nothing_to_export(preview)
                return ExportResult(known, preview)

    warning = plaintext_warning(cfg.homeassistant.url) if cfg.homeassistant.url else None
    if warning is not None:
        logger.warning("%s", warning)
    with contextlib.closing(connect()) as source:
        logger.info("Checking the Home Assistant API ...")
        source.check_api()
        instance = source.instance_config()
        zone = descriptor.determine(
            layout, recorded, configured=cfg.export.timezone, home_assistant=instance.time_zone
        )
        logger.info("Fetching entity states from /api/states ...")
        states = source.states()
        entities = _entities(cfg, states)
        if request.dry_run:
            plan = None
            if not cfg.snapshot_only:
                plan = plan_export(cfg, layout, request, zone=zone.zone, today=run.today(zone.zone))
            if plan is not None:
                hooks.planned(plan, entities, zone)
            logger.info("Dry run complete; nothing was fetched or written.")
            return ExportResult(zone, plan, entities, dry_run=True)

        with ArchiveSession(
            layout,
            cfg.storage.temp_dir,
            run.run_id,
            holder=run.lock_holder(),
            configured_zone=cfg.export.timezone,
            home_assistant_zone=instance.time_zone,
            now=run.started_at,
            retries=cfg.storage.locked_file_retries,
            retry_sleep=cfg.storage.locked_file_retry_sleep,
        ) as session:
            archive_zone = session.descriptor.timezone
            local_start = run.local(archive_zone)
            log_file = layout.log_file(local_start)
            hooks.attach_log(log_file)
            logger.info("Run %s writes to %s.", run.run_id, layout.root)
            snapshot = snapshot_document(states, local_start)
            snapshot_file = write_snapshot(layout, snapshot, local_start, run.run_id)
            logger.info(
                "Entity snapshot saved: %s (%d entities).",
                snapshot_file.name,
                snapshot["entity_count"],
            )
            if cfg.snapshot_only:
                logger.info("Snapshot only: no history is requested and no day is written.")
                return ExportResult(
                    zone, None, entities, snapshot_file=snapshot_file, log_file=log_file
                )
            plan = plan_export(
                cfg, layout, request, zone=archive_zone, today=run.today(archive_zone)
            )
            hooks.planned(plan, entities, zone)
            context = CaptureContext(
                cfg=cfg,
                layout=layout,
                workspace=session.workspace,
                source=source,
                tz=archive_zone,
                entity_ids=entities.requested,
                entity_count_total=entities.candidates,
                sleep=sleep,
                clock=clock,
            )
            days = _export_days(context, plan, run, layout, sleep)
            return ExportResult(
                zone, plan, entities, days, snapshot_file=snapshot_file, log_file=log_file
            )


def plan_export(
    cfg: Config, layout: ArchiveLayout, request: ExportRequest, *, zone: ZoneInfo, today: date
) -> ExportPlan:
    """Decide every requested day from its manifest, the calendar and the retention.

    A day the known Recorder retention may already have purged stops the plan.
    """
    requested = request.selection.resolve(today)

    def completed(day: date) -> str | None:
        loaded = load_manifest(layout, DayWindow.full(day, zone).identity())
        if loaded is None or loaded.status is not ManifestStatus.OK:
            return None
        return f"manifest.json status=ok, {loaded.state_object_count} state objects"

    plan = build_plan(
        requested.start,
        requested.end,
        today=today,
        completed=completed,
        force=request.force,
        partial_day=requested.partial_day,
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


def _nothing_to_export(plan: ExportPlan) -> None:
    logger.info(
        "Nothing to export - %d day(s) already exported, %d day(s) not yet complete.",
        len(plan.days(DayAction.SKIP_EXISTING)),
        len(plan.days(DayAction.SKIP_INCOMPLETE)),
    )


def _entities(cfg: Config, states: Sequence[StateObject]) -> EntitySummary:
    selection = cfg.entities
    selected = select_entities(
        states,
        EntitySelection(
            include_unknown=selection.include_unknown,
            include_unavailable=selection.include_unavailable,
            exclude_domains=selection.exclude_domains,
            exclude_patterns=selection.exclude_patterns,
        ),
    )
    if selected.excluded_count:
        logger.info(
            "Excludes removed %d entities (%d remain).",
            selected.excluded_count,
            len(selected.entity_ids),
        )
    return EntitySummary(
        candidates=selected.candidate_count,
        requested=selected.entity_ids,
        excluded=selected.excluded_count,
        unknown=sum(1 for state in states if state.get("state") == "unknown"),
        unavailable=sum(1 for state in states if state.get("state") == "unavailable"),
    )


def _export_days(
    ctx: CaptureContext,
    plan: ExportPlan,
    run: RunContext,
    layout: ArchiveLayout,
    sleep: Callable[[float], None],
) -> tuple[DayResult, ...]:
    days = plan.days_to_export
    partial = frozenset(plan.partial_days)
    if partial:
        logger.warning(
            "Today (%s) is not over yet. Exporting it captures the changes recorded so "
            "far; the day is recorded as status=partial and exported in full later.",
            ", ".join(str(day) for day in sorted(partial)),
        )
    if days:
        logger.info(
            "Starting export: %d day(s), %d entities, batch size %d.",
            len(days),
            len(ctx.entity_ids),
            ctx.cfg.requests.batch_size,
        )
    results = []
    for index, day in enumerate(days):
        window = (
            DayWindow.elapsed(day, ctx.tz, run.started_at)
            if day in partial
            else DayWindow.full(day, ctx.tz)
        )
        logger.info(
            "== Day %d/%d: %s  [%s -> %s) ==",
            index + 1,
            len(days),
            day,
            format_iso(window.start),
            format_iso(window.end),
        )
        outcome = capture_day(ctx, window)
        _log_run(layout, outcome.manifest)
        results.append(
            DayResult(
                day,
                outcome.manifest.status,
                outcome.manifest.state_object_count,
                outcome.manifest.error,
            )
        )
        if outcome.fatal is not None:
            raise outcome.fatal
        if outcome.manifest.status is ManifestStatus.PARTIAL:
            logger.warning(
                "Day %s holds an incomplete day and is marked status=partial; the next "
                "run that includes it replaces it with the complete day.",
                day,
            )
        if index < len(days) - 1:
            sleep(ctx.cfg.requests.sleep_between_days)
    return tuple(results)


def _log_run(layout: ArchiveLayout, manifest: Manifest14) -> None:
    try:
        append_run_log(layout, run_log_entry(manifest))
    except OSError as exc:
        logger.warning("Could not append to export_runs.jsonl (%s).", type(exc).__name__)


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
