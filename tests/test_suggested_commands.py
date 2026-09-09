"""Every command HHE suggests must be a command HHE accepts.

Error messages hand the user a copy-pasteable command. A renamed key or a
retired option turns that help into a dead end, and nothing else in the suite
would notice. This module collects the suggestions from the source and checks
each one against the argument parser and the key registry.
"""

from __future__ import annotations

import argparse
import ast
import shlex
from pathlib import Path

import pytest

from ha_history_exporter.cli.parser import build_parser
from ha_history_exporter.settings import schema

PACKAGE = Path(__file__).parents[1] / "ha_history_exporter"

#: How a suggestion may name the program.
PROGRAMS = ("hhe", "ha-history-exporter", "python -m ha_history_exporter")

#: Placeholders stand for a value the user supplies.
PLACEHOLDER = ("<", "%", "$")


def suggested_commands() -> list[tuple[str, str]]:
    """Every string constant in the package that reads like an HHE command."""
    found: list[tuple[str, str]] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            text = node.value.strip()
            if not any(
                text == program or text.startswith(f"{program} ")
                for program in PROGRAMS
            ):
                continue
            found.append((f"{path.name}:{node.lineno}", text))
    return found


def descend(tokens: list[str]) -> tuple[list[str], set[str], list[str]]:
    """Walk the parser tree as far as the tokens name subcommands.

    Returns the command path taken, every option those parsers accept, and the
    remaining tokens. argparse exposes no public accessor for its subparsers;
    reading the action list is still better than duplicating the grammar this
    test exists to check.
    """
    parser = build_parser()
    path: list[str] = []
    options: set[str] = set()
    rest = list(tokens)

    while True:
        options.update(
            option for action in parser._actions for option in action.option_strings
        )
        action = next(
            (
                item
                for item in parser._actions
                if isinstance(item, argparse._SubParsersAction)
            ),
            None,
        )
        if action is None or not rest or rest[0] not in action.choices:
            return path, options, rest
        parser = action.choices[rest[0]]
        path.append(rest.pop(0))


def strip_program(tokens: list[str]) -> list[str]:
    for program in PROGRAMS:
        parts = program.split()
        if tokens[: len(parts)] == parts:
            return tokens[len(parts) :]
    raise AssertionError(tokens)


CASES = suggested_commands()


def test_the_package_suggests_commands_at_all():
    """A collector that silently finds nothing would pass every check below."""
    assert len(CASES) >= 30


@pytest.mark.parametrize(("where", "command"), CASES, ids=[c[0] for c in CASES])
def test_a_suggested_command_is_one_hhe_accepts(where, command):
    tokens = strip_program(shlex.split(command))
    if not tokens:
        return  # the bare program name, used in help texts

    path, known_options, rest = descend(tokens)
    assert path, f"{where}: {tokens[0]!r} is not a command"

    for token in rest:
        if token.startswith("--"):
            option = token.split("=", 1)[0]
            assert option in known_options, (
                f"{where}: {' '.join(path)} has no {option}"
            )

    names_a_key = (
        path[:2] in (["config", "get"], ["config", "set"], ["config", "unset"])
        and rest
        and not rest[0].startswith(PLACEHOLDER)
    )
    if names_a_key:
        assert rest[0] in schema.BY_PATH, f"{where}: unknown key {rest[0]!r}"

    if "--format" in rest:
        value = rest[rest.index("--format") + 1]
        if not value.startswith(PLACEHOLDER):
            schema.format_set(value, "--format")
