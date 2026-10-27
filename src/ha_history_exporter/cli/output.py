"""Everything the command line prints for people.

This module is the only one that writes to standard output and standard error
directly; logging aside, nothing else prints. Colour is used only on a
terminal and only when neither ``NO_COLOR`` nor ``HHE_NO_COLOR`` holds a value
(https://no-color.org).
"""

from __future__ import annotations

import json
import math
import sys
import textwrap
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, TextIO

from ..app.results import (
    ArchiveState,
    ArtifactStatus,
    Check,
    CheckStatus,
    ConversionOutcome,
    ConvertResult,
    DayState,
    EntitySummary,
    ExportResult,
    StatusResult,
    VerifyResult,
)
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


def say_interrupted(stream: TextIO) -> None:
    stream.write("\nInterrupted.\n")


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


def print_json(document: Mapping[str, Any]) -> None:
    """One JSON document on standard output, for programs that read it."""
    say(json.dumps(document, indent=2, ensure_ascii=False))


def error_document(command: str, exc: HHEError) -> dict[str, Any]:
    """The ``--json`` document of a command that ended with an error."""
    return {
        "command": command,
        "exit_code": int(exc.exit_code),
        "error": {
            "code": exc.code.value,
            "summary": exc.summary,
            "details": exc.details,
            "remedies": [
                {"description": remedy.description, "command": remedy.command}
                for remedy in exc.remedies
            ],
        },
    }


def _archive_lines(state: ArchiveState) -> list[str]:
    zone = state.zone
    shown = "not known yet" if zone is None else f"{zone.zone.key} ({_ZONE_ORIGIN[zone.source]})"
    lines = [f"Archive    : {state.output_dir}", f"Time zone  : {shown}"]
    if not state.described and zone is not None:
        lines.append("  The next export or conversion records the time zone in hhe-archive.json.")
    if state.pending_transactions:
        lines.append(
            f"  {state.pending_transactions} interrupted day transaction(s) wait for the next "
            "export or conversion to restore them; their days may look damaged until then."
        )
    if state.running is not None:
        lines.append(f"  An export is running ({state.running}); its current day may change.")
    return lines


_DAY_STATES = (
    DayState.OK,
    DayState.PARTIAL,
    DayState.FAILED,
    DayState.PENDING,
    DayState.INVALID,
    DayState.MISSING,
)


def print_status(result: StatusResult) -> None:
    lines = _archive_lines(result.archive)
    if not result.days:
        say("\n".join([*lines, "", "No day is exported yet."]))
        return
    lines += ["", f"Days {result.days[0].day} to {result.days[-1].day}:", ""]
    for month, days in result.months():
        counts = [
            f"{sum(d.state is state for d in days)} {state.value}"
            for state in _DAY_STATES
            if any(d.state is state for d in days)
        ]
        lines.append(f"{month}  {', '.join(counts)}")
        for state in _DAY_STATES[1:]:
            named = [f"{d.day:%d}" for d in days if d.state is state]
            if named:
                lines.append(f"         {state.value}: {', '.join(named)}")
    say("\n".join(lines))


def print_verify(result: VerifyResult) -> None:
    lines = [*_archive_lines(result.archive), ""]
    for day in result.days:
        if day.invalid is not None:
            lines.append(f"{day.day}  invalid manifest: {day.invalid}")
            continue
        notable = [
            check
            for check in day.artifacts
            if check.status is not ArtifactStatus.OK or check.detail is not None
        ]
        for check in notable:
            detail = f" - {check.detail}" if check.detail else ""
            lines.append(f"{day.day}  {check.format.value:<8}{check.status.value}{detail}")
    counts = ", ".join(
        f"{result.count(status)} {status.value}"
        for status in ArtifactStatus
        if result.count(status)
    )
    checked = "decoded and checksummed" if result.deep else "checksummed"
    lines.append(f"{len(result.days)} day(s) {checked}: {counts or 'no artifacts'}.")
    if result.invalid_days:
        lines.append(f"{len(result.invalid_days)} manifest(s) are invalid.")
    lines.append(
        "Problems found." if result.exit_code else "Every stored artifact matches its manifest."
    )
    say("\n".join(lines))


def print_convert(result: ConvertResult) -> None:
    lines = []
    for day in result.days:
        if day.outcome is ConversionOutcome.CONVERTED:
            added = ", ".join(fmt.value for fmt in day.added)
            source = day.source.value if day.source else "?"
            lines.append(f"{day.day}  converted  {added} from {source}")
        elif day.outcome is not ConversionOutcome.PRESENT:
            lines.append(f"{day.day}  {day.outcome.value:<10} {day.reason or ''}".rstrip())
    summary = ", ".join(
        f"{result.count(outcome)} {outcome.value}"
        for outcome in ConversionOutcome
        if result.count(outcome)
    )
    lines.append(f"{len(result.days)} day(s): {summary or 'nothing to convert'}.")
    if result.log_file is not None:
        lines.append(f"Log file: {result.log_file}")
    say("\n".join(lines))
