"""Export plan builder.

Determines which days in a requested range need to be exported, based on:
  - whether the day is already complete in local time
  - whether a valid export already exists (resume mode)
  - whether --force overrides existing exports
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

from .time_utils import (
    format_iso,
    is_day_complete,
    iter_days,
    latest_complete_day,
    local_day_bounds,
    today_local,
)

logger = logging.getLogger(__name__)


@dataclass
class DayDecision:
    day: date
    action: str            # "export" | "skip_existing" | "skip_incomplete"
    reason: Optional[str] = None


@dataclass
class ExportPlan:
    requested_start: date
    requested_end: date
    today: date
    latest_complete: date
    decisions: List[DayDecision] = field(default_factory=list)

    @property
    def days_to_export(self) -> List[date]:
        return [d.day for d in self.decisions if d.action == "export"]

    @property
    def days_skipped_existing(self) -> List[date]:
        return [d.day for d in self.decisions if d.action == "skip_existing"]

    @property
    def days_skipped_incomplete(self) -> List[date]:
        return [d.day for d in self.decisions if d.action == "skip_incomplete"]

    def print_summary(
        self,
        entity_count: int,
        unknown_count: int,
        unavailable_count: int,
        batch_size: int,
    ) -> None:
        """Print a human-readable dry-run or pre-run summary."""
        import math

        batches_per_day = math.ceil(entity_count / batch_size) if entity_count else 0
        print(f"\n{'=' * 60}")
        print(f"  Home Assistant REST History Exporter")
        print(f"{'=' * 60}")
        print(f"  Requested range : {self.requested_start} → {self.requested_end}")
        print(f"  Current local   : {self.today}")
        print(f"  Latest complete : {self.latest_complete}")
        print()
        print(f"  Entities (from /api/states): {entity_count}")
        print(f"    unknown     : {unknown_count}")
        print(f"    unavailable : {unavailable_count}")
        print(f"  Batch size      : {batch_size}")
        print(f"  Requests / day  : ~{batches_per_day}")
        print()
        if self.days_to_export:
            print(f"  Will export ({len(self.days_to_export)}):")
            for d in self.days_to_export:
                print(f"    {d}")
        else:
            print("  Nothing to export.")
        if self.days_skipped_existing:
            print(f"\n  Will skip — already exported ({len(self.days_skipped_existing)}):")
            for d in self.days_skipped_existing:
                print(f"    {d}")
        if self.days_skipped_incomplete:
            print(f"\n  Will skip — not yet complete ({len(self.days_skipped_incomplete)}):")
            for d in self.days_skipped_incomplete:
                print(f"    {d}")
        print(f"{'=' * 60}\n")


def build_plan(
    requested_start: date,
    requested_end: date,
    tz: ZoneInfo,
    cfg,  # AppConfig
    force: bool,
    resume: bool,
) -> ExportPlan:
    """Build an export plan for the given date range.

    Args:
        requested_start: First day requested by the user.
        requested_end:   Last day requested by the user (inclusive).
        tz:              Local timezone (e.g. Europe/Berlin).
        cfg:             AppConfig with output paths.
        force:           If True, always re-export even if status=ok.
        resume:          If True, skip days with a valid existing export.
    """
    today = today_local(tz)
    latest = latest_complete_day(tz)

    plan = ExportPlan(
        requested_start=requested_start,
        requested_end=requested_end,
        today=today,
        latest_complete=latest,
    )

    for day in iter_days(requested_start, requested_end):
        if not is_day_complete(day, tz):
            plan.decisions.append(
                DayDecision(
                    day=day,
                    action="skip_incomplete",
                    reason=f"Day {day} is not yet complete (today is {today}).",
                )
            )
            continue

        if resume and not force:
            existing = _check_existing(day, cfg)
            if existing:
                plan.decisions.append(
                    DayDecision(day=day, action="skip_existing", reason=existing)
                )
                logger.info("Day %s: skipping — %s", day, existing)
                continue

        plan.decisions.append(DayDecision(day=day, action="export"))

    logger.info(
        "Export plan: %d days to export, %d skip (existing), %d skip (incomplete).",
        len(plan.days_to_export),
        len(plan.days_skipped_existing),
        len(plan.days_skipped_incomplete),
    )
    return plan


def _check_existing(day: date, cfg) -> Optional[str]:
    """Return a reason string if this day already has a valid export, else None."""
    manifest_path = cfg.day_file(day, "manifest.json")
    jsonl_path = cfg.day_file(day, "jsonl")

    if not manifest_path.exists():
        return None
    if not jsonl_path.exists():
        return None

    try:
        with manifest_path.open("r", encoding="utf-8") as f:
            m = json.load(f)
        if m.get("status") != "ok":
            return None
        return f"manifest.json status=ok, {m.get('state_object_count', '?')} state objects"
    except Exception:
        return None
