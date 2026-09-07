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


def test_local_config_is_ignored_and_example_is_publishable():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")

    assert "/export_config.yaml" in gitignore
    assert "/config.yaml" in gitignore
    assert "/ha-history-exporter.yaml" in gitignore
    assert "config.example.yaml" not in gitignore
    assert not re.search(r"[A-Za-z]:\\\\Users\\\\", example)
    assert "HA_TOKEN:" not in example
    assert "synthetic-test-token" not in example


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
        ROOT / "config.example.yaml",
        ROOT / "ha_history_batch_export.py",
        ROOT / ".github" / "workflows" / "release.yml",
        *(ROOT / "ha_history_exporter").glob("*.py"),
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


def test_example_configuration_covers_exactly_the_supported_keys():
    """The published example must track the key registry, in both directions."""
    from ha_history_exporter.settings import schema
    from ha_history_exporter.settings.schema import KeyStatus

    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")

    for key in schema.KEYS:
        mentioned = re.search(
            rf"^\s*#?\s*{re.escape(key.name)}:", text, re.MULTILINE
        )
        if key.status is KeyStatus.SUPPORTED:
            assert mentioned, f"{key.path} is missing from config.example.yaml"
        elif key.status is KeyStatus.DEFAULT_ONLY:
            assert not mentioned, (
                f"{key.path} is not implemented and must not be advertised"
            )


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
