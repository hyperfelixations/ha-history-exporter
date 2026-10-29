"""``convert``: derive formats from the stored days, without Home Assistant."""

from __future__ import annotations

import argparse

from ...app.convert import ConvertHooks, ConvertRequest, run_convert
from ...domain.formats import FormatSetError, parse_format_set
from ...errors import Remedy, UsageError
from ...settings import resolve
from ..days import day_selection
from ..logging import CommandLogging
from ..output import print_convert, print_json
from ..parser import overrides
from ..services import Services


def run(args: argparse.Namespace, services: Services, logs: CommandLogging) -> int:
    try:
        formats = parse_format_set(args.format)
    except FormatSetError as exc:
        raise UsageError(
            f"--format {exc.problem}",
            details=exc.details,
            remedies=(Remedy("For example:", "hhe convert --format parquet"),),
        ) from None
    settings = resolve(
        environ=services.environ,
        cwd=services.cwd,
        explicit_config=args.config,
        cli=overrides(args),
    )
    result = run_convert(
        ConvertRequest(formats, day_selection(args, "convert")),
        settings.config,
        clock=services.clock,
        hooks=ConvertHooks(attach_log=logs.attach_file),
    )
    if args.json:
        print_json(result.to_json())
    else:
        print_convert(result)
    return int(result.exit_code)
