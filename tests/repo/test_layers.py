"""The import contracts of ``pyproject.toml`` hold: layers import downwards only."""

from __future__ import annotations

import subprocess
import sys

import pytest

from tests.repo import tooling

pytestmark = pytest.mark.repo

#: import-linter configures logging globally, so it runs in a process of its own.
LINT = "from importlinter.cli import lint_imports; raise SystemExit(lint_imports(no_cache=True))"


def test_the_import_contracts_are_kept() -> None:
    result = subprocess.run(
        [sys.executable, "-c", LINT],
        cwd=tooling.ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 broken" in result.stdout
