"""The contract every output format implements."""

from __future__ import annotations

import hashlib
import importlib.util
import io
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Protocol, Self

from ..domain.formats import Format
from ..domain.records import HistoryRecord


class RecordWriter(Protocol):
    """Streams records into one artifact; :meth:`close` leaves it durable."""

    def write(self, record: HistoryRecord) -> None: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


#: Reads one artifact from a binary stream; the string names it in errors.
type RecordReader = Callable[[BinaryIO, str], Iterator[HistoryRecord]]


@dataclass(frozen=True)
class FormatSpec:
    """Everything HHE knows about one output format."""

    format: Format
    open_writer: Callable[[Path], RecordWriter]
    read: RecordReader
    #: True when :attr:`read` consumes the stream once from start to end, so
    #: the bytes can be hashed while they are decoded.
    sequential: bool
    #: Import name of an optional package the format needs, if any.
    requirement: str | None = None

    def requirement_met(self) -> bool:
        return self.requirement is None or importlib.util.find_spec(self.requirement) is not None


class HashingReader(io.RawIOBase):
    """A raw stream that hashes and counts every byte read through it."""

    def __init__(self, raw: BinaryIO) -> None:
        super().__init__()
        self._raw = raw
        self._sha256 = hashlib.sha256()
        self.size = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:  # type: ignore[override]
        view = memoryview(buffer)
        data = self._raw.read(len(view))
        count = len(data)
        view[:count] = data
        self._sha256.update(data)
        self.size += count
        return count

    def hexdigest(self) -> str:
        return self._sha256.hexdigest()


def sha256_of(stream: BinaryIO, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    """SHA-256 and byte count of *stream* from its current position to the end."""
    digest = hashlib.sha256()
    size = 0
    for block in iter(lambda: stream.read(chunk_size), b""):
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size
