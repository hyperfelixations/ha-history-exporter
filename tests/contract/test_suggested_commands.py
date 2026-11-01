"""Every command HHE suggests must be a command HHE accepts.

Error messages hand the user a copy-pasteable command. A renamed key or a
retired option turns that help into a dead end, and nothing else in the suite
would notice. This module collects the suggestions from the source and checks
each one against the argument parser and the key registry.
"""

from __future__ import annotations

import ast
import shlex
from pathlib import Path

import pytest

from ha_history_exporter.settings.registry import BY_PATH, TOKEN_KEY
from ha_history_exporter.settings.validators import Formats, ParseContext
from tests.support.commands import PLACEHOLDER, PROGRAMS, descend, strip_program

PACKAGE = Path(__file__).parents[2] / "src" / "ha_history_exporter"


def suggested_commands() -> list[tuple[str, str]]:
    """Every string constant in the package that reads like an HHE command."""
    found: list[tuple[str, str]] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            text = node.value.strip()
            if not any(text == program or text.startswith(f"{program} ") for program in PROGRAMS):
                continue
            found.append((f"{path.name}:{node.lineno}", text))
    return found


CASES = suggested_commands()


def test_the_package_suggests_commands_at_all() -> None:
    """A collector that silently finds nothing would pass every check below."""
    assert len(CASES) >= 30


@pytest.mark.parametrize(("where", "command"), CASES, ids=[c[0] for c in CASES])
def test_a_suggested_command_is_one_hhe_accepts(where: str, command: str) -> None:
    tokens = strip_program(shlex.split(command))
    if not tokens:
        return  # the bare program name, used in help texts

    path, known_options, rest = descend(tokens)
    assert path, f"{where}: {tokens[0]!r} is not a command"

    for token in rest:
        if token.startswith("--"):
            option = token.split("=", 1)[0]
            assert option in known_options, f"{where}: {' '.join(path)} has no {option}"

    names_a_key = (
        path[:2] in (["config", "get"], ["config", "set"], ["config", "unset"])
        and rest
        and not rest[0].startswith(PLACEHOLDER)
    )
    if names_a_key:
        assert rest[0] in {*BY_PATH, TOKEN_KEY}, f"{where}: unknown key {rest[0]!r}"

    if "--format" in rest:
        value = rest[rest.index("--format") + 1]
        if not value.startswith(PLACEHOLDER):
            Formats().from_text(value, ParseContext(Path.cwd(), {}))
