"""The ``export`` command: fetch Home Assistant history for complete days.

This module owns the export flow only. Argument grammar lives in
:mod:`ha_history_exporter.cli.parser`, error rendering and exit codes in
:mod:`ha_history_exporter.cli`.
"""

from __future__ import annotations

import logging
import sys
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
from ...settings import load_settings
from ...time_utils import last_n_complete_days, parse_date_arg

logger = logging.getLogger(__name__)

#: Command-line options that override a configuration key. Switches are listed
#: separately because argparse cannot distinguish "not given" from "false".
_VALUE_OVERRIDES = {
    "outdir": "export.output_dir",
    "timezone": "home_assistant.timezone",
    "batch_size": "requests.batch_size_entities",
    "sleep_between_requests": "requests.sleep_between_requests_seconds",
    "sleep_between_days": "requests.sleep_between_days_seconds",
    "timeout": "requests.request_timeout_seconds",
    "max_retries": "requests.max_retries",
}

_LEGACY_FORMAT_SWITCHES = ("jsonl", "parquet", "no_csv")

_VALID_FORMATS = ("jsonl", "csv", "parquet", "none")


def run(args) -> int:
    """Execute one export run and return the process exit code."""
    settings = load_settings(
        explicit_config=args.config,
        cli_overrides=cli_overrides(args),
    )
    cfg = settings.config
    config_files = ", ".join(str(path) for path in settings.config_files)

    try:
        tz = ZoneInfo(cfg.home_assistant.timezone)
    except Exception as exc:
        raise ConfigError(
            f"Invalid timezone '{cfg.home_assistant.timezone}': {exc}",
            details=(
                "home_assistant.timezone must be an IANA time zone name, "
                "for example Europe/Berlin, UTC, or America/New_York."
            ),
            remedies=(
                Remedy(
                    "Correct the value in the configuration file, or override "
                    "it for a single run:",
                    "hhe export --timezone Europe/Berlin --date yesterday",
                ),
            ),
            context={"config_file": config_files or "none"},
        ) from exc

    log_level = getattr(logging, (args.log_level or "INFO").upper(), logging.INFO)
    configure_logging(log_level, cfg, tz)

    logger.info("Output directory: %s", Path(cfg.export.output_dir).resolve())
    logger.info("Configuration: %s", config_files or "built-in defaults only")

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

    force = args.force or cfg.export.force
    resume = not force and (args.resume or cfg.export.resume)

    plan = build_plan(
        requested_start=requested_start,
        requested_end=requested_end,
        tz=tz,
        cfg=cfg,
        force=force,
        resume=resume,
    )

    if not plan.days_to_export and not args.dry_run and not cfg.snapshot_only:
        logger.info(
            "Nothing to export — %d day(s) skipped (existing) and %d day(s) "
            "skipped (not yet complete).",
            len(plan.days_skipped_existing),
            len(plan.days_skipped_incomplete),
        )
        return 0

    # Resolved through the module so the transport stays a testable seam.
    with ha_client.HomeAssistantClient(
        url=cfg.ha_url,
        token=cfg.ha_token,
        timeout=cfg.requests.request_timeout_seconds,
        max_retries=cfg.requests.max_retries,
        backoff_seconds=cfg.requests.backoff_seconds,
    ) as client:

        logger.info("Checking HA API at %s …", cfg.ha_url)
        client.check_api()

        logger.info("Fetching entity list from /api/states …")
        states = client.get_states()

        snapshot = build_snapshot(states, tz)
        save_snapshot(snapshot, cfg.metadata_dir)

        if cfg.snapshot_only:
            logger.info(
                "Snapshot-only mode complete — saved %d current entity states; "
                "no history was requested and no daily manifest was written.",
                snapshot["entity_count"],
            )
            return 0

        entity_ids = extract_entity_ids(
            states,
            include_unknown=cfg.entity_selection.include_unknown,
            include_unavailable=cfg.entity_selection.include_unavailable,
        )
        entity_count_total = len(entity_ids)

        entity_ids, excluded = apply_optional_excludes(
            entity_ids,
            cfg.entity_selection.optional_exclude_domains,
            cfg.entity_selection.optional_exclude_patterns,
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
            batch_size=cfg.requests.batch_size_entities,
        )

        if args.dry_run:
            logger.info("Dry run complete — no history data was fetched.")
            return 0

        if not plan.days_to_export:
            logger.info("Nothing to export — all requested days are already done.")
            return 0

        logger.info(
            "Starting export: %d day(s), %d entities, batch size %d.",
            len(plan.days_to_export),
            len(entity_ids),
            cfg.requests.batch_size_entities,
        )

        return run_export(
            cfg=cfg,
            plan=plan,
            entity_ids=entity_ids,
            entity_count_total=entity_count_total,
            client=client,
            tz=tz,
            dry_run=False,
        )


# ── argument translation ──────────────────────────────────────────────────────

def parse_format_list(raw: str) -> dict[str, bool]:
    """Turn ``--format jsonl,parquet`` into an absolute format selection."""
    requested = [item.strip().lower() for item in raw.split(",") if item.strip()]
    if not requested:
        raise UsageError(
            "--format needs at least one value.",
            details=f"Valid values: {', '.join(_VALID_FORMATS)}.",
        )

    unknown = [item for item in requested if item not in _VALID_FORMATS]
    if unknown:
        raise UsageError(
            f"Unknown output format(s): {', '.join(unknown)}.",
            details=f"Valid values: {', '.join(_VALID_FORMATS)}.",
            remedies=(
                Remedy("Select one or more formats:", "hhe export --format jsonl,parquet"),
                Remedy("Or capture the entity snapshot only:", "hhe export --format none"),
            ),
        )

    if "none" in requested and len(requested) > 1:
        raise UsageError(
            "--format none cannot be combined with another format.",
            details=(
                "'none' means snapshot-only: no History API request and no "
                "daily manifest."
            ),
        )

    selected = set(requested)
    return {
        "formats.jsonl": "jsonl" in selected,
        "formats.csv": "csv" in selected,
        "formats.parquet": "parquet" in selected,
    }


def cli_overrides(args) -> dict:
    """Translate parsed arguments into configuration overrides."""
    overrides: dict = {}
    for dest, key_path in _VALUE_OVERRIDES.items():
        value = getattr(args, dest, None)
        if value is not None:
            overrides[key_path] = value

    legacy_used = [
        name for name in _LEGACY_FORMAT_SWITCHES if getattr(args, name, False)
    ]
    if getattr(args, "format", None) is not None:
        if legacy_used:
            raise UsageError(
                "--format cannot be combined with --jsonl, --parquet, or --no-csv.",
                details=(
                    "--format states the complete set of output formats, while "
                    "the older switches only add or remove one."
                ),
                remedies=(
                    Remedy("Use --format alone:", "hhe export --format jsonl,parquet"),
                ),
            )
        overrides.update(parse_format_list(args.format))
        return overrides

    # Legacy switches are one-directional by design: --jsonl and --parquet only
    # enable, --no-csv only disables.
    if getattr(args, "jsonl", False):
        overrides["formats.jsonl"] = True
    if getattr(args, "parquet", False):
        overrides["formats.parquet"] = True
    if getattr(args, "no_csv", False):
        overrides["formats.csv"] = False
    return overrides


def resolve_date_range(args, tz):
    """Return (start_date, end_date) as date objects."""
    if getattr(args, "last_days", None) is not None:
        return last_n_complete_days(args.last_days, tz)

    if args.date:
        day = parse_date_arg(args.date, tz)
        if args.date.lower() == "today":
            print(
                f"[WARNING] --date today ({day}) is not yet complete "
                "and will be skipped.",
                file=sys.stderr,
            )
        return day, day

    if not args.end_date:
        raise ValueError("--end-date is required when --start-date is used.")
    start = parse_date_arg(args.start_date, tz)
    end = parse_date_arg(args.end_date, tz)
    if start > end:
        raise ValueError(
            f"--start-date ({start}) must not be after --end-date ({end})."
        )
    return start, end


# ── logging ───────────────────────────────────────────────────────────────────

_HHE_HANDLER = "_hhe_handler"


def configure_logging(level: int, cfg, tz: ZoneInfo) -> None:
    """Set up stderr and file logging, idempotently.

    Repeated calls in one process replace HHE's own handlers instead of
    stacking them, so a library user or a test may call the CLI more than once.
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
    log_dir = cfg.logs_dir
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"ha_history_export_{ts}.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(fmt, datefmt))
        setattr(file_handler, _HHE_HANDLER, True)
        root.addHandler(file_handler)
        logger.debug("Log file: %s", log_file)
    except Exception as exc:
        logger.warning("Could not create log file: %s", exc)

    # Suppress urllib3/requests noise unless DEBUG.
    if level > logging.DEBUG:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests").setLevel(logging.WARNING)


__all__ = [
    "cli_overrides",
    "configure_logging",
    "parse_format_list",
    "resolve_date_range",
    "run",
]
