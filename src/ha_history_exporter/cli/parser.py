"""The command-line grammar.

Every invocation names its command: ``export``, ``convert``, ``verify``,
``status``, ``init``, ``config`` or ``doctor``. The program name is passed in rather than taken from
``sys.argv[0]``, so help and usage show what the user typed. The export
options that override a configuration key come from the key registry and
carry the key's documentation and default.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import __version__
from ..settings.registry import BY_OPTION, Key
from ..settings.validators import Formats, Integer, OptionalValue, PathValue, Seconds, Zone
from .days import add_day_options

EPILOG = """\
Examples:
  {prog} export                          the most recent complete day
  {prog} export --last-days 7            the seven most recent complete days
  {prog} export --date 2026-06-15 --force
  {prog} export --start-date 2026-06-01 --end-date yesterday
  {prog} export --last-days 30 --format parquet
  {prog} verify                          check every stored day
  {prog} convert --format parquet        derive Parquet from the stored days

The Home Assistant URL and access token come from the configuration, or from
the HHE_URL and HHE_TOKEN environment variables.
"""


def build_parser(prog: str = "hhe") -> argparse.ArgumentParser:
    epilog = EPILOG.format(prog=prog)
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Export Home Assistant history through the REST API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    parser.add_argument("--version", "-V", action="version", version=f"{prog} {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    export = commands.add_parser(
        "export",
        help="Export history for one day or a range of days.",
        description=(
            "Export Home Assistant history for complete local days. Without a day "
            "selection, the most recent complete day is exported."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    _add_export_arguments(export)

    convert = commands.add_parser(
        "convert",
        help="Derive formats from the stored days, without Home Assistant.",
        description=(
            "Add output formats to days that are already exported, from an artifact "
            "they have. Without a day selection, every day of the archive is converted."
        ),
    )
    _add_archive_arguments(convert)
    convert.add_argument(
        "--format",
        metavar="LIST",
        required=True,
        help="The formats to add: any of jsonl, csv, parquet.",
    )
    _add_log_level(convert)

    verify = commands.add_parser(
        "verify",
        help="Check the stored days against their manifests, read-only.",
        description=(
            "Compare every artifact with its manifest and report files no manifest "
            "declares. Without a day selection, every day of the archive is checked."
        ),
    )
    _add_archive_arguments(verify)
    verify.add_argument(
        "--deep",
        action="store_true",
        help="Also decode every artifact and compare its records with the manifest.",
    )

    status = commands.add_parser(
        "status",
        help="Show which days the archive holds, month by month.",
        description=(
            "List exported, partial, failed, invalid and missing days. Without a day "
            "selection, the range runs from the first to the last exported day."
        ),
    )
    _add_archive_arguments(status)

    init = commands.add_parser(
        "init",
        help="Guided first-time setup.",
        description="Ask for the few values HHE needs, check them, and store them.",
    )
    _add_init_arguments(init)

    config = commands.add_parser(
        "config",
        help="Inspect and change the configuration.",
        description="Read the effective configuration, or change the user configuration file.",
    )
    _add_config_arguments(config)

    doctor = commands.add_parser(
        "doctor",
        help="Check the installation and the archive, read-only.",
        description="Run every self-check and print concrete remedies; changes nothing.",
    )
    _add_config_option(doctor, "Check exactly this configuration file.")
    doctor.add_argument(
        "--offline", action="store_true", help="Skip every check that contacts Home Assistant."
    )
    _add_json(doctor)
    return parser


def _add_config_option(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument("--config", metavar="FILE", type=Path, help=help_text)


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true", help="Print the result as one JSON document instead."
    )


def _add_log_level(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log level (default: %(default)s).",
    )


def _add_archive_arguments(parser: argparse.ArgumentParser) -> None:
    """The archive and the days a command that works on stored days reads."""
    _add_config_option(parser, "Read exactly this file instead of the user configuration.")
    key = BY_OPTION["--output-dir"]
    parser.add_argument(
        "--output-dir", dest=_dest("--output-dir"), metavar=_metavar(key), help=_option_help(key)
    )
    add_day_options(parser)
    _add_json(parser)


_METAVARS = {PathValue: "DIR", Zone: "ZONE", Formats: "LIST", Integer: "N", Seconds: "S"}


def _metavar(key: Key) -> str:
    kind = key.setting.kind
    if isinstance(kind, OptionalValue):
        kind = kind.inner
    return _METAVARS.get(type(kind), "VALUE")


def _option_help(key: Key) -> str:
    default = key.default()
    shown = "not set" if default is None else key.setting.kind.display(default)
    return f"{key.doc} Overrides {key.path} (default: {shown})."


def _add_export_arguments(parser: argparse.ArgumentParser) -> None:
    _add_config_option(parser, "Read exactly this file instead of the user configuration.")
    add_day_options(parser)
    parser.add_argument(
        "--dry-run", action="store_true", help="Show the plan; fetch and write nothing."
    )
    parser.add_argument(
        "--force", action="store_true", help="Export days again that have a successful manifest."
    )
    for option, key in BY_OPTION.items():
        parser.add_argument(
            option, dest=_dest(option), metavar=_metavar(key), help=_option_help(key)
        )
    _add_log_level(parser)
    _add_json(parser)


def _dest(option: str) -> str:
    return "override_" + option.removeprefix("--").replace("-", "_")


def overrides(args: argparse.Namespace) -> dict[str, str]:
    """The configuration keys the export options override, with their raw text."""
    found = {}
    for option, key in BY_OPTION.items():
        value = getattr(args, _dest(option), None)
        if value is not None:
            found[key.path] = value
    return found


def _add_init_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--force", action="store_true", help="Replace an existing configuration entirely."
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Ask nothing: the token comes from HHE_TOKEN or standard input.",
    )
    parser.add_argument("--url", metavar="URL", help="Home Assistant base URL.")
    parser.add_argument("--output-dir", metavar="DIR", help="Directory of the archive.")
    parser.add_argument(
        "--format", metavar="LIST", help="Output formats: jsonl, csv, parquet, or none."
    )
    parser.add_argument(
        "--timezone", metavar="ZONE", help="IANA time zone of the days; default: Home Assistant's."
    )
    parser.add_argument(
        "--purge-keep-days", metavar="N", help="Recorder retention of Home Assistant in days."
    )


def _add_config_arguments(parser: argparse.ArgumentParser) -> None:
    _add_config_option(parser, "Read exactly this configuration file.")
    actions = parser.add_subparsers(dest="action", metavar="ACTION", required=True)

    get = actions.add_parser("get", help="Print one effective value.")
    get.add_argument("key", metavar="KEY")

    set_ = actions.add_parser("set", help="Store a value in the user configuration.")
    set_.add_argument("key", metavar="KEY")
    set_.add_argument(
        "value", metavar="VALUE", nargs="?", help="Omit for the token; it is asked for hidden."
    )
    set_.add_argument("--stdin", action="store_true", help="Read the token from standard input.")

    unset = actions.add_parser("unset", help="Remove a value from the user configuration.")
    unset.add_argument("key", metavar="KEY")

    listing = actions.add_parser("list", help="Print every effective value.")
    listing.add_argument(
        "--origin", action="store_true", help="Also show where each value comes from."
    )

    actions.add_parser("path", help="Print the files and directories in use.")
    actions.add_parser("edit", help="Open the user configuration in an editor, then check it.")
