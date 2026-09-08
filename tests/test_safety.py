from __future__ import annotations

import os
import re
import socket
from pathlib import Path

import pytest
from pytest_socket import SocketBlockedError

ROOT = Path(__file__).parents[1]


def test_socket_access_is_blocked_by_default():
    with (
        pytest.warns(UserWarning, match="tried to use socket.socket"),
        pytest.raises(SocketBlockedError),
    ):
        socket.socket()


def test_production_environment_is_removed():
    assert "HA_URL" not in os.environ
    assert "HA_TOKEN" not in os.environ


def test_local_configuration_files_are_ignored():
    """A configuration file in the checkout must never be committed."""
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "/export_config.yaml" in gitignore
    assert "/config.yaml" in gitignore
    assert "/ha-history-exporter.yaml" in gitignore


def test_no_example_configuration_is_shipped():
    """`hhe config edit` generates a documented file with real defaults.

    A checked-in example would duplicate the key registry and drift.
    """
    assert not (ROOT / "config.example.yaml").exists()


def test_test_sources_contain_no_absolute_user_paths_or_real_endpoints():
    forbidden_patterns = [
        re.compile(r"[A-Za-z]:\\\\Users\\\\", re.IGNORECASE),
        re.compile(r"https?://(?:homeassistant|ha)\\.(?!invalid)", re.IGNORECASE),
    ]

    for path in (ROOT / "tests").rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".md"}:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            assert not pattern.search(text), f"{path} contains {pattern.pattern}"


def test_publishable_sources_contain_no_private_workspace_paths():
    publishable_paths = [
        ROOT / "README.md",
        ROOT / "LICENSE",
        ROOT / "pyproject.toml",
        ROOT / ".gitattributes",
        ROOT / ".github" / "workflows" / "release.yml",
        ROOT / "tools" / "refresh_golden.py",
        *(ROOT / "ha_history_exporter").rglob("*.py"),
        *(ROOT / "tests" / "golden").iterdir(),
    ]
    forbidden_patterns = [
        re.compile(r"[A-Za-z]:\\Users\\", re.IGNORECASE),
        re.compile(r"\buser\b", re.IGNORECASE),
        re.compile(r"\bInternal-(?:Data|HomeAssistant)\b", re.IGNORECASE),
    ]

    for path in publishable_paths:
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            assert not pattern.search(text), f"{path} contains {pattern.pattern}"


def test_local_helper_script_is_not_published():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/ha_history_export_last_10_days.bat" in gitignore


def test_runtime_output_stays_ascii():
    """Log lines and printed text must survive a legacy console code page."""
    import ast

    non_ascii = re.compile(r"[^\x00-\x7f]")
    offenders = []

    for path in sorted((ROOT / "ha_history_exporter").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            doc
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
            for doc in [ast.get_docstring(node, clean=False)]
            if doc
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value in docstrings:
                continue
            if non_ascii.search(node.value):
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, f"non-ASCII runtime strings: {offenders}"
