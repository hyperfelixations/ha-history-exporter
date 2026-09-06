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
    example = (ROOT / "export_config.example.yaml").read_text(encoding="utf-8")

    assert "/export_config.yaml" in gitignore
    assert "export_config.example.yaml" not in gitignore
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
        ROOT / "export_config.example.yaml",
        ROOT / "ha_history_batch_export.py",
        ROOT / "ha_history_export_last_10_days.bat",
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
