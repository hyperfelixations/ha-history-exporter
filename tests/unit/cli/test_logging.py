from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest

from ha_history_exporter.cli import logging as command_logging
from ha_history_exporter.cli.logging import FILE_ONLY, CommandLogging

logger = logging.getLogger("hhe.test")


def _failure() -> BaseException:
    try:
        raise ZeroDivisionError("synthetic defect")
    except ZeroDivisionError as exc:
        return exc


def test_the_terminal_never_shows_a_traceback_and_the_file_always_does(tmp_path: Path) -> None:
    terminal = io.StringIO()
    logs = CommandLogging(logging.INFO, terminal, buffered=True)
    try:
        logger.error("Day failed.", exc_info=_failure())
        logs.attach_file(tmp_path / "logs" / "run.log")
        logger.error("Again.", exc_info=_failure())
    finally:
        logs.close()
    assert "Traceback" not in terminal.getvalue()
    assert terminal.getvalue().count("failed.") == 1
    text = (tmp_path / "logs" / "run.log").read_text(encoding="utf-8")
    assert text.count("Traceback") == 2
    assert text.index("Day failed.") < text.index("Again.")


def test_file_only_records_skip_the_terminal(tmp_path: Path) -> None:
    terminal = io.StringIO()
    logs = CommandLogging(logging.INFO, terminal, buffered=False)
    try:
        logs.attach_file(tmp_path / "run.log")
        logger.error("Shown as an error block instead.", extra=FILE_ONLY)
    finally:
        logs.close()
    assert terminal.getvalue() == ""
    assert "error block" in (tmp_path / "run.log").read_text(encoding="utf-8")


def test_an_overflowing_buffer_says_how_many_records_it_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command_logging, "BUFFER_CAPACITY", 3)
    logs = CommandLogging(logging.INFO, io.StringIO(), buffered=True)
    try:
        for number in range(5):
            logger.info("record %d", number)
        logs.attach_file(tmp_path / "run.log")
    finally:
        logs.close()
    lines = (tmp_path / "run.log").read_text(encoding="utf-8").splitlines()
    assert "2 earlier log records were dropped." in lines[0]
    assert [line.rsplit(" ", 1)[-1] for line in lines[1:]] == ["2", "3", "4"]


def test_a_second_command_replaces_the_handlers_of_the_first() -> None:
    root = logging.getLogger()
    before = len(root.handlers)
    first = CommandLogging(logging.INFO, io.StringIO(), buffered=True)
    second = CommandLogging(logging.INFO, io.StringIO(), buffered=True)
    assert len(root.handlers) == before + 2
    second.close()
    first.close()
    assert len(root.handlers) == before
