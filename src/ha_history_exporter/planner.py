"""Export plan builder.

Decides, for every requested day, whether to export it, skip it, or export
it as a partial day:
  - a day that is not yet complete is skipped, unless it was asked for
    explicitly as the partial day
  - a day whose manifest says ``ok`` is skipped, unless --force is given
  - everything else is exported
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List
from zoneinfo import ZoneInfo

from . import manifest as mf
from .errors import ConfigError, Remedy
from .time_utils import (
    format_iso,
    is_day_complete,
    iter_days,
    latest_complete_day,
    local_day_bounds,
    to_utc,
    today_local,
)

logger = logging.getLogger(__name__)


#: Actions a day can be planned for.
EXPORT = "export"
EXPORT_PARTIAL = "export_partial"
SKIP_EXISTING = "skip_existing"
SKIP_INCOMPLETE = "skip_incomplete"


@dataclass
class DayDecision:
    day: date
    action: str
    reason: str | None = None


@dataclass
class ExportPlan:
    requested_start: date
    requested_end: date
    today: date
    latest_complete: date
    decisions: List[DayDecision] = field(default_factory=list)

    @property
    def days_to_export(self) -> List[date]:
        """Every day that will be fetched, complete or partial."""
        return [
            d.day
            for d in self.decisions
            if d.action in (EXPORT, EXPORT_PARTIAL)
        ]

    @property
    def partial_days(self) -> List[date]:
        """Days fetched only up to now, and therefore not finished."""
        return [d.day for d in self.decisions if d.action == EXPORT_PARTIAL]

    @property
    def days_skipped_existing(self) -> List[date]:
        return [d.day for d in self.decisions if d.action == SKIP_EXISTING]

    @property
    def days_skipped_incomplete(self) -> List[date]:
        return [d.day for d in self.decisions if d.action == SKIP_INCOMPLETE]

    def print_summary(
        self,
        entity_count: int,
        unknown_count: int,
        unavailable_count: int,
        batch_size: int,
        output_dir: str | None = None,
        config_source: str | None = None,
        log_file: str | None = None,
    ) -> None:
        """Print a human-readable dry-run or pre-run summary.

        Where the run writes and what configured it come first: a run pointed
        at the wrong directory is recognised by those two lines alone, and this
        summary appears before anything is fetched.
        """
        import math

        batches_per_day = math.ceil(entity_count / batch_size) if entity_count else 0
        print(f"\n{'=' * 60}")
        print("  Home Assistant REST History Exporter")
        print(f"{'=' * 60}")
        for label, value in (
            ("Output", output_dir),
            ("Configuration", config_source),
            ("Log file", log_file),
        ):
            if value is not None:
                print(f"  {label:<16}: {value}")
        if output_dir is not None:
            print()
        print(f"  Requested range : {self.requested_start} -> {self.requested_end}")
        print(f"  Current local   : {self.today}")
        print(f"  Latest complete : {self.latest_complete}")
        print()
        print(f"  Entities (from /api/states): {entity_count}")
        print(f"    unknown     : {unknown_count}")
        print(f"    unavailable : {unavailable_count}")
        print(f"  Batch size      : {batch_size}")
        print(f"  Requests / day  : ~{batches_per_day}")
        print()
        partial = set(self.partial_days)
        if self.days_to_export:
            print(f"  Will export ({len(self.days_to_export)}):")
            for d in self.days_to_export:
                marker = "  (partial - today is not over yet)" if d in partial else ""
                print(f"    {d}{marker}")
        else:
            print("  Nothing to export.")
        if self.days_skipped_existing:
            print(f"\n  Will skip - already exported ({len(self.days_skipped_existing)}):")
            for d in self.days_skipped_existing:
                print(f"    {d}")
        if self.days_skipped_incomplete:
            print(f"\n  Will skip - not yet complete ({len(self.days_skipped_incomplete)}):")
            for d in self.days_skipped_incomplete:
                print(f"    {d}")
        print(f"{'=' * 60}\n")


def build_plan(
    requested_start: date,
    requested_end: date,
    tz: ZoneInfo,
    cfg,  # Config
    force: bool = False,
    partial_day: date | None = None,
    now: date | None = None,
) -> ExportPlan:
    """Build an export plan for the given date range.

    Args:
        requested_start: First day requested by the user.
        requested_end:   Last day requested by the user (inclusive).
        tz:              Local timezone (e.g. Europe/Berlin).
        cfg:             Config with output paths.
        force:           If True, re-export even days whose manifest says ok.
        partial_day:     The still running day the caller asked for by name.
                         Any other incomplete day is skipped as before.
    """
    today = now or today_local(tz)
    latest = today - timedelta(days=1) if now is not None else latest_complete_day(tz)

    plan = ExportPlan(
        requested_start=requested_start,
        requested_end=requested_end,
        today=today,
        latest_complete=latest,
    )

    retention_blocked: list[date] = []
    for day in iter_days(requested_start, requested_end):
        complete = day < today if now is not None else is_day_complete(day, tz)
        if not complete:
            if day == partial_day:
                plan.decisions.append(
                    DayDecision(
                        day=day,
                        action=EXPORT_PARTIAL,
                        reason=(
                            f"Day {day} was requested by name and is exported "
                            "up to now."
                        ),
                    )
                )
                continue
            plan.decisions.append(
                DayDecision(
                    day=day,
                    action=SKIP_INCOMPLETE,
                    reason=f"Day {day} is not yet complete (today is {today}).",
                )
            )
            continue

        existing = _check_existing(day, cfg, tz)
        if existing and not force:
            plan.decisions.append(
                DayDecision(day=day, action=SKIP_EXISTING, reason=existing)
            )
            logger.info("Day %s: skipping - %s", day, existing)
            continue

        keep_days = cfg.recorder.purge_keep_days
        if keep_days is not None and (today - day).days >= keep_days:
            retention_blocked.append(day)
            continue

        plan.decisions.append(DayDecision(day=day, action=EXPORT))

    if retention_blocked:
        dates = ", ".join(str(day) for day in retention_blocked)
        raise ConfigError(
            "Recorder retention blocks one or more requested days.",
            details=(
                f"These days touch or exceed the configured retention boundary: "
                f"{dates}. HHE will not publish a potentially incomplete capture."
            ),
            remedies=(
                Remedy(
                    "Choose complete days wholly inside the Recorder retention window."
                ),
            ),
        )

    if cfg.recorder.purge_keep_days is None and plan.days_to_export:
        logger.warning(
            "Recorder retention is not configured. A non-empty validated response "
            "may be exported, but an empty response cannot be verified and will fail."
        )

    logger.info(
        "Export plan: %d day(s) to export (%d partial), %d skip (existing), "
        "%d skip (incomplete).",
        len(plan.days_to_export),
        len(plan.partial_days),
        len(plan.days_skipped_existing),
        len(plan.days_skipped_incomplete),
    )
    return plan


def _check_existing(
    day: date, cfg, tz: ZoneInfo | None = None
) -> str | None:
    """Return a reason if this day has a successful manifest, else ``None``.

    The manifest is the durable source of truth for resume decisions. Export
    artifacts may have been archived or moved after a successful run.

    A missing manifest means export. Every other manifest is parsed through the
    versioned fail-closed contract before either skip or force is considered.
    """
    timezone = tz or ZoneInfo(cfg.export.timezone)
    start, end = local_day_bounds(day, timezone)
    loaded = mf.load_existing(
        day=day,
        tz_name=cfg.export.timezone,
        start_local=format_iso(start),
        end_local=format_iso(end),
        start_utc=format_iso(to_utc(start)),
        end_utc=format_iso(to_utc(end)),
        cfg=cfg,
    )
    if loaded is None or loaded.status != "ok":
        return None
    return f"manifest.json status=ok, {loaded.state_object_count} state objects"
