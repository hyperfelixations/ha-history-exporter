"""``export``: translate the options, follow the run, report its result."""

from __future__ import annotations

import argparse
import logging

from ...app.export import ExportHooks, ExportRequest, run_export
from ...app.results import EntitySummary
from ...archive.descriptor import ArchiveZone
from ...domain.plan import DaySelection, ExportPlan, parse_day
from ...errors import Remedy, UsageError
from ...settings import resolve
from ..logging import CommandLogging
from ..output import print_export_result, print_plan
from ..parser import overrides
from ..services import Services

logger = logging.getLogger(__name__)


def run(args: argparse.Namespace, services: Services, logs: CommandLogging) -> int:
    settings = resolve(
        environ=services.environ,
        cwd=services.cwd,
        explicit_config=args.config,
        cli=overrides(args),
    )
    for name in settings.unknown_environment:
        logger.warning("%s is not a variable HHE knows; it has no effect.", name)
    if settings.config_file is None:
        logger.warning(
            "No configuration file found; running with built-in defaults. Run "
            "'hhe init' to create one, or 'hhe config path' to see where it belongs."
        )
    config_source = str(settings.config_file or "built-in defaults only")
    cfg = settings.config

    def planned(plan: ExportPlan, entities: EntitySummary, zone: ArchiveZone) -> None:
        print_plan(
            plan,
            entities,
            zone,
            batch_size=cfg.requests.batch_size,
            output_dir=cfg.export.output_dir,
            config_source=config_source,
            log_file=logs.log_file,
        )

    result = run_export(
        ExportRequest(selection(args), force=args.force, dry_run=args.dry_run),
        cfg,
        lambda: services.connect(settings),
        clock=services.clock,
        sleep=services.sleep,
        hooks=ExportHooks(attach_log=logs.attach_file, planned=planned),
    )
    print_export_result(result)
    return int(result.exit_code)


def selection(args: argparse.Namespace) -> DaySelection:
    """The days the options name; without any, the most recent complete day."""
    try:
        return _selection(args)
    except ValueError as exc:
        raise UsageError(
            str(exc),
            remedies=(
                Remedy(
                    "Export the most recent complete days, one day, or a range:",
                    "hhe export --last-days 7",
                ),
            ),
        ) from None


def _selection(args: argparse.Namespace) -> DaySelection:
    if args.last_days is not None:
        if args.end_date:
            raise ValueError("--end-date belongs to --start-date, not to --last-days.")
        text = args.last_days.strip()
        if not text.isdigit() or int(text) < 1:
            raise ValueError(f"--last-days needs a whole number of at least 1, not '{text}'.")
        return DaySelection(last_days=int(text))
    if args.date:
        if args.end_date:
            raise ValueError("--end-date belongs to --start-date, not to --date.")
        return DaySelection.single(parse_day(args.date))
    if args.start_date or args.end_date:
        if not (args.start_date and args.end_date):
            raise ValueError("A range needs both --start-date and --end-date.")
        return DaySelection(start=parse_day(args.start_date), end=parse_day(args.end_date))
    return DaySelection()
