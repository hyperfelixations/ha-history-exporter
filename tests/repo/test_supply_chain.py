"""What the repository promises about its tools and distributions.

Syntax and security of the workflows are checked by actionlint and zizmor
(pinned actions, persisted credentials, permissions, Trusted Publishing,
Dependabot cooldown). These tests keep the rules those tools do not know.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.repo import tooling

pytestmark = pytest.mark.repo

WORKFLOWS = sorted((tooling.ROOT / ".github" / "workflows").glob("*.yml"))
REQUIREMENTS = tooling.ROOT / "requirements-dev.txt"

#: Secret names that would replace Trusted Publishing with a stored token.
STORED_TOKENS = re.compile(r"PYPI_API_TOKEN|TWINE_PASSWORD|TWINE_USERNAME|secrets\.\w*PYPI", re.I)
#: What a workflow may install: this project, and requirements from the pinned file.
ALLOWED_TARGETS = frozenset({".", ".[parquet]"})
#: Install options that take a value.
VALUED_OPTIONS = frozenset({"-r", "--python", "--resolution"})


def test_every_development_tool_is_pinned_exactly() -> None:
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[A-Za-z0-9_.-]+==\d[\w.]*", line), line


def install_arguments(text: str) -> list[list[str]]:
    """The arguments after ``pip install`` of every command, continuation lines joined."""
    joined = re.sub(r"\\\n\s*", " ", text)
    return [
        command.split("pip install", 1)[1].replace('"', "").split()
        for command in joined.splitlines()
        if "pip install" in command
    ]


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_workflows_install_tools_only_from_the_pins(workflow: Path) -> None:
    """An unpinned ``pip install build`` would run whatever PyPI serves that day."""
    for arguments in install_arguments(workflow.read_text(encoding="utf-8")):
        tokens = iter(arguments)
        for token in tokens:
            if token == "-r":
                assert next(tokens) == "requirements-dev.txt", arguments
            elif token in VALUED_OPTIONS:
                next(tokens)
            elif not token.startswith("-"):
                assert token in ALLOWED_TARGETS, arguments


def test_the_install_rule_rejects_an_unpinned_tool() -> None:
    (arguments,) = install_arguments("run: |\n  python -m pip install build \\\n    twine\n")
    assert arguments == ["build", "twine"]
    assert not set(arguments) <= ALLOWED_TARGETS


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_no_workflow_stores_an_index_token(workflow: Path) -> None:
    text = workflow.read_text(encoding="utf-8")
    assert not STORED_TOKENS.search(text), "publishing stays on Trusted Publishing"


def test_dependency_manifests_live_where_dependabot_watches() -> None:
    """Dependabot reads the repository root; a manifest elsewhere would never be updated."""
    for folder in ("src", "tests", "tools"):
        assert not list((tooling.ROOT / folder).rglob("requirements*.txt")), folder
        assert not list((tooling.ROOT / folder).rglob("pyproject.toml")), folder


def test_the_sdist_check_knows_what_belongs_to_the_repository_only() -> None:
    tool = tooling.load_tool("verify_distributions")
    check = tool.sdist_problems
    complete = [f"ha_history_exporter-2.0.0/{path}" for path in tool.SDIST_REQUIRED]
    assert check(complete) == []
    assert check([*complete, "ha_history_exporter-2.0.0/tests/repo/test_privacy.py"]) == [
        "contains tests/repo/test_privacy.py"
    ]
    assert check(complete[1:]) == ["missing LICENSE"]
