"""Reading an HHE command line the way the argument parser reads it."""

from __future__ import annotations

import argparse

from ha_history_exporter.cli.parser import build_parser

#: How a command may name the program.
PROGRAMS = ("hhe", "ha-history-exporter", "python -m ha_history_exporter")

#: Placeholders stand for a value the user supplies.
PLACEHOLDER = ("<", "%", "$")


def descend(tokens: list[str]) -> tuple[list[str], set[str], list[str]]:
    """Walk the parser tree as far as the tokens name subcommands.

    Returns the command path taken, every option those parsers accept, and the
    remaining tokens. argparse exposes no public accessor for its subparsers;
    reading the action list is still better than duplicating the grammar under
    test.
    """
    parser = build_parser()
    path: list[str] = []
    options: set[str] = set()
    rest = list(tokens)

    while True:
        options.update(option for action in parser._actions for option in action.option_strings)
        action = next(
            (item for item in parser._actions if isinstance(item, argparse._SubParsersAction)),
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
