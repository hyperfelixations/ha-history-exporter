"""Dispatch and exit codes.

Each command receives the services it may use - environment, working
directory, standard streams, the Home Assistant source, the clock, prompts,
the editor - so a test drives the real command without replacing a module.
An error becomes one error block on standard error and its exit code; a
defect additionally leaves its traceback in the log file of the run. See
internal dev doc § "Errors and exit codes".
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable

from ..errors import ExitCode, HHEError, InternalError
from .commands import config, doctor, export, init
from .logging import FILE_ONLY, CommandLogging
from .output import render_error
from .parser import build_parser
from .services import Services

logger = logging.getLogger(__name__)


def main(
    argv: list[str] | None = None, *, prog: str = "hhe", services: Services | None = None
) -> int:
    services = services or Services.system()
    try:
        args = build_parser(prog).parse_args(argv)
    except SystemExit as exit_:
        return int(exit_.code or 0)
    level = getattr(logging, getattr(args, "log_level", "WARNING"))
    logs = CommandLogging(level, services.stderr, buffered=args.command == "export")
    try:
        return int(_HANDLERS[args.command](args, services, logs))
    except HHEError as exc:
        logger.error("%s", exc.summary, extra=FILE_ONLY)
        render_error(exc, stream=services.stderr, environ=services.environ)
        return int(exc.exit_code)
    except KeyboardInterrupt:
        logger.warning("Interrupted.", extra=FILE_ONLY)
        services.stderr.write("\nInterrupted.\n")
        return int(ExitCode.INTERRUPTED)
    except Exception as exc:
        logger.error("Unexpected internal error.", exc_info=exc, extra=FILE_ONLY)
        render_error(
            InternalError.during(f"{prog} {args.command}"),
            stream=services.stderr,
            environ=services.environ,
        )
        return int(ExitCode.INTERNAL)
    finally:
        logs.close()


_HANDLERS: dict[str, Callable[[argparse.Namespace, Services, CommandLogging], int]] = {
    "config": config.run,
    "doctor": doctor.run,
    "export": export.run,
    "init": init.run,
}
