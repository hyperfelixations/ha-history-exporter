"""``verify``: compare the stored days with their manifests; change nothing."""

from __future__ import annotations

import argparse

from ...app.verify import Depth, verify_archive
from ...settings import resolve
from ..days import day_selection
from ..logging import CommandLogging
from ..output import print_json, print_verify
from ..parser import overrides
from ..services import Services


def run(args: argparse.Namespace, services: Services, logs: CommandLogging) -> int:
    settings = resolve(
        environ=services.environ,
        cwd=services.cwd,
        explicit_config=args.config,
        cli=overrides(args),
    )
    result = verify_archive(
        settings.config,
        day_selection(args, "verify"),
        depth=Depth.DECODED if args.deep else Depth.CHECKSUMS,
        clock=services.clock,
    )
    if args.json:
        print_json(result.to_json())
    else:
        print_verify(result)
    return int(result.exit_code)
