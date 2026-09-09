"""Export plan builder.

Decides, for every requested day, whether to export it, skip it, or export
it as a partial day:
  - a day that is not yet complete is skipped, unless it was asked for
    explicitly as the partial day
  - a day whose manifest says ``ok`` is skipped, unless --force is given
  - everything else is exported
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import List
from zoneinfo import ZoneInfo

from .errors import ExportError, Remedy
from .time_utils import (
    is_day_complete,
    iter_days,
    latest_complete_day,
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
    ) -> None:
        """Print a human-readable dry-run or pre-run summary."""
        import math

        batches_per_day = math.ceil(entity_count / batch_size) if entity_count else 0
        print(f"\n{'=' * 60}")
        print("  Home Assistant REST History Exporter")
        print(f"{'=' * 60}")
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

        if not force:
            existing = _check_existing(day, cfg)
            if existing:
                plan.decisions.append(
                    DayDecision(day=day, action=SKIP_EXISTING, reason=existing)
                )
                logger.info("Day %s: skipping - %s", day, existing)
                continue

        plan.decisions.append(DayDecision(day=day, action=EXPORT))

    logger.info(
        "Export plan: %d day(s) to export (%d partial), %d skip (existing), "
        "%d skip (incomplete).",
        len(plan.days_to_export),
        len(plan.partial_days),
        len(plan.days_skipped_existing),
        len(plan.days_skipped_incomplete),
    )
    return plan


def _check_existing(day: date, cfg) -> str | None:
    """Return a reason if this day has a successful manifest, else ``None``.

    The manifest is the durable source of truth for resume decisions. Export
    artifacts may have been archived or moved after a successful run.

    Three distinct answers, never collapsed into one: the manifest is absent
    (export the day), unreadable (stop — treating it as absent would overwrite
    a finished day), or malformed (warn and export). See internal dev doc,
    Wiederanlauf.
    """
    manifest_path = cfg.layout.day_file(day, "manifest.json")

    try:
        with manifest_path.open("r", encoding="utf-8") as f:
            m = json.load(f)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except json.JSONDecodeError as exc:
        logger.warning(
            "Day %s: manifest %s is not valid JSON (%s); exporting the day again.",
            day,
            manifest_path,
            exc,
        )
        return None
    except OSError as exc:
        raise ExportError(
            f"Cannot read the manifest for {day}: {exc.strerror or exc}",
            details=(
                "The manifest records whether this day was already captured. "
                "Without it HHE cannot tell a finished day from a missing one, "
                "and exporting again would overwrite the existing files."
            ),
            remedies=(
                Remedy(
                    "Check the permissions on the export directory, then run again:",
                    "hhe doctor",
                ),
            ),
            context={"day": str(day), "output_dir": str(manifest_path)},
        ) from exc

    if not isinstance(m, dict):
        logger.warning(
            "Day %s: manifest %s does not contain an object; exporting the day again.",
            day,
            manifest_path,
        )
        return None
    if m.get("status") != "ok":
        return None
    return f"manifest.json status=ok, {m.get('state_object_count', '?')} state objects"
