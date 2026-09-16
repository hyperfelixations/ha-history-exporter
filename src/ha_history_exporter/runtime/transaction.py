"""Crash-recoverable publication of one complete day generation."""

from __future__ import annotations

import json
import os
import shutil
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ExportError
from ..writers import atomic_replace
from .workspace import Workspace, same_filesystem

JOURNAL_DIRNAME = ".hhe-transactions"
JOURNAL_VERSION = 1


def journal_root(output_dir: Path) -> Path:
    return output_dir / JOURNAL_DIRNAME


def _replace_file(src: Path, dst: Path, retries: int, retry_sleep: float) -> None:
    atomic_replace(src, dst, retries, retry_sleep)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_journal(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    _fsync_directory(path.parent)


def _inside(root: Path, path: Path) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve()
    try:
        return resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ExportError("A transaction path escaped the output directory.") from exc


def _relative(root: Path, path: Path) -> str:
    return _inside(root, path).as_posix()


def _from_relative(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ExportError("An export transaction journal contains an invalid path.")
    candidate = root / Path(value)
    _inside(root, candidate)
    return candidate


def _remove_empty_parents(path: Path, stop: Path) -> None:
    current = path
    while current != stop:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


@dataclass
class DayTransaction:
    """Publish validated artifacts and their manifest as one generation."""

    workspace: Workspace
    day: str
    artifacts: list[tuple[Path, Path]]
    manifest: tuple[Path, Path]
    locked_file_retries: int = 0
    locked_file_retry_sleep: float = 0.0

    def _stage(self, source: Path, index: int) -> Path:
        staging = self.workspace.staging_dir
        staging.mkdir(parents=True, exist_ok=True)
        destination = staging / f"{index:02d}-{source.name}"
        if same_filesystem(source.parent, staging):
            _replace_file(
                source,
                destination,
                self.locked_file_retries,
                self.locked_file_retry_sleep,
            )
        else:
            shutil.copy2(source, destination)
            _fsync_file(destination)
            source.unlink()
        return destination

    def commit(self) -> None:
        """Commit the generation, rolling normal failures back byte-exactly."""
        output = self.workspace.output_dir.resolve()
        pairs = [*self.artifacts, self.manifest]
        staged_pairs: list[tuple[Path, Path]] = []
        for index, (source, final) in enumerate(pairs):
            _inside(output, final)
            staged_pairs.append((self._stage(source, index), final))

        root = journal_root(output)
        backup_root = root / "backups" / self.workspace.run_id / self.day
        journal = root / f"{self.workspace.run_id}-{self.day}.json"
        entries: list[dict[str, Any]] = []
        for staged, final in staged_pairs:
            final_relative = _relative(output, final)
            backup = backup_root / Path(final_relative)
            entries.append(
                {
                    "staged": _relative(output, staged),
                    "final": final_relative,
                    "backup": _relative(output, backup),
                    "had_original": final.exists(),
                }
            )
        data: dict[str, Any] = {
            "version": JOURNAL_VERSION,
            "run_id": self.workspace.run_id,
            "day": self.day,
            "phase": "prepared",
            "entries": entries,
        }
        _write_journal(journal, data)

        try:
            for entry in entries:
                if not entry["had_original"]:
                    continue
                final = _from_relative(output, entry["final"])
                backup = _from_relative(output, entry["backup"])
                backup.parent.mkdir(parents=True, exist_ok=True)
                _replace_file(
                    final,
                    backup,
                    self.locked_file_retries,
                    self.locked_file_retry_sleep,
                )
            data["phase"] = "backed_up"
            _write_journal(journal, data)

            for entry in entries[:-1]:
                _replace_file(
                    _from_relative(output, entry["staged"]),
                    _from_relative(output, entry["final"]),
                    self.locked_file_retries,
                    self.locked_file_retry_sleep,
                )
            data["phase"] = "artifacts_promoted"
            _write_journal(journal, data)

            manifest_entry = entries[-1]
            _replace_file(
                _from_relative(output, manifest_entry["staged"]),
                _from_relative(output, manifest_entry["final"]),
                self.locked_file_retries,
                self.locked_file_retry_sleep,
            )
            data["phase"] = "committed"
            _write_journal(journal, data)
        except Exception:
            _rollback(output, journal, data, self.locked_file_retries, self.locked_file_retry_sleep)
            raise

        # The manifest is already visible, so the generation is committed. A
        # remaining committed journal is safely cleaned by the next run.
        with suppress(OSError):
            _cleanup(output, journal, data)


def _validate_journal(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != {
        "version",
        "run_id",
        "day",
        "phase",
        "entries",
    }:
        raise ExportError("An export transaction journal is malformed.")
    if data["version"] != JOURNAL_VERSION or data["phase"] not in {
        "prepared",
        "backed_up",
        "artifacts_promoted",
        "committed",
    }:
        raise ExportError("An export transaction journal is not supported.")
    if not isinstance(data["entries"], list) or not data["entries"]:
        raise ExportError("An export transaction journal has no files.")
    for entry in data["entries"]:
        if not isinstance(entry, dict) or set(entry) != {
            "staged",
            "final",
            "backup",
            "had_original",
        } or not isinstance(entry["had_original"], bool):
            raise ExportError("An export transaction journal entry is malformed.")
    return data


def _rollback(
    output: Path,
    journal: Path,
    data: dict[str, Any],
    retries: int,
    retry_sleep: float,
) -> None:
    for entry in reversed(data["entries"]):
        final = _from_relative(output, entry["final"])
        backup = _from_relative(output, entry["backup"])
        if backup.exists():
            _replace_file(backup, final, retries, retry_sleep)
        elif not entry["had_original"]:
            final.unlink(missing_ok=True)
    _cleanup(output, journal, data)


def _cleanup(output: Path, journal: Path, data: dict[str, Any]) -> None:
    for entry in data["entries"]:
        staged = _from_relative(output, entry["staged"])
        backup = _from_relative(output, entry["backup"])
        staged.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
        _remove_empty_parents(backup.parent, journal_root(output))
    journal.unlink(missing_ok=True)
    root = journal_root(output)
    staging = output / ".hhe-staging" / data["run_id"]
    _remove_empty_parents(staging, output / ".hhe-staging")
    _remove_empty_parents(root, output)


def recover_incomplete(
    output_dir: Path,
    locked_file_retries: int = 0,
    locked_file_retry_sleep: float = 0.0,
) -> int:
    """Roll back every interrupted generation while the output lock is held."""
    output = output_dir.resolve()
    root = journal_root(output)
    if not root.is_dir():
        return 0
    recovered = 0
    for journal in sorted(root.glob("*.json")):
        try:
            data = _validate_journal(json.loads(journal.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExportError("An export transaction journal cannot be read safely.") from exc
        for entry in data["entries"]:
            for name in ("staged", "final", "backup"):
                _from_relative(output, entry[name])
        if data["phase"] == "committed":
            _cleanup(output, journal, data)
        else:
            _rollback(
                output,
                journal,
                data,
                locked_file_retries,
                locked_file_retry_sleep,
            )
        recovered += 1
    return recovered
