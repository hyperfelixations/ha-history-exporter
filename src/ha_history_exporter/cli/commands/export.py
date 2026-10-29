"""``export``: translate the options, follow the run, report its result."""

from __future__ import annotations

import argparse
import logging

from ...app.export import ExportHooks, ExportRequest, run_export
from ...app.results import EntitySummary
from ...archive.descriptor import ArchiveZone
from ...domain.plan import DaySelection, ExportPlan
from ...settings import resolve
from ..days import day_selection
from ..logging import CommandLogging
from ..output import print_export_result, print_json, print_plan
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
        if args.json:
            return
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
        ExportRequest(
            day_selection(args, "export") or DaySelection(), force=args.force, dry_run=args.dry_run
        ),
        cfg,
        lambda: services.connect(settings),
        clock=services.clock,
        sleep=services.sleep,
        hooks=ExportHooks(attach_log=logs.attach_file, planned=planned),
    )
    if args.json:
        print_json(result.to_json())
    else:
        print_export_result(result)
    return int(result.exit_code)
