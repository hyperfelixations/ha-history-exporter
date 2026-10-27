from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ha_history_exporter import fsio


def test_atomic_write_creates_parents_and_replaces_the_content(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "file.json"
    fsio.atomic_write(target, b"first")
    fsio.atomic_write(target, b"second\n")

    assert target.read_bytes() == b"second\n"
    assert [path.name for path in target.parent.iterdir()] == ["file.json"]


def test_atomic_write_leaves_the_old_content_when_the_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    directory.mkdir()
    target = directory / "file.json"
    target.write_bytes(b"old")

    def refuse(self: Path, target: Path) -> Path:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(OSError, match="synthetic"):
        fsio.atomic_write(target, b"new")

    assert target.read_bytes() == b"old"
    assert [path.name for path in directory.iterdir()] == ["file.json"]


def test_atomic_write_flushes_file_and_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        synced.append("file")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(fsio, "fsync_dir", lambda path: synced.append(f"dir:{path.name}"))

    fsio.atomic_write(tmp_path / "file.json", b"data")

    assert synced == ["file", f"dir:{tmp_path.name}"]


def test_ensure_dir_names_every_new_directory_durably_in_its_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[Path] = []
    monkeypatch.setattr(fsio, "fsync_dir", synced.append)
    (tmp_path / "a").mkdir()

    fsio.ensure_dir(tmp_path / "a" / "b" / "c")
    fsio.ensure_dir(tmp_path / "a" / "b" / "c")

    assert (tmp_path / "a" / "b" / "c").is_dir()
    assert synced == [tmp_path / "a", tmp_path / "a" / "b"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
def test_new_directories_are_private_and_existing_ones_keep_their_mode(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    old_umask = os.umask(0o022)
    try:
        fsio.ensure_dir(shared / "a" / "b")
        fsio.create_dir(shared / "c")
    finally:
        os.umask(old_umask)
    assert shared.stat().st_mode & 0o777 == 0o755
    for name in ("a", "a/b", "c"):
        assert (shared / name).stat().st_mode & 0o777 == 0o700, name


def test_create_dir_refuses_an_existing_directory(tmp_path: Path) -> None:
    fsio.create_dir(tmp_path / "parent" / "run")
    with pytest.raises(FileExistsError):
        fsio.create_dir(tmp_path / "parent" / "run")


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
def test_atomic_write_applies_the_mode_to_a_new_file(tmp_path: Path) -> None:
    target = tmp_path / "secret"
    fsio.atomic_write(target, b"x", mode=0o600)
    assert target.stat().st_mode & 0o777 == 0o600


def test_replace_retries_while_another_program_locks_the_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_bytes(b"new")
    attempts: list[int] = []
    sleeps: list[float] = []
    real_replace = Path.replace

    def flaky(self: Path, destination: Path) -> Path:
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("locked")
        return real_replace(self, destination)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "replace", flaky)
        fsio.replace(source, target, retries=2, retry_sleep=0.5, sleep=sleeps.append)

    assert target.read_bytes() == b"new"
    assert len(attempts) == 3
    assert sleeps == [0.5, 0.5]


def test_replace_gives_up_after_the_last_retry(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"new")

    def locked(self: Path, destination: Path) -> Path:
        raise PermissionError("locked")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "replace", locked)
        with pytest.raises(PermissionError):
            fsio.replace(source, tmp_path / "target", retries=1, sleep=lambda _s: None)


def test_append_durable_creates_and_extends_a_file(tmp_path: Path) -> None:
    target = tmp_path / "log" / "runs.jsonl"
    fsio.append_durable(target, b"one\n")
    fsio.append_durable(target, b"two\n")
    assert target.read_bytes() == b"one\ntwo\n"


@pytest.mark.parametrize(
    ("relative", "inside"),
    [
        ("exports/daily/2026/07/x.jsonl", True),
        (".", True),
        ("exports/../metadata/runs.jsonl", True),
        ("../outside", False),
        ("exports/../../outside", False),
    ],
)
def test_is_within_compares_lexically(tmp_path: Path, relative: str, inside: bool) -> None:
    root = tmp_path / "output"
    assert fsio.is_within(root, root / relative) is inside


def test_is_within_accepts_a_linked_subdirectory_on_another_disk(tmp_path: Path) -> None:
    root = tmp_path / "output"
    elsewhere = tmp_path / "elsewhere" / "exports"
    root.mkdir()
    elsewhere.mkdir(parents=True)
    link = root / "exports"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(elsewhere)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(elsewhere, target_is_directory=True)

    assert fsio.is_within(root, link / "daily" / "2026" / "x.jsonl")


@pytest.mark.skipif(os.name != "nt", reason="drive letters exist on Windows only")
def test_is_within_rejects_another_drive() -> None:
    assert not fsio.is_within(Path("C:/archive"), Path("D:/archive/exports"))
