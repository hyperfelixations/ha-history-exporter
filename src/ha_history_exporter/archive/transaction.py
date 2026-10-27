"""Crash-recoverable publication of one day generation.

A generation is a day's artifacts plus its manifest. Publishing it moves
through journal phases; each phase is written only after everything it claims
is durable on disk:

``prepared``   artifacts and manifest are staged on the output file system
``backed_up``  every file the new generation replaces or retires is moved aside
``promoted``   the new artifacts are in place
``committed``  the manifest is in place, last

A transaction interrupted before ``committed`` is rolled back to the previous
generation by :func:`recover`, byte for byte; a committed one only loses its
backups. ``remove`` entries retire files that the previous manifest declared
but the new generation no longer contains. Paths are checked lexically, so a
linked subdirectory on another disk stays usable. See internal dev doc
§ "Day transaction".
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from .. import fsio
from ..errors import ExportError
from .layout import ArchiveLayout

JOURNAL_VERSION = 2


class Phase(StrEnum):
    PREPARED = "prepared"
    BACKED_UP = "backed_up"
    PROMOTED = "promoted"
    COMMITTED = "committed"


class Action(StrEnum):
    REPLACE = "replace"
    REMOVE = "remove"


@dataclass(frozen=True)
class Entry:
    """One file of the transaction; paths are relative to the output root."""

    action: Action
    final: str
    backup: str
    had_original: bool
    staged: str | None = None


@dataclass(frozen=True)
class Journal:
    run_id: str
    day: str
    phase: Phase
    entries: tuple[Entry, ...]

    def encode(self) -> bytes:
        data = {
            "version": JOURNAL_VERSION,
            "run_id": self.run_id,
            "day": self.day,
            "phase": self.phase.value,
            "entries": [
                {
                    "action": entry.action.value,
                    "final": entry.final,
                    "backup": entry.backup,
                    "had_original": entry.had_original,
                    "staged": entry.staged,
                }
                for entry in self.entries
            ],
        }
        return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _unreadable() -> ExportError:
    return ExportError("An export transaction journal cannot be read safely.")


def _decode(raw: bytes) -> Journal:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _unreadable() from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("entries"), list)
        or not data["entries"]
    ):
        raise _unreadable()
    version = data.get("version")
    if version == 1:
        return _decode_v1(data)
    if version != JOURNAL_VERSION or set(data) != {"version", "run_id", "day", "phase", "entries"}:
        raise _unreadable()
    try:
        phase = Phase(data["phase"])
        entries = tuple(_entry(raw_entry) for raw_entry in data["entries"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _unreadable() from exc
    return Journal(str(data["run_id"]), str(data["day"]), phase, entries)


def _entry(raw: Any) -> Entry:
    if not isinstance(raw, dict) or set(raw) != {
        "action",
        "final",
        "backup",
        "had_original",
        "staged",
    }:
        raise ValueError("malformed journal entry")
    if not isinstance(raw["had_original"], bool):
        raise TypeError("malformed journal entry")
    return Entry(
        action=Action(raw["action"]),
        final=raw["final"],
        backup=raw["backup"],
        had_original=raw["had_original"],
        staged=raw["staged"],
    )


def _decode_v1(data: dict[str, Any]) -> Journal:
    """Journal version 1: replace entries only, manifest last."""
    phases = {
        "prepared": Phase.PREPARED,
        "backed_up": Phase.BACKED_UP,
        "artifacts_promoted": Phase.PROMOTED,
        "committed": Phase.COMMITTED,
    }
    if set(data) != {"version", "run_id", "day", "phase", "entries"} or data["phase"] not in phases:
        raise _unreadable()
    entries = []
    for raw in data["entries"]:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"staged", "final", "backup", "had_original"}
            or not isinstance(raw["had_original"], bool)
        ):
            raise _unreadable()
        entries.append(
            Entry(Action.REPLACE, raw["final"], raw["backup"], raw["had_original"], raw["staged"])
        )
    return Journal(str(data["run_id"]), str(data["day"]), phases[data["phase"]], tuple(entries))


class _Paths:
    """Lexically contained conversion between output-relative and absolute paths."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def relative(self, path: Path) -> str:
        if not fsio.is_within(self.root, path):
            raise ExportError("A transaction path escaped the output directory.")
        relative = os.path.relpath(os.path.normpath(path.absolute()), self.root.absolute())
        return PurePosixPath(Path(relative).as_posix()).as_posix()

    def absolute(self, value: object) -> Path:
        if not isinstance(value, str) or not value or PurePosixPath(value).is_absolute():
            raise ExportError("An export transaction journal contains an invalid path.")
        candidate = self.root / Path(value)
        if not fsio.is_within(self.root, candidate) or Path(value).is_absolute():
            raise ExportError("An export transaction journal contains an invalid path.")
        return candidate


class DayTransaction:
    """Publish prepared artifacts and their manifest as one generation."""

    def __init__(
        self,
        layout: ArchiveLayout,
        run_id: str,
        day: date,
        *,
        artifacts: Sequence[tuple[Path, Path]],
        manifest: tuple[Path, Path],
        retire: Sequence[Path] = (),
        retries: int = 0,
        retry_sleep: float = 0.0,
    ) -> None:
        self.layout = layout
        self.run_id = run_id
        self.day = day.isoformat()
        self.artifacts = list(artifacts)
        self.manifest = manifest
        self.retire = list(retire)
        self.retries = retries
        self.retry_sleep = retry_sleep
        self._paths = _Paths(layout.root)
        self.journal_path = layout.journal_root / f"{run_id}-{self.day}.json"

    def _replace(self, source: Path, target: Path) -> None:
        fsio.ensure_dir(target.parent)
        fsio.replace(source, target, retries=self.retries, retry_sleep=self.retry_sleep)

    def _stage(self, source: Path, index: int) -> Path:
        staging = self.layout.staging_dir(self.run_id)
        fsio.ensure_dir(staging)
        staged = staging / f"{index:02d}-{source.name}"
        if fsio.same_device(source.parent, staging):
            self._replace(source, staged)
        else:
            shutil.copy2(source, staged)
            fsio.fsync_file(staged)
            source.unlink()
        return staged

    def _write(self, journal: Journal) -> Journal:
        fsio.atomic_write(self.journal_path, journal.encode())
        return journal

    def commit(self) -> None:
        """Publish the generation; any failure restores the previous one."""
        pairs = [*self.artifacts, self.manifest]
        for _, final in pairs:
            self._paths.relative(final)
        backups = self.layout.journal_root / "backups" / self.run_id / self.day
        entries = []
        for index, (source, final) in enumerate(pairs):
            staged = self._stage(source, index)
            final_relative = self._paths.relative(final)
            entries.append(
                Entry(
                    action=Action.REPLACE,
                    final=final_relative,
                    backup=self._paths.relative(backups / final_relative),
                    had_original=final.exists(),
                    staged=self._paths.relative(staged),
                )
            )
        fsio.fsync_dir(self.layout.staging_dir(self.run_id))
        for path in self.retire:
            if path.exists():
                final_relative = self._paths.relative(path)
                entries.insert(
                    len(entries) - 1,
                    Entry(
                        action=Action.REMOVE,
                        final=final_relative,
                        backup=self._paths.relative(backups / final_relative),
                        had_original=True,
                    ),
                )

        journal = self._write(Journal(self.run_id, self.day, Phase.PREPARED, tuple(entries)))
        try:
            journal = self._advance(journal)
        except BaseException:
            _rollback(
                self._paths, self.layout, self.journal_path, journal, self.retries, self.retry_sleep
            )
            raise
        with suppress(OSError):
            _cleanup(self._paths, self.layout, self.journal_path, journal)

    def _advance(self, journal: Journal) -> Journal:
        paths = self._paths
        touched: set[Path] = set()
        for entry in journal.entries:
            if entry.had_original:
                backup = paths.absolute(entry.backup)
                self._replace(paths.absolute(entry.final), backup)
                touched.update({backup.parent, paths.absolute(entry.final).parent})
        _fsync_dirs(touched)
        journal = self._write(replace(journal, phase=Phase.BACKED_UP))

        *artifacts, manifest = journal.entries
        touched = set()
        for entry in artifacts:
            if entry.action is Action.REPLACE:
                final = paths.absolute(entry.final)
                self._replace(paths.absolute(entry.staged), final)
                touched.add(final.parent)
        _fsync_dirs(touched)
        journal = self._write(replace(journal, phase=Phase.PROMOTED))

        final = paths.absolute(manifest.final)
        self._replace(paths.absolute(manifest.staged), final)
        fsio.fsync_dir(final.parent)
        return self._write(replace(journal, phase=Phase.COMMITTED))


def _fsync_dirs(directories: set[Path]) -> None:
    for directory in sorted(directories):
        fsio.fsync_dir(directory)


def _rollback(
    paths: _Paths,
    layout: ArchiveLayout,
    journal_path: Path,
    journal: Journal,
    retries: int,
    retry_sleep: float,
) -> None:
    """Restore the previous generation; safe to repeat after another crash."""
    touched: set[Path] = set()
    for entry in reversed(journal.entries):
        final = paths.absolute(entry.final)
        backup = paths.absolute(entry.backup)
        if backup.exists():
            fsio.ensure_dir(final.parent)
            fsio.replace(backup, final, retries=retries, retry_sleep=retry_sleep)
            touched.add(final.parent)
        elif entry.action is Action.REPLACE and not entry.had_original and final.exists():
            final.unlink()
            touched.add(final.parent)
    _fsync_dirs(touched)
    _cleanup(paths, layout, journal_path, journal)


def _cleanup(paths: _Paths, layout: ArchiveLayout, journal_path: Path, journal: Journal) -> None:
    for entry in journal.entries:
        paths.absolute(entry.backup).unlink(missing_ok=True)
        _remove_empty_parents(paths.absolute(entry.backup).parent, layout.journal_root)
        if entry.staged is not None:
            paths.absolute(entry.staged).unlink(missing_ok=True)
    journal_path.unlink(missing_ok=True)
    _remove_empty_parents(layout.staging_dir(journal.run_id), layout.root)
    _remove_empty_parents(layout.journal_root, layout.root)


def _remove_empty_parents(directory: Path, stop: Path) -> None:
    current = directory
    while current != stop and fsio.is_within(stop, current):
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def pending(layout: ArchiveLayout) -> int:
    """The journals of interrupted transactions that the next writer recovers."""
    root = layout.journal_root
    return sum(1 for _ in root.glob("*.json")) if root.is_dir() else 0


def recover(layout: ArchiveLayout, retries: int = 0, retry_sleep: float = 0.0) -> int:
    """Finish every interrupted transaction; call only while holding the archive lock.

    Returns the number of journals processed. Staging left by a run that died
    before its first journal is removed as well.
    """
    recovered = _recover_journals(layout, retries, retry_sleep)
    shutil.rmtree(layout.staging_root, ignore_errors=True)
    return recovered


def _recover_journals(layout: ArchiveLayout, retries: int, retry_sleep: float) -> int:
    root = layout.journal_root
    if not root.is_dir():
        return 0
    paths = _Paths(layout.root)
    for leftover in root.glob("*.tmp"):
        leftover.unlink(missing_ok=True)
    recovered = 0
    for journal_path in sorted(root.glob("*.json")):
        try:
            raw = journal_path.read_bytes()
        except OSError as exc:
            raise _unreadable() from exc
        journal = _decode(raw)
        for entry in journal.entries:
            paths.absolute(entry.final)
            paths.absolute(entry.backup)
            if entry.staged is not None:
                paths.absolute(entry.staged)
        if journal.phase is Phase.COMMITTED:
            _cleanup(paths, layout, journal_path, journal)
        else:
            _rollback(paths, layout, journal_path, journal, retries, retry_sleep)
        recovered += 1
    return recovered
