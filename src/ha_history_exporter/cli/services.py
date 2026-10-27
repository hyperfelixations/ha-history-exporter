"""What a command may use of the world outside: injected, so tests drive real commands."""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from ..app.connect import open_source
from ..domain.clock import Clock, SystemClock
from ..settings import ResolvedSettings
from ..source.protocol import HistorySource


def _run_editor(command: list[str]) -> int:
    return subprocess.run(command, check=False).returncode  # noqa: S603 - the user's own editor


def _open_with_default_app(path: Path) -> bool:
    start = getattr(os, "startfile", None)  # Windows only
    if start is None:
        return False
    start(str(path))
    return True


@dataclass(frozen=True)
class Services:
    environ: Mapping[str, str]
    cwd: Path
    stdin: TextIO
    stderr: TextIO
    connect: Callable[[ResolvedSettings], HistorySource]
    clock: Clock
    sleep: Callable[[float], None]
    ask: Callable[[str], str]
    ask_secret: Callable[[str], str]
    run_editor: Callable[[list[str]], int]
    open_with_default_app: Callable[[Path], bool]

    @classmethod
    def system(cls) -> Services:
        return cls(
            environ=os.environ,
            cwd=Path.cwd(),
            stdin=sys.stdin,
            stderr=sys.stderr,
            connect=open_source,
            clock=SystemClock(),
            sleep=time.sleep,
            ask=input,
            ask_secret=getpass.getpass,
            run_editor=_run_editor,
            open_with_default_app=_open_with_default_app,
        )
