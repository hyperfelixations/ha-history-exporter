"""Command-line grammar.

HHE grew from a single flag-only command into a tool with several commands.
Both forms stay valid: ``hhe export --date yesterday`` and the historical
``ha-history-exporter --date yesterday`` parse identically, because
:func:`normalize_argv` inserts the implicit ``export`` command.
"""

from __future__ import annotations

import argparse
from typing import List, Sequence

from .. import __version__

COMMANDS = ("export", "init", "config", "doctor")

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

    init = subcommands.add_parser(
        "init",
        help="Guided first-time setup.",
        description="Ask for the few values HHE needs and store them.",
    )
    add_init_arguments(init)

    config = subcommands.add_parser(
        "config",
        help="Inspect and change the configuration.",
        description="Read the effective configuration, or change the user file.",
    )
    add_config_arguments(config)

    doctor = subcommands.add_parser(
        "doctor",
        help="Check the installation and report how to fix problems.",
        description="Run every self-check and print concrete remedies.",
    )
    doctor.add_argument("--config", metavar="FILE",
                        help="Check exactly this configuration file.")
    doctor.add_argument("--offline", action="store_true",
                        help="Skip every check that contacts Home Assistant.")

    return parser


def add_init_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--force", action="store_true",
                   help="Overwrite an existing configuration.")
    p.add_argument("--non-interactive", action="store_true",
                   help="Ask nothing; take every value from the options.")
    p.add_argument("--url", metavar="URL", help="Home Assistant base URL.")
    p.add_argument("--output-dir", metavar="DIR",
                   help="Directory that receives exports, metadata, and logs.")
    p.add_argument("--format", metavar="LIST",
                   help="Comma-separated output formats: jsonl, csv, parquet, none.")
    p.add_argument("--timezone", metavar="TZ", help="IANA time zone name.")


def add_config_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", metavar="FILE",
                   help="Read exactly this configuration file.")
    actions = p.add_subparsers(dest="config_action", metavar="ACTION")
    actions.required = True

    get = actions.add_parser("get", help="Print one effective value.")
    get.add_argument("key", metavar="KEY")

    set_ = actions.add_parser("set", help="Store a value in the user configuration.")
    set_.add_argument("key", metavar="KEY")
    set_.add_argument("value", metavar="VALUE", nargs="?",
                      help="Omit for a secret to be asked for without echo.")

    unset = actions.add_parser("unset", help="Remove a value from the user configuration.")
    unset.add_argument("key", metavar="KEY")

    listing = actions.add_parser("list", help="Print every effective value.")
    listing.add_argument("--origin", action="store_true",
                         help="Also show where each value comes from.")

    actions.add_parser("path", help="Print the files and directories in use.")
    actions.add_parser("edit", help="Open the user configuration in an editor.")


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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse *argv*, accepting both the command form and the legacy flag form."""
    import sys

    raw = sys.argv[1:] if argv is None else argv
    return build_parser().parse_args(normalize_argv(raw))
