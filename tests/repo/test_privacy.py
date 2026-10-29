"""The privacy audit: what it finds, and that the repository has nothing for it to find.

Private identifiers of a maintainer belong in a denylist outside the
repository, never in this file: naming them here would be the leak itself.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.repo import tooling

pytestmark = pytest.mark.repo

#: A token shape the audit knows, assembled so this file does not contain one.
MARKER = "gh" + "p_" + "A" * 36


def test_the_publishable_tree_is_clean() -> None:
    assert tooling.load_tool("privacy_audit").scan_paths(tooling.ROOT) == set()


def test_secret_shapes_are_found_without_echoing_them(tmp_path: Path) -> None:
    audit = tooling.load_tool("privacy_audit")
    (tmp_path / "sample.txt").write_text("Authorization: Bearer " + MARKER, encoding="utf-8")

    findings = audit.scan_paths(tmp_path)

    assert {item.rule for item in findings} == {"bearer_token", "known_token_shape"}
    assert MARKER not in repr(findings)


def test_the_denylist_must_live_outside_the_repository() -> None:
    audit = tooling.load_tool("privacy_audit")
    with pytest.raises(ValueError, match="outside the repository"):
        audit.load_external_denylist(tooling.ROOT / "synthetic-denylist.txt", tooling.ROOT)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs Git")
def test_history_scanning_finds_a_secret_that_a_later_commit_removed(tmp_path: Path) -> None:
    audit = tooling.load_tool("privacy_audit")

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.com", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(message: str, *parents: str) -> str:
        """A commit of the index, written with plumbing, so no hook is involved."""
        tree = git("write-tree")
        return git("commit-tree", tree, *(f"-p{parent}" for parent in parents), "-m", message)

    git("init", "--quiet")
    leaked = tmp_path / "notes.txt"
    leaked.write_text(MARKER, encoding="utf-8")
    git("add", "notes.txt")
    first = commit("Add notes")
    leaked.write_text("nothing", encoding="utf-8")
    git("add", "notes.txt")
    git("update-ref", "HEAD", commit("Remove the secret", first))

    assert audit.scan_paths(tmp_path) == set()
    findings = audit.scan_git_history(tmp_path)
    assert {item.rule for item in findings} == {"known_token_shape"}
    assert all(item.location.endswith(":notes.txt") for item in findings)
