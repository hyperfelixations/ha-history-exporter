"""``status``: which days the archive holds, month by month; change nothing."""

from __future__ import annotations

import argparse

from ...app.status import archive_status
from ...settings import resolve
from ..days import day_selection
from ..logging import CommandLogging
from ..output import print_json, print_status
from ..parser import overrides
from ..services import Services


def run(args: argparse.Namespace, services: Services, logs: CommandLogging) -> int:
    settings = resolve(
        environ=services.environ,
        cwd=services.cwd,
        explicit_config=args.config,
        cli=overrides(args),
    )
    result = archive_status(settings.config, day_selection(args, "status"), clock=services.clock)
    if args.json:
        print_json(result.to_json())
    else:
        print_status(result)
    return int(result.exit_code)
