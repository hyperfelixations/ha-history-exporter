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
from .commands import config, convert, doctor, export, init, status, verify
from .logging import FILE_ONLY, CommandLogging
from .output import error_document, print_json, render_error
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
    logs = CommandLogging(level, services.stderr, buffered=args.command in _WRITERS)
    try:
        return int(_HANDLERS[args.command](args, services, logs))
    except HHEError as exc:
        logger.error("%s", exc.summary, extra=FILE_ONLY)
        return _failed(exc, args, services)
    except KeyboardInterrupt:
        logger.warning("Interrupted.", extra=FILE_ONLY)
        services.stderr.write("\nInterrupted.\n")
        return int(ExitCode.INTERRUPTED)
    except Exception:
        logger.exception("Unexpected internal error.", extra=FILE_ONLY)
        return _failed(InternalError.during(f"{prog} {args.command}"), args, services)
    finally:
        logs.close()


def _failed(exc: HHEError, args: argparse.Namespace, services: Services) -> int:
    """Report *exc* on standard error, and as a document with ``--json``."""
    render_error(exc, stream=services.stderr, environ=services.environ)
    if getattr(args, "json", False):
        print_json(error_document(args.command, exc))
    return int(exc.exit_code)


_HANDLERS: dict[str, Callable[[argparse.Namespace, Services, CommandLogging], int]] = {
    "config": config.run,
    "convert": convert.run,
    "doctor": doctor.run,
    "export": export.run,
    "init": init.run,
    "status": status.run,
    "verify": verify.run,
}

#: Commands that write an archive and keep a log file of the run there.
_WRITERS = frozenset({"export", "convert"})
