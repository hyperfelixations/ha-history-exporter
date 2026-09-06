"""Command-line interface for the Home Assistant history exporter.

Quick start (PowerShell):
    $env:HA_URL   = "http://homeassistant.local:8123"   # or Tailscale IP, etc.
    $env:HA_TOKEN = "<your-long-lived-access-token>"

    ha-history-exporter --date yesterday --dry-run
    ha-history-exporter --date yesterday
    ha-history-exporter --start-date 2026-06-10 --end-date 2026-06-15

    When installed with pip/pipx, run the command above. In a cloned
    checkout, run "python ha_history_batch_export.py" instead — same
    arguments, same behaviour.

Why a package and not a single file?
    Robustness requires modular design: each concern (auth, time zones, streaming
    writes, validation, retry logic) is isolated so failures are easier to trace
    and fix.  From your perspective it is still a single command.  The package
    folder is an implementation detail you do not need to touch.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

from .config import load_config, validate_config
from .exceptions import AuthError, ConfigError
from .exporter import run_export
from .time_utils import is_day_complete, iter_days, parse_date_arg, today_local


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    cfg_path = Path(args.config)

    # ── load config first (needed for log dir path) ───────────────────────────
    try:
        cfg = load_config(cfg_path)
    except ConfigError as exc:
        print(f"\n[ERROR] {exc}\n", file=sys.stderr)
        return 2

    # ── CLI overrides ─────────────────────────────────────────────────────────
    if args.outdir:
        cfg.export.output_dir = args.outdir
    if args.timezone:
        cfg.home_assistant.timezone = args.timezone
    if args.batch_size is not None:
        cfg.requests.batch_size_entities = args.batch_size
    if args.sleep_between_requests is not None:
        cfg.requests.sleep_between_requests_seconds = args.sleep_between_requests
    if args.sleep_between_days is not None:
        cfg.requests.sleep_between_days_seconds = args.sleep_between_days
    if args.timeout is not None:
        cfg.requests.request_timeout_seconds = args.timeout
    if args.max_retries is not None:
        cfg.requests.max_retries = args.max_retries
    if args.no_csv:
        cfg.formats.csv = False
    if args.jsonl:
        cfg.formats.jsonl = True
    if args.parquet:
        cfg.formats.parquet = True
    if args.log_level:
        args.log_level_str = args.log_level  # used below

    try:
        validate_config(cfg)
        tz = ZoneInfo(cfg.home_assistant.timezone)
    except ConfigError as exc:
        print(f"\n[ERROR] {exc}\n", file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            f"\n[ERROR] Invalid timezone '{cfg.home_assistant.timezone}': {exc}\n",
            file=sys.stderr,
        )
        return 2

    # ── logging setup ─────────────────────────────────────────────────────────
    log_level = getattr(logging, (args.log_level or "INFO").upper(), logging.INFO)
    _configure_logging(log_level, cfg, tz)

    logger = logging.getLogger(__name__)

    # ── date range ────────────────────────────────────────────────────────────
    try:
        requested_start, requested_end = _resolve_date_range(args, tz)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    # ── force / resume ────────────────────────────────────────────────────────
    force = args.force or cfg.export.force
    resume = not force and (args.resume or cfg.export.resume)

    # ── build export plan ─────────────────────────────────────────────────────
    from .planner import build_plan
    plan = build_plan(
        requested_start=requested_start,
        requested_end=requested_end,
        tz=tz,
        cfg=cfg,
        force=force,
        resume=resume,
    )

    # ── short-circuit if nothing to do and not a dry-run ─────────────────────
    if not plan.days_to_export and not args.dry_run and not cfg.snapshot_only:
        logger.info(
            "Nothing to export — %d day(s) skipped (existing) and %d day(s) "
            "skipped (not yet complete).",
            len(plan.days_skipped_existing),
            len(plan.days_skipped_incomplete),
        )
        return 0

    # ── connect to HA and get entity list ─────────────────────────────────────
    from .ha_client import HomeAssistantClient
    from .entity_snapshot import (
        apply_optional_excludes,
        build_snapshot,
        extract_entity_ids,
        save_snapshot,
    )

    try:
        with HomeAssistantClient(
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

            entity_ids = extract_entity_ids(states)
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

            # Print plan summary.
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

    except AuthError as exc:
        logger.error("Authentication failed: %s", exc)
        print(
            "\n[FATAL] Authentication failed.\n"
            "  → Open Home Assistant → Profile → Long-Lived Access Tokens\n"
            "  → Create or renew a token, then set:\n"
            "      $env:HA_TOKEN = \"<token>\"\n",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        return 130
    except Exception as exc:
        logger.exception("Unexpected error: %s", exc)
        return 1


# ── argument parser ───────────────────────────────────────────────────────────

def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="Export Home Assistant history to JSONL/CSV via the REST API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--config", default="export_config.yaml", metavar="FILE",
        help="YAML config file (default: export_config.yaml).",
    )

    # Date
    dg = p.add_mutually_exclusive_group(required=True)
    dg.add_argument("--date", metavar="DATE",
                    help="Single day: YYYY-MM-DD, 'yesterday', or 'today'.")
    dg.add_argument("--start-date", metavar="DATE",
                    help="Range start (requires --end-date).")
    p.add_argument("--end-date", metavar="DATE",
                   help="Range end inclusive (required with --start-date).")

    # Behaviour
    p.add_argument("--dry-run", action="store_true",
                   help="Show plan without fetching history.")
    p.add_argument("--force", action="store_true",
                   help="Re-export even if already status=ok.")
    p.add_argument("--resume", action="store_true",
                   help="Skip days already status=ok (default from config).")

    # Output
    p.add_argument("--outdir", metavar="DIR", help="Override output directory.")
    p.add_argument("--no-csv", action="store_true", help="Suppress CSV output.")
    p.add_argument("--jsonl", action="store_true", help="Ensure JSONL output (default).")
    p.add_argument("--parquet", action="store_true", help="Enable Parquet output (requires pyarrow).")

    # Tuning
    p.add_argument("--timezone", metavar="TZ",
                   help="Override timezone (default: Europe/Berlin).")
    p.add_argument("--batch-size", type=int, metavar="N",
                   help="Entities per history request (default: 5).")
    p.add_argument("--sleep-between-requests", type=float, metavar="S",
                   help="Seconds between batch requests.")
    p.add_argument("--sleep-between-days", type=float, metavar="S",
                   help="Seconds between days.")
    p.add_argument("--timeout", type=int, metavar="S",
                   help="HTTP request timeout in seconds.")
    p.add_argument("--max-retries", type=int, metavar="N",
                   help="Max retries on transient errors.")
    p.add_argument("--log-level", metavar="LEVEL",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Log level (default: INFO).")

    return p.parse_args(argv)


# ── helpers ───────────────────────────────────────────────────────────────────

def _resolve_date_range(args, tz):
    """Return (start_date, end_date) as date objects."""
    if args.date:
        d = parse_date_arg(args.date, tz)
        if args.date.lower() == "today":
            print(
                f"[WARNING] --date today ({d}) is not yet complete "
                "and will be skipped.",
                file=sys.stderr,
            )
        return d, d

    if not args.end_date:
        raise ValueError("--end-date is required when --start-date is used.")
    start = parse_date_arg(args.start_date, tz)
    end = parse_date_arg(args.end_date, tz)
    if start > end:
        raise ValueError(
            f"--start-date ({start}) must not be after --end-date ({end})."
        )
    return start, end


def _configure_logging(level: int, cfg, tz: ZoneInfo) -> None:
    """Set up stderr + file logging."""
    from datetime import datetime as _dt
    ts = _dt.now(tz).strftime("%Y-%m-%d_%H%M%S")

    log_dir = cfg.logs_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"ha_history_export_{ts}.log"

    fmt = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(level)

    # Stderr handler.
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(sh)

    # File handler.
    try:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter(fmt, datefmt))
        root.addHandler(fh)
        logging.getLogger(__name__).debug("Log file: %s", log_file)
    except Exception as exc:
        logging.getLogger(__name__).warning("Could not create log file: %s", exc)

    # Suppress urllib3/requests noise unless DEBUG.
    if level > logging.DEBUG:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests").setLevel(logging.WARNING)
