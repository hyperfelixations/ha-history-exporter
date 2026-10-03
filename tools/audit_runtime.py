#!/usr/bin/env python3
"""Audit the runtime dependencies a user installs for known vulnerabilities.

The project is installed with its extras into a fresh environment that contains
nothing else, and pip-audit inspects exactly that environment. Test and build
tooling is never audited: none of it ships.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import venv
from collections.abc import Sequence
from pathlib import Path

INSTALL_TARGET = ".[parquet]"


def _python(environment: Path) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return environment / directory / executable


def install_command(python: Path) -> list[str]:
    """Install into `python`'s environment using the interpreter that runs the tool."""
    return [
        sys.executable,
        "-m",
        "pip",
        "--python",
        str(python),
        "install",
        "--disable-pip-version-check",
        INSTALL_TARGET,
    ]


def audit_command(site_packages: Path, ignored: Sequence[str]) -> list[str]:
    command = [sys.executable, "-m", "pip_audit", "--path", str(site_packages)]
    for vulnerability in ignored:
        command += ["--ignore-vuln", vulnerability]
    return command


def _site_packages(python: Path) -> Path:
    result = subprocess.run(  # noqa: S603 - arguments are constructed, never shell-parsed
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def audit_runtime(repository: Path, ignored: Sequence[str]) -> int:
    with tempfile.TemporaryDirectory(prefix="hhe-audit-") as temporary:
        environment = Path(temporary).resolve() / "environment"
        # Without pip in the environment, pip itself is not part of what gets audited.
        venv.EnvBuilder(with_pip=False).create(environment)
        python = _python(environment)
        subprocess.run(  # noqa: S603 - arguments are constructed, never shell-parsed
            install_command(python), check=True, cwd=repository
        )
        site_packages = _site_packages(python)
        return subprocess.run(  # noqa: S603 - arguments are constructed, never shell-parsed
            audit_command(site_packages, ignored), check=False, cwd=repository
        ).returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ignore-vuln",
        action="append",
        default=[],
        metavar="ID",
        help="Vulnerability ID to ignore; name the reason in the commit that adds it.",
    )
    args = parser.parse_args(argv)
    return audit_runtime(Path(__file__).resolve().parents[1], args.ignore_vuln)


if __name__ == "__main__":
    raise SystemExit(main())
