"""Text HHE prints or logs survives a legacy console code page."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import ha_history_exporter

PACKAGE = Path(ha_history_exporter.__file__).parent


def test_runtime_output_stays_ascii() -> None:
    non_ascii = re.compile(r"[^\x00-\x7f]")
    offenders = []

    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            doc
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
            for doc in [ast.get_docstring(node, clean=False)]
            if doc
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value in docstrings:
                continue
            if non_ascii.search(node.value):
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, f"non-ASCII runtime strings: {offenders}"
