"""The privacy audit: what it finds, and that the repository has nothing for it to find.

Private identifiers of a maintainer belong in a denylist outside the
repository, never in this file: naming them here would be the leak itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.repo import tooling

pytestmark = pytest.mark.repo

#: A token shape the audit knows, assembled so this file does not contain one.
MARKER = "gh" + "p_" + "A" * 36
#: An address outside the allowed domains, assembled so the tree audit does not report this file.
PERSONAL_EMAIL = "someone" + "@" + "mail.invalid"
SYNTHETIC = ("Synthetic", "synthetic@example.com")

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs Git")


class Repository:
    """A Git repository committed with plumbing: no hook and no user configuration applies."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.head: str | None = None
        self.git("init", "--quiet")

    def git(
        self,
        *args: str,
        author: tuple[str, str] = SYNTHETIC,
        committer: tuple[str, str] = SYNTHETIC,
    ) -> str:
        identity = {
            "GIT_AUTHOR_NAME": author[0],
            "GIT_AUTHOR_EMAIL": author[1],
            "GIT_COMMITTER_NAME": committer[0],
            "GIT_COMMITTER_EMAIL": committer[1],
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            env=os.environ | identity,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(
        self,
        files: dict[str, str],
        message: str,
        *,
        author: tuple[str, str] = SYNTHETIC,
        committer: tuple[str, str] = SYNTHETIC,
    ) -> str:
        """Write *files*, commit the index on top of HEAD and move HEAD there."""
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            self.git("add", name)
        parents = [f"-p{self.head}"] if self.head else []
        tree = self.git("write-tree")
        self.head = self.git(
            "commit-tree", tree, *parents, "-m", message, author=author, committer=committer
        )
        self.git("update-ref", "HEAD", self.head)
        return self.head

    def blob(self, revision: str, name: str) -> str:
        return self.git("rev-parse", f"{revision}:{name}")

    def acknowledge(self, *lines: str) -> None:
        path = self.root / ".github" / "privacy-audit-acknowledged.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_text("# synthetic\n" + "".join(f"{line}\n" for line in lines), encoding="utf-8")


@pytest.fixture
def audit() -> ModuleType:
    return tooling.load_tool("privacy_audit")


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    return Repository(tmp_path)


def rules(findings: set[object]) -> set[str]:
    return {item.rule for item in findings}  # type: ignore[attr-defined]


def test_the_publishable_tree_is_clean(audit: ModuleType) -> None:
    assert audit.scan_paths(tooling.ROOT) == set()


def test_secret_shapes_are_found_without_echoing_them(audit: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("Authorization: Bearer " + MARKER, encoding="utf-8")

    findings = audit.scan_paths(tmp_path)

    assert rules(findings) == {"bearer_token", "known_token_shape"}
    assert MARKER not in repr(findings)


def test_the_denylist_must_live_outside_the_repository(audit: ModuleType) -> None:
    with pytest.raises(ValueError, match="outside the repository"):
        audit.load_external_denylist(tooling.ROOT / "synthetic-denylist.txt", tooling.ROOT)


@needs_git
def test_history_scanning_finds_a_secret_that_a_later_commit_removed(
    audit: ModuleType, repository: Repository
) -> None:
    repository.commit({"notes.txt": MARKER}, "Add notes")
    repository.commit({"notes.txt": "nothing"}, "Remove the secret")

    assert audit.scan_paths(repository.root) == set()
    findings = audit.scan_git_history(repository.root)
    assert rules(findings) == {"known_token_shape"}
    assert all(item.location.endswith(":notes.txt") for item in findings)


@needs_git
def test_history_scanning_reads_every_commit_s_identities_and_message(
    audit: ModuleType, repository: Repository
) -> None:
    clean = repository.commit({"a.txt": "a"}, "Add a")
    leaking = repository.commit(
        {"b.txt": "b"}, f"Add b\n\nToken {MARKER}", author=("Someone", PERSONAL_EMAIL)
    )

    findings = audit.scan_git_history(repository.root)

    assert {(item.location, item.rule) for item in findings} == {
        (f"git-commit:{leaking}", "personal_email"),
        (f"git-commit:{leaking}", "known_token_shape"),
    }
    assert clean not in repr(findings)
    assert MARKER not in repr(findings)
    assert PERSONAL_EMAIL not in repr(findings)


@needs_git
def test_the_addresses_of_github_s_own_services_are_allowed(
    audit: ModuleType, repository: Repository
) -> None:
    """GitHub commits merges and Dependabot updates under these addresses."""
    repository.commit(
        {"a.txt": "a"},
        "Bump a\n\nSigned-off-by: dependabot[bot] <support@github.com>",
        author=("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com"),
        committer=("GitHub", "noreply@github.com"),
    )

    assert audit.scan_git_history(repository.root) == set()


@needs_git
def test_reviewed_history_findings_are_left_out(audit: ModuleType, repository: Repository) -> None:
    repository.commit({"notes.txt": MARKER}, "Add notes")
    blob = repository.blob("HEAD", "notes.txt")
    leaking = repository.commit({"notes.txt": "nothing"}, f"Remove {MARKER}")
    repository.acknowledge(
        f"{blob}:known_token_shape  # synthetic marker in an old note",
        f"{leaking}:known_token_shape  # synthetic marker in a message",
    )

    entries = audit.load_acknowledged(repository.root)
    remaining, acknowledged = audit.apply_acknowledged(
        audit.scan_git_history(repository.root), entries
    )

    assert remaining == set()
    assert acknowledged == 2


@needs_git
@pytest.mark.parametrize(
    ("line", "problem"),
    [
        ("{blob}:known_token_shape", "expected"),
        ("{blob}:no_such_rule  # reason", "unknown rule"),
        ("{blob}:external_denylist_1  # reason", "never acknowledged"),
        ("{head_blob}:known_token_shape  # reason", "part of HEAD"),
        ("{missing}:known_token_shape  # reason", "neither a blob nor a commit"),
    ],
)
def test_an_unsafe_or_malformed_entry_is_refused(
    audit: ModuleType, repository: Repository, line: str, problem: str
) -> None:
    repository.commit({"notes.txt": MARKER}, "Add notes")
    blob = repository.blob("HEAD", "notes.txt")
    repository.commit({"notes.txt": "nothing"}, "Remove the secret")
    head_blob = repository.blob("HEAD", "notes.txt")
    repository.acknowledge(line.format(blob=blob, head_blob=head_blob, missing="0" * 40))

    with pytest.raises(audit.AcknowledgedError, match=problem):
        audit.load_acknowledged(repository.root)


@needs_git
def test_a_duplicate_entry_is_refused(audit: ModuleType, repository: Repository) -> None:
    repository.commit({"notes.txt": MARKER}, "Add notes")
    blob = repository.blob("HEAD", "notes.txt")
    repository.commit({"notes.txt": "nothing"}, "Remove the secret")
    repository.acknowledge(*[f"{blob}:known_token_shape  # reason"] * 2)

    with pytest.raises(audit.AcknowledgedError, match="listed twice"):
        audit.load_acknowledged(repository.root)


@needs_git
def test_an_entry_that_suppresses_nothing_is_out_of_date(
    audit: ModuleType, repository: Repository
) -> None:
    repository.commit({"notes.txt": MARKER}, "Add notes")
    blob = repository.blob("HEAD", "notes.txt")
    repository.commit({"notes.txt": "nothing"}, "Remove the secret")
    repository.acknowledge(f"{blob}:personal_email  # reason")

    entries = audit.load_acknowledged(repository.root)
    with pytest.raises(audit.AcknowledgedError, match="suppresses no finding"):
        audit.apply_acknowledged(audit.scan_git_history(repository.root), entries)


@needs_git
def test_an_invalid_acknowledgement_file_exits_with_2(repository: Repository) -> None:
    repository.commit({"a.txt": "a"}, "Add a")
    repository.acknowledge("not an entry")

    result = subprocess.run(
        [
            sys.executable,
            str(tooling.TOOLS / "privacy_audit.py"),
            "--repository",
            str(repository.root),
            "--history",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2, result.stdout + result.stderr
