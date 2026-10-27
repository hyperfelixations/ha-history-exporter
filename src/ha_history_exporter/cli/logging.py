"""Logging of one command: standard error at once, the run's log file under the lock.

Records logged before the log file exists are kept in a bounded buffer and
written into the file when it is attached; when the buffer overflows, the
oldest records go and the file says how many. Tracebacks reach the log file
only, never the terminal. See internal dev doc § "Errors and exit codes".
"""

from __future__ import annotations

import logging
from collections import deque
from pathlib import Path
from typing import TextIO

from .. import fsio

FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
BUFFER_CAPACITY = 2000

_MARK = "_hhe_handler"


class _WithoutTraceback(logging.Formatter):
    """The message of a record without its traceback or stack."""

    def format(self, record: logging.LogRecord) -> str:
        saved = record.exc_info, record.exc_text, record.stack_info
        record.exc_info, record.exc_text, record.stack_info = None, None, None
        try:
            return super().format(record)
        finally:
            record.exc_info, record.exc_text, record.stack_info = saved


class _Buffer(logging.Handler):
    def __init__(self, capacity: int) -> None:
        super().__init__()
        self.records: deque[logging.LogRecord] = deque(maxlen=capacity)
        self.dropped = 0

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.records) == self.records.maxlen:
            self.dropped += 1
        self.records.append(record)


#: ``extra`` for a record that belongs in the log file but not on the terminal,
#: because the terminal shows it otherwise, as an error block.
FILE_ONLY = {"hhe_file_only": True}


def _not_file_only(record: logging.LogRecord) -> bool:
    return not getattr(record, "hhe_file_only", False)


class CommandLogging:
    """The handlers of one command on the root logger; replaces those of an earlier one."""

    def __init__(self, level: int, stream: TextIO, *, buffered: bool) -> None:
        self.level = level
        self.log_file: Path | None = None
        root = logging.getLogger()
        for handler in list(root.handlers):
            if getattr(handler, _MARK, False):
                root.removeHandler(handler)
                handler.close()
        root.setLevel(level)
        terminal = logging.StreamHandler(stream)
        terminal.setFormatter(_WithoutTraceback(FORMAT, DATE_FORMAT))
        terminal.addFilter(_not_file_only)
        self._add(terminal)
        self._buffer: _Buffer | None = None
        if buffered:
            self._buffer = _Buffer(BUFFER_CAPACITY)
            self._add(self._buffer)
        quiet = logging.DEBUG if level <= logging.DEBUG else logging.WARNING
        for name in ("urllib3", "requests"):
            logging.getLogger(name).setLevel(quiet)

    def _add(self, handler: logging.Handler) -> None:
        setattr(handler, _MARK, True)
        handler.setLevel(self.level)
        logging.getLogger().addHandler(handler)

    def attach_file(self, path: Path) -> None:
        """Write the run's log to *path* from now on, after the buffered records."""
        fsio.ensure_dir(path.parent)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter(FORMAT, DATE_FORMAT))
        buffer, self._buffer = self._buffer, None
        if buffer is not None:
            logging.getLogger().removeHandler(buffer)
            if buffer.dropped:
                notice = logging.makeLogRecord(
                    {
                        "name": __name__,
                        "levelno": logging.WARNING,
                        "levelname": "WARNING",
                        "msg": f"{buffer.dropped} earlier log records were dropped.",
                    }
                )
                handler.handle(notice)
            for record in buffer.records:
                handler.handle(record)
        self._add(handler)
        self.log_file = path

    def close(self) -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            if getattr(handler, _MARK, False):
                root.removeHandler(handler)
                handler.close()
