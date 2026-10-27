from __future__ import annotations

import itertools
import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from ha_history_exporter import fsio
from ha_history_exporter.archive import transaction
from ha_history_exporter.archive.layout import ArchiveLayout
from ha_history_exporter.archive.transaction import (
    Action,
    DayTransaction,
    Entry,
    Journal,
    Phase,
    recover,
)
from ha_history_exporter.errors import ExportError

DAY = date(2026, 7, 28)
RUN = "run"
ALL_FORMATS = ("jsonl", "csv", "parquet")


class Archive:
    """An output tree plus a working directory holding a new generation."""

    def __init__(self, root: Path) -> None:
        self.layout = ArchiveLayout(root / "output")
        self.work = root / "work"
        self.work.mkdir(parents=True)

    def final(self, fmt: str) -> Path:
        return self.layout.day_file(DAY, fmt)

    @property
    def manifest(self) -> Path:
        return self.layout.manifest_file(DAY)

    def publish_old(self, formats: Sequence[str]) -> None:
        for fmt in formats:
            fsio.atomic_write(self.final(fmt), f"old {fmt}".encode())
        fsio.atomic_write(self.manifest, b"old manifest")

    def transaction(self, formats: Sequence[str], *, retire: Sequence[str] = ()) -> DayTransaction:
        artifacts = []
        for fmt in formats:
            source = self.work / f"{DAY}.{fmt}"
            source.write_bytes(f"new {fmt}".encode())
            artifacts.append((source, self.final(fmt)))
        manifest = self.work / f"{DAY}.manifest.json"
        manifest.write_bytes(b"new manifest")
        return DayTransaction(
            self.layout,
            RUN,
            DAY,
            artifacts=artifacts,
            manifest=(manifest, self.manifest),
            retire=[self.final(fmt) for fmt in retire],
        )

    def day_files(self) -> dict[str, bytes]:
        directory = self.manifest.parent
        if not directory.is_dir():
            return {}
        return {
            path.name: path.read_bytes()
            for path in sorted(directory.iterdir())
            if path.name.startswith(DAY.isoformat())
        }

    def generation(self, tag: str, formats: Sequence[str]) -> dict[str, bytes]:
        files = {self.final(fmt).name: f"{tag} {fmt}".encode() for fmt in formats}
        files[self.manifest.name] = f"{tag} manifest".encode()
        return dict(sorted(files.items()))

    def assert_clean(self) -> None:
        assert not self.layout.journal_root.exists()
        assert not self.layout.staging_root.exists()


def fail_replace_into(
    monkeypatch: pytest.MonkeyPatch, target: Path, *, source_part: str | None = None
) -> None:
    real = fsio.replace

    def failing(source: Path, destination: Path, **kwargs: float) -> None:
        if destination == target and (source_part is None or source_part in source.parts):
            raise PermissionError("synthetic replace failure")
        real(source, destination, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fsio, "replace", failing)


def test_a_commit_promotes_the_manifest_last_and_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(tmp_path)
    promoted: list[str] = []
    real = fsio.replace

    def recording(source: Path, destination: Path, **kwargs: float) -> None:
        real(source, destination, **kwargs)  # type: ignore[arg-type]
        if destination.parent == archive.manifest.parent:
            promoted.append(destination.name)

    monkeypatch.setattr(fsio, "replace", recording)
    archive.transaction(["jsonl", "csv"]).commit()

    assert promoted == ["2026-07-28.jsonl", "2026-07-28.csv", "2026-07-28.manifest.json"]
    assert archive.day_files() == archive.generation("new", ["jsonl", "csv"])
    assert list(archive.work.iterdir()) == []
    archive.assert_clean()


def test_a_commit_copies_into_staging_when_the_work_directory_is_on_another_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(tmp_path)
    copied: list[Path] = []
    real_copy = transaction.shutil.copy2

    def recording_copy(source: Path, destination: Path) -> object:
        copied.append(destination.parent)
        return real_copy(source, destination)

    monkeypatch.setattr(fsio, "same_device", lambda first, second: False)
    monkeypatch.setattr(transaction.shutil, "copy2", recording_copy)
    archive.transaction(["jsonl"]).commit()

    assert copied == [archive.layout.staging_dir(RUN)] * 2
    assert archive.day_files() == archive.generation("new", ["jsonl"])
    assert list(archive.work.iterdir()) == []
    archive.assert_clean()


def test_a_failed_manifest_promotion_restores_the_old_generation_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(tmp_path)
    archive.publish_old(["jsonl"])
    fail_replace_into(monkeypatch, archive.manifest, source_part=".hhe-staging")

    with pytest.raises(PermissionError, match="synthetic"):
        archive.transaction(["jsonl"]).commit()

    assert archive.day_files() == archive.generation("old", ["jsonl"])
    archive.assert_clean()


def test_a_failed_first_generation_leaves_no_visible_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(tmp_path)
    fail_replace_into(monkeypatch, archive.manifest, source_part=".hhe-staging")

    with pytest.raises(PermissionError):
        archive.transaction(["jsonl", "csv"]).commit()

    assert archive.day_files() == {}
    archive.assert_clean()


def test_a_failure_while_backing_up_restores_every_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(tmp_path)
    archive.publish_old(["jsonl", "csv"])
    backup = archive.layout.journal_root / "backups" / RUN / DAY.isoformat()
    real = fsio.replace

    def fail_second_backup(source: Path, destination: Path, **kwargs: float) -> None:
        if source == archive.final("csv") and fsio.is_within(backup, destination):
            raise PermissionError("synthetic backup failure")
        real(source, destination, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fsio, "replace", fail_second_backup)
    with pytest.raises(PermissionError, match="synthetic"):
        archive.transaction(["jsonl", "csv"]).commit()

    assert archive.day_files() == archive.generation("old", ["jsonl", "csv"])
    archive.assert_clean()


def test_a_cleanup_failure_after_commit_keeps_the_new_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(tmp_path)
    archive.publish_old(["jsonl"])

    def refuse(*_args: object) -> None:
        raise PermissionError("synthetic cleanup failure")

    with monkeypatch.context() as patch:
        patch.setattr(transaction, "_cleanup", refuse)
        archive.transaction(["jsonl"]).commit()
    assert archive.day_files() == archive.generation("new", ["jsonl"])

    assert recover(archive.layout) == 1
    assert archive.day_files() == archive.generation("new", ["jsonl"])
    archive.assert_clean()


def test_recovery_finishes_a_rollback_that_was_itself_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(tmp_path)
    archive.publish_old(["jsonl", "csv"])

    def interrupted(*_args: object) -> None:
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        fail_replace_into(patch, archive.manifest, source_part=".hhe-staging")
        patch.setattr(transaction, "_rollback", interrupted)
        with pytest.raises(KeyboardInterrupt):
            archive.transaction(["jsonl", "csv"]).commit()
    assert archive.day_files() != archive.generation("old", ["jsonl", "csv"])

    assert recover(archive.layout) == 1
    assert archive.day_files() == archive.generation("old", ["jsonl", "csv"])
    archive.assert_clean()


def _subsets() -> list[tuple[str, ...]]:
    return [
        subset
        for size in range(1, len(ALL_FORMATS) + 1)
        for subset in itertools.combinations(ALL_FORMATS, size)
    ]


@pytest.mark.parametrize(("old", "new"), list(itertools.product(_subsets(), repeat=2)))
def test_a_new_generation_retires_every_format_it_no_longer_contains(
    tmp_path: Path, old: tuple[str, ...], new: tuple[str, ...]
) -> None:
    archive = Archive(tmp_path)
    archive.publish_old(old)
    retire = [fmt for fmt in old if fmt not in new]

    archive.transaction(new, retire=retire).commit()

    assert archive.day_files() == archive.generation("new", new)
    archive.assert_clean()


def test_a_failed_generation_restores_the_files_it_was_retiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(tmp_path)
    archive.publish_old(["jsonl", "csv", "parquet"])
    fail_replace_into(monkeypatch, archive.manifest, source_part=".hhe-staging")

    with pytest.raises(PermissionError):
        archive.transaction(["jsonl"], retire=["csv", "parquet"]).commit()

    assert archive.day_files() == archive.generation("old", ["jsonl", "csv", "parquet"])
    archive.assert_clean()


def test_a_retired_file_that_is_already_gone_is_ignored(tmp_path: Path) -> None:
    archive = Archive(tmp_path)
    archive.publish_old(["jsonl"])

    archive.transaction(["jsonl"], retire=["csv"]).commit()

    assert archive.day_files() == archive.generation("new", ["jsonl"])


def test_each_phase_is_journaled_only_after_its_files_are_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(tmp_path)
    archive.publish_old(["jsonl"])
    events: list[tuple[str, object]] = []
    real_write = fsio.atomic_write

    def recording_write(path: Path, data: bytes, **kwargs: object) -> None:
        real_write(path, data, **kwargs)  # type: ignore[arg-type]
        if path.parent == archive.layout.journal_root:
            events.append(("journal", json.loads(data)["phase"]))

    monkeypatch.setattr(fsio, "fsync_dir", lambda path: events.append(("dir", path)))
    monkeypatch.setattr(fsio, "atomic_write", recording_write)
    archive.transaction(["jsonl"]).commit()

    def index(event: tuple[str, object]) -> int:
        return events.index(event)

    day_dir = archive.manifest.parent
    backup_dir = archive.layout.journal_root / "backups" / RUN / DAY.isoformat()
    backup_day_dir = backup_dir / day_dir.relative_to(archive.layout.root)
    phases = [index(("journal", phase.value)) for phase in Phase]
    assert phases == sorted(phases)
    assert index(("dir", archive.layout.staging_dir(RUN))) < phases[0]
    assert phases[0] < index(("dir", backup_day_dir)) < phases[1]
    promoted_flushes = [i for i, event in enumerate(events) if event == ("dir", day_dir)]
    assert any(phases[1] < i < phases[2] for i in promoted_flushes)
    assert any(phases[2] < i < phases[3] for i in promoted_flushes)


def test_a_journal_round_trips_through_its_encoding() -> None:
    journal = Journal(
        RUN,
        DAY.isoformat(),
        Phase.BACKED_UP,
        (
            Entry(Action.REPLACE, "a/x.jsonl", "j/x.jsonl", True, ".hhe-staging/run/00-x"),
            Entry(Action.REMOVE, "a/x.csv", "j/x.csv", True),
        ),
    )
    assert transaction._decode(journal.encode()) == journal


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b"{broken",
        b"[]",
        b'{"version": 2, "entries": []}',
        b'{"version": 3, "run_id": "r", "day": "d", "phase": "prepared", "entries": [{}]}',
        b'{"version": 2, "run_id": "r", "day": "d", "phase": "later", "entries": '
        b'[{"action": "replace", "final": "f", "backup": "b", "had_original": true, '
        b'"staged": "s"}]}',
        b'{"version": 2, "run_id": "r", "day": "d", "phase": "prepared", "entries": '
        b'[{"action": "replace", "final": "f", "backup": "b", "had_original": "yes", '
        b'"staged": "s"}]}',
    ],
)
def test_a_damaged_journal_stops_recovery(tmp_path: Path, raw: bytes) -> None:
    layout = ArchiveLayout(tmp_path)
    layout.journal_root.mkdir(parents=True)
    (layout.journal_root / "run-2026-07-28.json").write_bytes(raw)
    with pytest.raises(ExportError, match="cannot be read safely"):
        recover(layout)


@pytest.mark.parametrize("final", ["../outside.jsonl", "/absolute.jsonl", ""])
def test_a_journal_path_outside_the_archive_stops_recovery(tmp_path: Path, final: str) -> None:
    layout = ArchiveLayout(tmp_path / "output")
    journal = Journal(
        RUN, DAY.isoformat(), Phase.PREPARED, (Entry(Action.REMOVE, final, "b", True),)
    )
    fsio.atomic_write(layout.journal_root / "run-2026-07-28.json", journal.encode())
    with pytest.raises(ExportError, match="invalid path"):
        recover(layout)


def test_a_transaction_target_outside_the_archive_is_refused(tmp_path: Path) -> None:
    archive = Archive(tmp_path)
    source = archive.work / "x.jsonl"
    source.write_bytes(b"x")
    with pytest.raises(ExportError, match="escaped the output directory"):
        DayTransaction(
            archive.layout,
            RUN,
            DAY,
            artifacts=[(source, tmp_path / "elsewhere.jsonl")],
            manifest=(source, archive.manifest),
        ).commit()


def test_recovery_restores_an_interrupted_first_protocol_journal(tmp_path: Path) -> None:
    archive = Archive(tmp_path)
    root = archive.layout.root
    final = archive.final("jsonl")
    relative = final.relative_to(root).as_posix()
    backup = archive.layout.journal_root / "backups" / RUN / DAY.isoformat() / relative
    staged = archive.layout.staging_dir(RUN) / "00-x.jsonl"
    for path, content in ((final, b"new jsonl"), (backup, b"old jsonl"), (staged, b"")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    journal = {
        "version": 1,
        "run_id": RUN,
        "day": DAY.isoformat(),
        "phase": "artifacts_promoted",
        "entries": [
            {
                "staged": staged.relative_to(root).as_posix(),
                "final": relative,
                "backup": backup.relative_to(root).as_posix(),
                "had_original": True,
            }
        ],
    }
    journal_path = archive.layout.journal_root / f"{RUN}-{DAY}.json"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    journal_path.with_suffix(".json.tmp").write_text("{", encoding="utf-8")

    assert recover(archive.layout) == 1
    assert final.read_bytes() == b"old jsonl"
    archive.assert_clean()


def test_recovery_removes_staging_of_a_run_that_died_before_its_journal(tmp_path: Path) -> None:
    layout = ArchiveLayout(tmp_path / "output")
    leftover = layout.staging_dir("dead") / "00-2026-07-28.jsonl"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"x")
    assert recover(layout) == 0
    assert not layout.staging_root.exists()


_CRASHING_CHILD = """
import os, sys
from datetime import date
from pathlib import Path
from ha_history_exporter import fsio
from ha_history_exporter.archive.layout import ArchiveLayout
from ha_history_exporter.archive.transaction import DayTransaction

root, work, crash_after = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
real, calls = fsio.replace, 0

def crashing(source, target, **kwargs):
    global calls
    real(source, target, **kwargs)
    calls += 1
    if calls == crash_after:
        os._exit(17)

fsio.replace = crashing
layout, day = ArchiveLayout(root), date(2026, 7, 28)
DayTransaction(
    layout,
    "run",
    day,
    artifacts=[
        (work / "2026-07-28.jsonl", layout.day_file(day, "jsonl")),
        (work / "2026-07-28.parquet", layout.day_file(day, "parquet")),
    ],
    manifest=(work / "2026-07-28.manifest.json", layout.manifest_file(day)),
    retire=[layout.day_file(day, "csv")],
).commit()
"""


def test_a_crash_at_any_step_recovers_to_exactly_one_generation(tmp_path: Path) -> None:
    """Old jsonl+csv is replaced by new jsonl+parquet; a process dies after each rename."""
    outcomes = []
    for crash_after in itertools.count(1):
        archive = Archive(tmp_path / f"crash-{crash_after}")
        archive.publish_old(["jsonl", "csv"])
        for fmt in ("jsonl", "parquet"):
            (archive.work / f"{DAY}.{fmt}").write_bytes(f"new {fmt}".encode())
        (archive.work / f"{DAY}.manifest.json").write_bytes(b"new manifest")
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                _CRASHING_CHILD,
                str(archive.layout.root),
                str(archive.work),
                str(crash_after),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert child.returncode in (0, 17), child.stderr
        recover(archive.layout)
        files = archive.day_files()
        old = archive.generation("old", ["jsonl", "csv"])
        new = archive.generation("new", ["jsonl", "parquet"])
        assert files in (old, new), f"crash after rename {crash_after}: {sorted(files)}"
        archive.assert_clean()
        outcomes.append(files == new)
        if child.returncode == 0:
            break
        assert crash_after < 50
    assert outcomes[0] is False
    assert outcomes[-1] is True
    assert outcomes == sorted(outcomes)
