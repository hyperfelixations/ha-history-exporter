"""Everything the command line prints for people.

This module is the only one that writes to standard output and standard error
directly; logging aside, nothing else prints. Colour is used only on a
terminal and only when neither ``NO_COLOR`` nor ``HHE_NO_COLOR`` holds a value
(https://no-color.org).
"""

from __future__ import annotations

import math
import sys
import textwrap
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from ..app.results import EntitySummary, ExportResult
from ..archive.descriptor import ArchiveZone, ZoneSource
from ..domain.plan import DayAction, ExportPlan
from ..errors import CONTEXT_LABELS, HHEError

WIDTH = 78
INDENT = "  "
COMMAND_INDENT = "      "

_RED = "\033[31m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def say(text: str = "", *, stream: TextIO | None = None) -> None:
    print(text, file=stream or sys.stdout)


def use_color(stream: TextIO, environ: Mapping[str, str]) -> bool:
    if environ.get("NO_COLOR") or environ.get("HHE_NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _wrap(text: str, indent: str) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        lines.extend(
            textwrap.wrap(paragraph, width=WIDTH, initial_indent=indent, subsequent_indent=indent)
        )
    return lines


def render_error(
    exc: HHEError, *, stream: TextIO | None = None, environ: Mapping[str, str] | None = None
) -> None:
    """A uniform, actionable error block."""
    stream = stream or sys.stderr
    label = "error:"
    if use_color(stream, environ or {}):
        label = f"{_BOLD}{_RED}error:{_RESET}"
    lines = ["", f"{label} {exc.summary}"]
    if exc.details:
        lines += ["", *_wrap(exc.details, INDENT)]
    for remedy in exc.remedies:
        lines += ["", *_wrap(remedy.description, INDENT)]
        if remedy.command:
            lines.append(f"{COMMAND_INDENT}{remedy.command}")
    context = [
        f"{INDENT}{CONTEXT_LABELS[key]}: {value}"
        for key, value in exc.context.items()
        if key in CONTEXT_LABELS
    ]
    if context:
        lines += ["", *context]
    lines.append("")
    stream.write("\n".join(lines) + "\n")


_ZONE_ORIGIN = {
    ZoneSource.RECORDED: "recorded in hhe-archive.json",
    ZoneSource.ADOPTED: "from the existing manifests",
    ZoneSource.CONFIGURED: "from export.timezone",
    ZoneSource.HOME_ASSISTANT: "from Home Assistant",
}


def print_plan(
    plan: ExportPlan,
    entities: EntitySummary,
    zone: ArchiveZone,
    *,
    batch_size: int,
    output_dir: Path,
    config_source: str,
    log_file: Path | None,
) -> None:
    """The plan: where the run writes and what configured it come first."""
    requests = math.ceil(len(entities.requested) / batch_size) if entities.requested else 0
    lines = ["", "=" * 60, "  Home Assistant History Exporter", "=" * 60]
    for label, value in (
        ("Output", str(output_dir)),
        ("Configuration", config_source),
        ("Log file", None if log_file is None else str(log_file)),
        ("Time zone", f"{zone.zone.key} ({_ZONE_ORIGIN[zone.source]})"),
    ):
        if value is not None:
            lines.append(f"  {label:<16}: {value}")
    lines += [
        "",
        f"  Requested range : {plan.requested_start} -> {plan.requested_end}",
        f"  Current local   : {plan.today}",
        f"  Latest complete : {plan.latest_complete}",
        "",
        f"  Entities        : {len(entities.requested)} requested of {entities.candidates}",
        f"    excluded    : {entities.excluded}",
        f"    unknown     : {entities.unknown}",
        f"    unavailable : {entities.unavailable}",
        f"  Batch size      : {batch_size}",
        f"  Requests / day  : ~{requests}",
        "",
    ]
    partial = set(plan.partial_days)
    if plan.days_to_export:
        lines.append(f"  Will export ({len(plan.days_to_export)}):")
        for day in plan.days_to_export:
            marker = "  (partial - today is not over yet)" if day in partial else ""
            lines.append(f"    {day}{marker}")
    else:
        lines.append("  Nothing to export.")
    for action, heading in (
        (DayAction.SKIP_EXISTING, "Will skip - already exported"),
        (DayAction.SKIP_INCOMPLETE, "Will skip - not yet complete"),
    ):
        days = plan.days(action)
        if days:
            lines += ["", f"  {heading} ({len(days)}):", *(f"    {day}" for day in days)]
    lines += ["=" * 60, ""]
    say("\n".join(lines))


def print_export_result(result: ExportResult) -> None:
    if result.dry_run:
        say("Dry run: nothing was fetched or written.")
        return
    for day in result.days:
        line = f"{day.day}  {day.status.value:<8} {day.state_object_count} state objects"
        if day.error:
            line += f"  - {day.error}"
        say(line)
    if result.snapshot_file is not None:
        say(f"Entity snapshot: {result.snapshot_file}")
    if result.log_file is not None:
        say(f"Log file: {result.log_file}")


class CheckStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    status: CheckStatus
    label: str
    detail: str = ""
    remedy: str = ""


_MARKS = {CheckStatus.OK: "[ ok ]", CheckStatus.WARN: "[warn]", CheckStatus.FAIL: "[fail]"}


def print_checks(checks: Iterable[Check]) -> None:
    items = list(checks)
    width = max(len(item.label) for item in items)
    for item in items:
        line = f"{_MARKS[item.status]} {item.label:<{width}}"
        if item.detail:
            line += f"  {item.detail}"
        say(line)
        if item.remedy:
            say(f"       {' ' * width}  -> {item.remedy}")
    failures = sum(item.status is CheckStatus.FAIL for item in items)
    warnings = sum(item.status is CheckStatus.WARN for item in items)
    say()
    if failures:
        say(f"{failures} problem(s) need attention, {warnings} warning(s).")
    elif warnings:
        say(f"No problems, {warnings} warning(s).")
    else:
        say("Everything checks out.")
