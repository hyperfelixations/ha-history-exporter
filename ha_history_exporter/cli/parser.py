"""Command-line grammar.

HHE grew from a single flag-only command into a tool with several commands.
Both forms stay valid: ``hhe export --date yesterday`` and the historical
``ha-history-exporter --date yesterday`` parse identically, because
:func:`normalize_argv` inserts the implicit ``export`` command.
"""

from __future__ import annotations

import argparse
from typing import List, Optional, Sequence

from .. import __version__

COMMANDS = ("export",)

_TOP_LEVEL_FLAGS = frozenset({"-h", "--help", "--version", "-V"})

FORMAT_CHOICES = ("jsonl", "csv", "parquet", "none")

EPILOG = """\
Examples:
  hhe export --last-days 7            the seven most recent complete days
  hhe export --date yesterday         a single day
  hhe export --date 2026-06-15 --force
  hhe export --start-date 2026-06-01 --end-date 2026-06-15
  hhe export --last-days 30 --format parquet

The Home Assistant URL and access token are read from the configuration, or
from the HA_URL and HA_TOKEN environment variables.
"""


def normalize_argv(argv: Sequence[str]) -> List[str]:
    """Insert the implicit 'export' command for the legacy flag-only form."""
    args = list(argv)
    if not args:
        return args
    head = args[0]
    if head in COMMANDS or head in _TOP_LEVEL_FLAGS:
        return args
    return ["export", *args]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export Home Assistant history through the REST API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    parser.add_argument(
        "--version", "-V", action="version", version=f"%(prog)s {__version__}"
    )
    subcommands = parser.add_subparsers(dest="command", metavar="COMMAND")
    subcommands.required = True

    export = subcommands.add_parser(
        "export",
        help="Export history for one day or a range of days.",
        description="Export Home Assistant history for complete local days.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    add_export_arguments(export)
    return parser


def add_export_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--config", metavar="FILE",
        help="Use exactly this YAML config file instead of the discovered ones.",
    )

    dates = p.add_mutually_exclusive_group(required=True)
    dates.add_argument("--date", metavar="DATE",
                       help="Single day: YYYY-MM-DD, 'yesterday', or 'today'.")
    dates.add_argument("--start-date", metavar="DATE",
                       help="Range start (requires --end-date).")
    dates.add_argument("--last-days", type=int, metavar="N",
                       help="The N most recent complete days, ending yesterday.")
    p.add_argument("--end-date", metavar="DATE",
                   help="Range end inclusive (required with --start-date).")

    p.add_argument("--dry-run", action="store_true",
                   help="Show plan without fetching history.")
    p.add_argument("--force", action="store_true",
                   help="Re-export even if already status=ok.")
    p.add_argument("--resume", action="store_true",
                   help="Skip days already status=ok (default from config).")

    p.add_argument("--outdir", metavar="DIR", help="Override output directory.")
    p.add_argument(
        "--format", metavar="LIST",
        help="Comma-separated output formats: jsonl, csv, parquet, or none.",
    )
    p.add_argument("--no-csv", action="store_true",
                   help="Suppress CSV output (deprecated, use --format).")
    p.add_argument("--jsonl", action="store_true",
                   help="Ensure JSONL output (deprecated, use --format).")
    p.add_argument("--parquet", action="store_true",
                   help="Enable Parquet output (deprecated, use --format).")

    p.add_argument("--timezone", metavar="TZ",
                   help="Override timezone (default: Europe/Berlin).")
    p.add_argument("--batch-size", type=int, metavar="N",
                   help="Entities per history request (default: 5).")
    p.add_argument("--sleep-between-requests", type=float, metavar="S",
                   help="Seconds between batch requests.")
    p.add_argument("--sleep-between-days", type=float, metavar="S",
                   help="Seconds between days.")
    p.add_argument("--timeout", type=int, metavar="S",
                   help="HTTP request timeout in seconds.")
    p.add_argument("--max-retries", type=int, metavar="N",
                   help="Max retries on transient errors.")
    p.add_argument("--log-level", metavar="LEVEL",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Log level (default: INFO).")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse *argv*, accepting both the command form and the legacy flag form."""
    import sys

    raw = sys.argv[1:] if argv is None else argv
    return build_parser().parse_args(normalize_argv(raw))
