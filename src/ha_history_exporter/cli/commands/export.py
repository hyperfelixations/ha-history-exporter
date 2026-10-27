"""The ``export`` command: fetch Home Assistant history for complete days.

This module owns the export flow only. Argument grammar lives in
:mod:`ha_history_exporter.cli.parser`, error rendering and exit codes in
:mod:`ha_history_exporter.cli`.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ... import ha_client
from ...app.export import plan_export, run_export
from ...archive.metadata import write_snapshot
from ...archive.workspace import new_run_id
from ...domain.formats import Format
from ...domain.plan import DayAction, ExportPlan
from ...domain.snapshot import EntitySelection, select_entities, snapshot_document
from ...errors import ConfigError, Remedy, UsageError
from ...settings import Config, resolve, schema
from ...time_utils import (
    last_n_complete_days,
    latest_complete_day,
    parse_date_arg,
    today_local,
)

logger = logging.getLogger(__name__)

#: Command-line options that override a configuration key. Each option carries
#: the name of the key it overrides, so the two can never drift apart.
_VALUE_OVERRIDES = {
    "output_dir": "export.output_dir",
    "timezone": "export.timezone",
    "format": "export.formats",
    "batch_size": "requests.batch_size",
    "sleep_between_requests": "requests.sleep_between_requests",
    "sleep_between_days": "requests.sleep_between_days",
    "timeout": "requests.timeout",
    "max_retries": "requests.max_retries",
}


def run(args: argparse.Namespace) -> int:
    """Execute one export run and return the process exit code."""
    settings = resolve(
        explicit_config=args.config,
        cli_overrides=cli_overrides(args),
    )
    cfg = settings.config
    config_source = (
        str(settings.config_file) if settings.config_file is not None else "built-in defaults only"
    )

    try:
        tz = ZoneInfo(cfg.export.timezone)
    except Exception as exc:
        raise ConfigError(
            f"Invalid timezone '{cfg.export.timezone}'.",
            details=(
                "export.timezone must be an IANA time zone name, "
                "for example Europe/Berlin, UTC, or America/New_York."
            ),
            remedies=(
                Remedy(
                    "Correct the value in the configuration file, or override it for a single run:",
                    "hhe export --timezone Europe/Berlin --date yesterday",
                ),
            ),
            context={"config_file": config_source},
        ) from exc

    log_level = getattr(logging, (args.log_level or "INFO").upper(), logging.INFO)
    log_file = configure_logging(log_level, cfg, tz)

    output_dir = Path(cfg.export.output_dir).resolve()
    logger.info("Output directory: %s", output_dir)
    logger.info("Configuration: %s", config_source)
    if log_file is not None:
        logger.info("Log file: %s", log_file)

    if settings.config_file is None:
        logger.warning(
            "No configuration file found; running with built-in defaults. "
            "Exports go to %s. Run 'hhe config path' to see where a "
            "configuration file is expected, or 'hhe init' to create one.",
            output_dir,
        )

    try:
        requested_start, requested_end = resolve_date_range(args, tz)
    except ValueError as exc:
        raise UsageError(
            str(exc),
            details="The requested export range could not be interpreted.",
            remedies=(
                Remedy(
                    "Export the most recent complete days, one day, or a range:",
                    "hhe export --last-days 7",
                ),
            ),
        ) from exc

    partial_day = requested_partial_day(args, tz)
    if partial_day is not None:
        logger.warning(
            "Today (%s) is not over yet. Exporting it now captures only the "
            "state changes recorded so far. The day is recorded as "
            "status=partial, and a later run will export it again in full.",
            partial_day,
        )

    plan = plan_export(
        cfg,
        requested_start=requested_start,
        requested_end=requested_end,
        today=today_local(tz),
        tz=tz,
        force=args.force,
        partial_day=partial_day,
    )

    if not plan.days_to_export and not args.dry_run and not cfg.snapshot_only:
        logger.info(
            "Nothing to export - %d day(s) skipped (existing) and %d day(s) "
            "skipped (not yet complete).",
            len(plan.days(DayAction.SKIP_EXISTING)),
            len(plan.days(DayAction.SKIP_INCOMPLETE)),
        )
        return 0

    # Resolved through the module so the transport stays a testable seam.
    with ha_client.HomeAssistantClient(
        url=cfg.homeassistant.url,
        token=settings.token,
        timeout=cfg.requests.timeout,
        max_retries=cfg.requests.max_retries,
        backoff_seconds=cfg.requests.backoff,
    ) as client:
        logger.info("Checking the Home Assistant API at %s ...", cfg.homeassistant.url)
        client.check_api()

        logger.info("Fetching entity list from /api/states ...")
        states = client.get_states()

        created_local = datetime.now(tz)
        snapshot = snapshot_document(states, created_local)
        snapshot_path = write_snapshot(
            cfg.layout, snapshot, created_local, new_run_id(created_local.astimezone(UTC))
        )
        logger.info(
            "Entity snapshot saved: %s (%d entities).", snapshot_path.name, snapshot["entity_count"]
        )

        if cfg.snapshot_only:
            logger.info(
                "Snapshot-only mode complete - saved %d current entity states; "
                "no history was requested and no daily manifest was written.",
                snapshot["entity_count"],
            )
            return 0

        selected = select_entities(
            states,
            EntitySelection(
                include_unknown=cfg.entities.include_unknown,
                include_unavailable=cfg.entities.include_unavailable,
                exclude_domains=cfg.entities.exclude_domains,
                exclude_patterns=cfg.entities.exclude_patterns,
            ),
        )
        if selected.excluded_count:
            logger.info(
                "Optional excludes removed %d entities (%d remain).",
                selected.excluded_count,
                len(selected.entity_ids),
            )

        print_plan(
            plan,
            entity_count=len(selected.entity_ids),
            unknown_count=snapshot["entity_count_unknown"],
            unavailable_count=snapshot["entity_count_unavailable"],
            batch_size=cfg.requests.batch_size,
            output_dir=str(output_dir),
            config_source=config_source,
            log_file=str(log_file) if log_file is not None else None,
        )

        if args.dry_run:
            logger.info("Dry run complete - no history data was fetched.")
            return 0

        if not plan.days_to_export:
            logger.info("Nothing to export - all requested days are already done.")
            return 0

        logger.info(
            "Starting export: %d day(s), %d entities, batch size %d.",
            len(plan.days_to_export),
            len(selected.entity_ids),
            cfg.requests.batch_size,
        )

        exit_code = run_export(
            cfg=cfg,
            plan=plan,
            entity_ids=selected.entity_ids,
            entity_count_total=selected.candidate_count,
            client=client,
            tz=tz,
        )

        for day in plan.partial_days:
            logger.warning(
                "Day %s was written from an incomplete day and is marked "
                "status=partial. The next run that includes it will replace "
                "it with the complete day.",
                day,
            )
        if log_file is not None:
            logger.info("Log file: %s", log_file)
        return exit_code


def print_plan(
    plan: ExportPlan,
    *,
    entity_count: int,
    unknown_count: int,
    unavailable_count: int,
    batch_size: int,
    output_dir: str,
    config_source: str,
    log_file: str | None,
) -> None:
    """Print the plan: where the run writes and what configured it come first."""
    batches_per_day = math.ceil(entity_count / batch_size) if entity_count else 0
    lines = ["", "=" * 60, "  Home Assistant REST History Exporter", "=" * 60]
    for label, value in (
        ("Output", output_dir),
        ("Configuration", config_source),
        ("Log file", log_file),
    ):
        if value is not None:
            lines.append(f"  {label:<16}: {value}")
    lines += [
        "",
        f"  Requested range : {plan.requested_start} -> {plan.requested_end}",
        f"  Current local   : {plan.today}",
        f"  Latest complete : {plan.latest_complete}",
        "",
        f"  Entities (from /api/states): {entity_count}",
        f"    unknown     : {unknown_count}",
        f"    unavailable : {unavailable_count}",
        f"  Batch size      : {batch_size}",
        f"  Requests / day  : ~{batches_per_day}",
        "",
    ]
    partial = set(plan.partial_days)
    if plan.days_to_export:
        lines.append(f"  Will export ({len(plan.days_to_export)}):")
        for day in plan.days_to_export:
            marker = "  (partial - today is not over yet)" if day in partial else ""
            lines.append(f"    {day}{marker}")
    else:
        lines.append("  Nothing to export.")
    for action, heading in (
        (DayAction.SKIP_EXISTING, "Will skip - already exported"),
        (DayAction.SKIP_INCOMPLETE, "Will skip - not yet complete"),
    ):
        days = plan.days(action)
        if days:
            lines.append(f"\n  {heading} ({len(days)}):")
            lines.extend(f"    {day}" for day in days)
    lines.append("=" * 60 + "\n")
    print("\n".join(lines))


def requested_partial_day(args: argparse.Namespace, tz: ZoneInfo) -> date | None:
    """The running day, when --date named it; otherwise None.

    Only a single-day selection can ask for today. A range or --last-days
    that happens to include today skips it, exactly as before: reaching into
    an unfinished day must be something the caller said, not something that
    happens to them. See internal dev doc, Teil-Export des heutigen Tages.
    """
    if not getattr(args, "date", None):
        return None
    day = parse_date_arg(args.date, tz)
    return day if day == today_local(tz) else None


# ── argument translation ──────────────────────────────────────────────────────


def parse_format_list(raw: str) -> frozenset[Format]:
    """Turn ``--format jsonl,parquet`` into the complete format set.

    ``--format`` always states the whole set, never a change to it, so what a
    command asks for is readable from the command alone.
    """
    try:
        return schema.format_set(raw, "--format")
    except ConfigError as exc:
        raise UsageError(
            exc.summary,
            details=exc.details,
            remedies=(
                Remedy(
                    "Select one or more formats:",
                    "hhe export --format jsonl,parquet",
                ),
                Remedy(
                    "Or capture the entity snapshot only:",
                    "hhe export --format none",
                ),
            ),
        ) from exc


def cli_overrides(args: argparse.Namespace) -> dict[str, object]:
    """Translate parsed arguments into configuration overrides."""
    overrides: dict[str, object] = {}
    for dest, key_path in _VALUE_OVERRIDES.items():
        value = getattr(args, dest, None)
        if value is None:
            continue
        overrides[key_path] = parse_format_list(value) if dest == "format" else value
    return overrides


def resolve_date_range(args: argparse.Namespace, tz: ZoneInfo) -> tuple[date, date]:
    """Return the requested (start_date, end_date), inclusive.

    Without a selection the answer is the most recent complete day: that is what
    a daily run wants, and it needs no argument to say so.
    """
    if getattr(args, "last_days", None) is not None:
        return last_n_complete_days(args.last_days, tz)

    if args.date:
        day = parse_date_arg(args.date, tz)
        return day, day

    if args.start_date:
        if not args.end_date:
            raise ValueError("--end-date is required when --start-date is used.")
        start = parse_date_arg(args.start_date, tz)
        end = parse_date_arg(args.end_date, tz)
        if start > end:
            raise ValueError(f"--start-date ({start}) must not be after --end-date ({end}).")
        return start, end

    if args.end_date:
        raise ValueError("--start-date is required when --end-date is used.")

    latest = latest_complete_day(tz)
    return latest, latest


# ── logging ───────────────────────────────────────────────────────────────────

_HHE_HANDLER = "_hhe_handler"


def configure_logging(level: int, cfg: Config, tz: ZoneInfo) -> Path | None:
    """Set up stderr and file logging, idempotently; return the log file.

    Repeated calls in one process replace HHE's own handlers instead of
    stacking them, so a library user or a test may call the CLI more than once.
    Returns ``None`` when no log file could be opened.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HHE_HANDLER, False):
            root.removeHandler(handler)
            handler.close()

    root.setLevel(level)

    fmt = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(logging.Formatter(fmt, datefmt))
    setattr(stream_handler, _HHE_HANDLER, True)
    root.addHandler(stream_handler)

    log_file: Path | None = None
    try:
        path = cfg.layout.log_file(datetime.now(tz))
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(fmt, datefmt))
        setattr(file_handler, _HHE_HANDLER, True)
        root.addHandler(file_handler)
        log_file = path
    except Exception as exc:
        logger.warning("Could not create log file: %s", exc)
        log_file = None

    # Suppress urllib3/requests noise unless DEBUG.
    if level > logging.DEBUG:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests").setLevel(logging.WARNING)

    return log_file


__all__ = [
    "cli_overrides",
    "configure_logging",
    "parse_format_list",
    "resolve_date_range",
    "run",
]
