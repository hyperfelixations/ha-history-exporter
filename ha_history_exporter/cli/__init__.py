"""Command-line entry point.

Quick start (PowerShell):
    $env:HA_URL   = "http://homeassistant.local:8123"   # or Tailscale, etc.
    $env:HA_TOKEN = "<your-long-lived-access-token>"

    hhe export --last-days 7
    hhe export --date yesterday --dry-run

Both console commands, ``ha-history-exporter`` and the short ``hhe``, and
``python -m ha_history_exporter`` behave identically. The historical flag-only
form without a command keeps working.

This module owns dispatch and the mapping from errors to exit codes:
    0 success · 1 runtime failure · 2 usage or configuration · 130 interrupted
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo  # re-exported: callers build timezones from here

from ..console import render_error
from ..errors import HHEError, Remedy
from .commands import config_cmd, doctor, export, init
from .parser import COMMANDS, build_parser, normalize_argv, parse_args

_HANDLERS: dict[str, Callable[[object], int]] = {
    "config": config_cmd.run,
    "doctor": doctor.run,
    "export": export.run,
    "init": init.run,
}


def main(argv: Optional[List[str]] = None) -> int:
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
    except Exception as exc:  # noqa: BLE001 - last line of defence
        _log_if_configured(logger.exception, "Unexpected error: %s", exc)
        render_error(
            HHEError(
                f"Unexpected error: {exc}",
                details=(
                    "This is either a defect in HHE or an unhandled "
                    "environment condition. When a log file was already open, "
                    "it contains the full traceback."
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


def _log_if_configured(log, message: str, *args) -> None:
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
    "normalize_argv",
    "parse_args",
]
