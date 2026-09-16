"""Command-line entry point.

Quick start (PowerShell):
    $env:HA_URL   = "http://homeassistant.local:8123"   # or Tailscale, etc.
    $env:HA_TOKEN = "<your-long-lived-access-token>"

    hhe export --last-days 7
    hhe export --date yesterday --dry-run

Both console commands, ``ha-history-exporter`` and the short ``hhe``, and
``python -m ha_history_exporter`` behave identically. A command is mandatory.

This module owns dispatch and the mapping from errors to exit codes:
    0 success · 1 runtime failure · 2 usage or configuration · 130 interrupted
"""

from __future__ import annotations

import argparse
import logging
from typing import Callable, List, Optional  # noqa: F401 - Optional is part of main()'s signature
from zoneinfo import ZoneInfo  # re-exported: callers build timezones from here

from ..errors import HHEError, Remedy
from .commands import config_cmd, doctor, export, init
from .output import render_error
from .parser import COMMANDS, build_parser, parse_args

_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "config": config_cmd.run,
    "doctor": doctor.run,
    "export": export.run,
    "init": init.run,
}


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)
    handler = _HANDLERS[args.command]
    logger = logging.getLogger(__name__)

    try:
        return handler(args)
    except HHEError as exc:
        _log_if_configured(logger.error, "%s", exc.summary)
        render_error(exc)
        return exc.exit_code
    except KeyboardInterrupt:
        _log_if_configured(logger.info, "Interrupted by user.")
        return 130
    except Exception as exc:
        location = _safe_exception_location(exc)
        if location is None:
            _log_if_configured(
                logger.error,
                "Unexpected internal error (%s).",
                type(exc).__name__,
            )
        else:
            _log_if_configured(
                logger.error,
                "Unexpected internal error (%s) at %s.",
                type(exc).__name__,
                location,
            )
        render_error(
            HHEError(
                "Unexpected internal error.",
                details=(
                    "This is either a defect in HHE or an unhandled "
                    "environment condition. Arbitrary exception text and "
                    "tracebacks are deliberately not written to the terminal "
                    "or log because they may contain private data."
                ),
                remedies=(
                    Remedy(
                        "Re-run with debug logging and keep the log file:",
                        "hhe export --log-level DEBUG --date yesterday",
                    ),
                ),
            )
        )
        return 1


def _safe_exception_location(exc: BaseException) -> str | None:
    """Return the deepest package frame without exposing filesystem paths."""
    location = None
    traceback = exc.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        module = frame.f_globals.get("__name__")
        if isinstance(module, str) and (
            module == "ha_history_exporter"
            or module.startswith("ha_history_exporter.")
        ):
            location = f"{module}:{frame.f_code.co_name}:{traceback.tb_lineno}"
        traceback = traceback.tb_next
    return location


def _log_if_configured(
    log: Callable[..., None], message: str, *args: object
) -> None:
    """Log only once logging is set up.

    A failure before the log file exists would otherwise reach the terminal
    twice: once through logging's last-resort handler and once through the
    rendered error block.
    """
    if logging.getLogger().handlers:
        log(message, *args)


# Backwards-compatible aliases for the pre-package module layout.
_parse_args = parse_args
_resolve_date_range = export.resolve_date_range
cli_overrides = export.cli_overrides
_configure_logging = export.configure_logging

__all__ = [
    "COMMANDS",
    "ZoneInfo",
    "build_parser",
    "cli_overrides",
    "main",
    "parse_args",
]
