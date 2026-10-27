from __future__ import annotations

import argparse

import pytest

from ha_history_exporter import __version__
from ha_history_exporter.cli.parser import build_parser, overrides
from ha_history_exporter.settings.registry import BY_OPTION


@pytest.mark.parametrize("prog", ["hhe", "ha-history-exporter", "python -m ha_history_exporter"])
def test_usage_and_version_name_the_program_that_was_run(
    prog: str, capsys: pytest.CaptureFixture[str]
) -> None:
    parser = build_parser(prog)
    assert parser.format_usage().startswith(f"usage: {prog} ")
    with pytest.raises(SystemExit) as exit_:
        parser.parse_args(["--version"])
    assert exit_.value.code == 0
    assert capsys.readouterr().out.strip() == f"{prog} {__version__}"


def test_a_command_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        build_parser().parse_args([])
    assert exit_.value.code == 2
    assert "COMMAND" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["export", "init", "config", "doctor"])
def test_every_command_is_reachable(command: str) -> None:
    extra = {"config": ["list"]}.get(command, [])
    assert build_parser().parse_args([command, *extra]).command == command


def test_export_options_override_the_keys_they_name() -> None:
    argv = ["export"]
    for option in BY_OPTION:
        argv += [option, f"value-of-{option}"]
    args = build_parser().parse_args(argv)
    assert overrides(args) == {key.path: f"value-of-{option}" for option, key in BY_OPTION.items()}
    assert overrides(build_parser().parse_args(["export"])) == {}


def test_export_help_shows_the_documented_defaults() -> None:
    export = _subparser("export")
    help_text = " ".join(export.format_help().split())
    assert "Overrides requests.batch_size (default: 15)" in help_text
    assert "Overrides export.timezone (default: not set)" in help_text


@pytest.mark.parametrize(
    "argv",
    [
        ["export", "--date", "yesterday", "--last-days", "3"],
        ["export", "--start-date", "2026-06-01", "--date", "yesterday"],
        ["export", "--batch_size", "3"],
        ["export", "--formats", "jsonl"],
    ],
)
def test_contradictions_and_second_spellings_are_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        build_parser().parse_args(argv)
    assert exit_.value.code == 2


def _subparser(name: str) -> argparse.ArgumentParser:
    parser = build_parser()
    action = next(item for item in parser._actions if isinstance(item, argparse._SubParsersAction))
    sub: argparse.ArgumentParser = action.choices[name]
    return sub
