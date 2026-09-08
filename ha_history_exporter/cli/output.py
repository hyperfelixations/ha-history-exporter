"""Terminal rendering for HHE.

The presentation layer owns every byte written to the terminal for human
consumption. It never touches configuration, files, or the network.

Colour is used only when the stream is a TTY and neither NO_COLOR nor
HHE_NO_COLOR is set (https://no-color.org).
"""

from __future__ import annotations

import os
import sys
import textwrap
from typing import TextIO

from ..errors import CONTEXT_LABELS, HHEError

WIDTH = 78
INDENT = "  "
COMMAND_INDENT = "      "

_RED = "\033[31m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def use_color(stream: TextIO) -> bool:
    """True when ANSI escapes are appropriate for *stream*."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("HHE_NO_COLOR") is not None:
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _wrap(text: str, indent: str) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        lines.extend(
            textwrap.wrap(
                paragraph,
                width=WIDTH,
                initial_indent=indent,
                subsequent_indent=indent,
            )
        )
    return lines


def render_error(exc: HHEError, *, stream: TextIO | None = None) -> None:
    """Write a uniform, actionable error block for *exc*."""
    stream = sys.stderr if stream is None else stream
    colored = use_color(stream)
    label = f"{_BOLD}{_RED}error:{_RESET}" if colored else "error:"

    lines: list[str] = ["", f"{label} {exc.summary}"]

    if exc.details:
        lines.append("")
        lines.extend(_wrap(exc.details, INDENT))

    for remedy in exc.remedies:
        lines.append("")
        lines.extend(_wrap(remedy.description, INDENT))
        if remedy.command:
            lines.append(f"{COMMAND_INDENT}{remedy.command}")

    context_lines = [
        f"{INDENT}{CONTEXT_LABELS[key]}: {value}"
        for key, value in exc.context.items()
        if key in CONTEXT_LABELS
    ]
    if context_lines:
        lines.append("")
        lines.extend(context_lines)

    lines.append("")
    stream.write("\n".join(lines) + "\n")
