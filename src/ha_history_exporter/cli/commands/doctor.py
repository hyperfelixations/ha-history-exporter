"""``doctor``: check installation, configuration and archive; change nothing."""

from __future__ import annotations

import argparse

from ...app.doctor import diagnose
from ...settings import resolve
from ..logging import CommandLogging
from ..output import print_checks, print_json
from ..services import Services


def run(args: argparse.Namespace, services: Services, logs: CommandLogging) -> int:
    result = diagnose(
        lambda: resolve(environ=services.environ, cwd=services.cwd, explicit_config=args.config),
        connect=None if args.offline else services.connect,
    )
    if args.json:
        print_json(result.to_json())
    else:
        print_checks(result.checks)
    return int(result.exit_code)
