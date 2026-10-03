"""Contract tests for the runtime dependency audit tool.

The tool audits what a user installs. Its commands are asserted here without network
access; whether an advisory exists is pip-audit's verdict in CI.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]


def load_tool() -> ModuleType:
    path = ROOT / "tools" / "audit_runtime.py"
    spec = importlib.util.spec_from_file_location("audit_runtime", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_audit_command_inspects_only_the_given_environment():
    command = load_tool().audit_command(Path("site-packages"), [])
    assert command[:3] == [sys.executable, "-m", "pip_audit"]
    assert command[command.index("--path") + 1] == "site-packages"
    assert "--ignore-vuln" not in command


def test_audit_command_ignores_only_explicitly_named_vulnerabilities():
    command = load_tool().audit_command(Path("site-packages"), ["GHSA-aaaa", "PYSEC-1"])
    ignored = [command[i + 1] for i, item in enumerate(command) if item == "--ignore-vuln"]
    assert ignored == ["GHSA-aaaa", "PYSEC-1"]


def test_install_command_puts_the_project_with_its_extras_into_the_target():
    command = load_tool().install_command(Path("env") / "python")
    assert command == [
        sys.executable,
        "-m",
        "pip",
        "--python",
        str(Path("env") / "python"),
        "install",
        "--disable-pip-version-check",
        ".[parquet]",
    ]


@pytest.mark.parametrize("audit_exit_code", [0, 1])
def test_the_audit_runs_after_the_install_and_its_verdict_is_the_result(
    monkeypatch, audit_exit_code
):
    tool = load_tool()
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append([str(item) for item in command])
        if "sysconfig" in " ".join(map(str, command)):
            return subprocess.CompletedProcess(command, 0, stdout="site-packages\n")
        is_audit = "pip_audit" in command
        return subprocess.CompletedProcess(command, audit_exit_code if is_audit else 0)

    monkeypatch.setattr(tool.subprocess, "run", fake_run)

    assert tool.audit_runtime(ROOT, []) == audit_exit_code
    kinds = [
        "install" if "install" in call else "audit" if "pip_audit" in call else "site"
        for call in calls
    ]
    assert kinds == ["install", "site", "audit"]


def test_a_failing_install_is_not_reported_as_a_clean_audit(monkeypatch):
    tool = load_tool()

    def fake_run(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(tool.subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        tool.audit_runtime(ROOT, [])
