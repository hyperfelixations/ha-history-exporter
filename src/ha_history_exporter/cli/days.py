"""The day options that ``export``, ``convert``, ``verify`` and ``status`` share."""

from __future__ import annotations

import argparse

from ..domain.plan import DaySelection, parse_day
from ..errors import Remedy, UsageError


def add_day_options(parser: argparse.ArgumentParser) -> None:
    days = parser.add_mutually_exclusive_group()
    days.add_argument("--date", metavar="DAY", help="One day: YYYY-MM-DD, yesterday or today.")
    days.add_argument("--start-date", metavar="DAY", help="First day of a range (with --end-date).")
    days.add_argument(
        "--last-days", metavar="N", help="The N most recent complete days, ending yesterday."
    )
    parser.add_argument("--end-date", metavar="DAY", help="Last day of a range, inclusive.")


def day_selection(args: argparse.Namespace, command: str) -> DaySelection | None:
    """The days the options name, or None when they name none."""
    try:
        return _selection(args)
    except ValueError as exc:
        raise UsageError(
            str(exc),
            remedies=(
                Remedy(
                    "Name the most recent complete days, one day, or a range:",
                    f"hhe {command} --last-days 7",
                ),
            ),
        ) from None


def _selection(args: argparse.Namespace) -> DaySelection | None:
    if args.last_days is not None:
        if args.end_date:
            raise ValueError("--end-date belongs to --start-date, not to --last-days.")
        text = args.last_days.strip()
        if not text.isdigit() or int(text) < 1:
            raise ValueError(f"--last-days needs a whole number of at least 1, not '{text}'.")
        return DaySelection(last_days=int(text))
    if args.date:
        if args.end_date:
            raise ValueError("--end-date belongs to --start-date, not to --date.")
        return DaySelection.single(parse_day(args.date))
    if args.start_date or args.end_date:
        if not (args.start_date and args.end_date):
            raise ValueError("A range needs both --start-date and --end-date.")
        return DaySelection(start=parse_day(args.start_date), end=parse_day(args.end_date))
    return None
