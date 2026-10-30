"""actionlint with the pinned shellcheck and zizmor accept every workflow.

The tools come from requirements-dev.txt; a missing tool fails instead of
skipping, so a local run checks exactly what CI checks.
"""

from __future__ import annotations

import shutil
import subprocess
import sysconfig
from pathlib import Path

import pytest

from tests.repo import tooling

pytestmark = pytest.mark.repo

#: A step whose negated command never stops the script; shellcheck reports it as SC2251.
NEGATED_STEP = """\
on: push
jobs:
  sample:
    runs-on: ubuntu-latest
    steps:
      - run: |
          set -e
          ! true
          echo "reached"
"""


def _tool(name: str) -> Path:
    found = shutil.which(name, path=sysconfig.get_path("scripts"))
    assert found is not None, f"{name} is missing; install requirements-dev.txt"
    return Path(found)


def _run(*arguments: str | Path, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(argument) for argument in arguments],
        cwd=tooling.ROOT,
        input=stdin,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )


def _actionlint(*arguments: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """actionlint with the pinned shellcheck and without pyflakes from the PATH."""
    return _run(
        _tool("actionlint"),
        "-no-color",
        "-shellcheck",
        _tool("shellcheck"),
        "-pyflakes=",
        *arguments,
        stdin=stdin,
    )


def test_the_pinned_shellcheck_is_active() -> None:
    """actionlint skips a shellcheck it cannot run without saying so."""
    result = _actionlint("-", stdin=NEGATED_STEP)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "SC2251" in result.stdout


def test_actionlint_accepts_every_workflow() -> None:
    result = _actionlint()
    assert result.returncode == 0, result.stdout + result.stderr


def test_zizmor_finds_nothing_in_the_workflows() -> None:
    result = _run(_tool("zizmor"), "--offline", "--no-progress", "--color=never", ".github")
    assert result.returncode == 0, result.stdout + result.stderr
