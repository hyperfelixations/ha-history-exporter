"""The ``export`` command: fetch Home Assistant history for complete days.

This module owns the export flow only. Argument grammar lives in
:mod:`ha_history_exporter.cli.parser`, error rendering and exit codes in
:mod:`ha_history_exporter.cli`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from ... import ha_client
from ...entity_snapshot import (
    apply_optional_excludes,
    build_snapshot,
    extract_entity_ids,
    save_snapshot,
)
from ...errors import ConfigError, Remedy, UsageError
from ...exporter import run_export
from ...planner import build_plan
from ...runtime.workspace import new_run_id
from ...settings import Config, Format, load_settings, schema
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
    settings = load_settings(
        explicit_config=args.config,
        cli_overrides=cli_overrides(args),
    )
    cfg = settings.config
    config_source = (
        str(settings.config_file)
        if settings.config_file is not None
        else "built-in defaults only"
    )

    try:
        tz = ZoneInfo(cfg.export.timezone)
    except Exception as exc:
        raise ConfigError(
            f"Invalid timezone '{cfg.export.timezone}': {exc}",
            details=(
                "export.timezone must be an IANA time zone name, "
                "for example Europe/Berlin, UTC, or America/New_York."
            ),
            remedies=(
                Remedy(
                    "Correct the value in the configuration file, or override "
                    "it for a single run:",
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

    plan = build_plan(
        requested_start=requested_start,
        requested_end=requested_end,
        tz=tz,
        cfg=cfg,
        force=args.force,
        partial_day=partial_day,
    )

    if not plan.days_to_export and not args.dry_run and not cfg.snapshot_only:
        logger.info(
            "Nothing to export - %d day(s) skipped (existing) and %d day(s) "
            "skipped (not yet complete).",
            len(plan.days_skipped_existing),
            len(plan.days_skipped_incomplete),
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

        logger.info("Checking HA API at %s ...", cfg.homeassistant.url)
        client.check_api()

        logger.info("Fetching entity list from /api/states ...")
        states = client.get_states()

        snapshot = build_snapshot(states, tz)
        save_snapshot(snapshot, cfg.layout.metadata_dir, run_id=new_run_id())

        if cfg.snapshot_only:
            logger.info(
                "Snapshot-only mode complete - saved %d current entity states; "
                "no history was requested and no daily manifest was written.",
                snapshot["entity_count"],
            )
            return 0

        entity_ids = extract_entity_ids(
            states,
            include_unknown=cfg.entities.include_unknown,
            include_unavailable=cfg.entities.include_unavailable,
        )
        entity_count_total = len(entity_ids)

        entity_ids, excluded = apply_optional_excludes(
            entity_ids,
            cfg.entities.exclude_domains,
            cfg.entities.exclude_patterns,
        )
        if excluded:
            logger.info(
                "Optional excludes removed %d entities (%d remain).",
                excluded,
                len(entity_ids),
            )

        plan.print_summary(
            entity_count=len(entity_ids),
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
            len(entity_ids),
            cfg.requests.batch_size,
        )

        exit_code = run_export(
            cfg=cfg,
            plan=plan,
            entity_ids=entity_ids,
            entity_count_total=entity_count_total,
            client=client,
            tz=tz,
            dry_run=False,
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


def requested_partial_day(
    args: argparse.Namespace, tz: ZoneInfo
) -> date | None:
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
        overrides[key_path] = (
            parse_format_list(value) if dest == "format" else value
        )
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
            raise ValueError(
                f"--start-date ({start}) must not be after --end-date ({end})."
            )
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
    from datetime import datetime as _dt

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

    ts = _dt.now(tz).strftime("%Y-%m-%d_%H%M%S")
    log_dir = cfg.layout.logs_dir
    log_file: Path | None = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"ha_history_export_{ts}.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(fmt, datefmt))
        setattr(file_handler, _HHE_HANDLER, True)
        root.addHandler(file_handler)
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
