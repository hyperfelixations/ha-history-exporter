#!/usr/bin/env python3
"""Install and smoke-test the single wheel in a distribution directory."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import venv
from pathlib import Path

DISTRIBUTION_NAME = "ha-history-exporter"
PACKAGE_NAME = "ha_history_exporter"


def _python(environment: Path) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return environment / directory / executable


def _entrypoint(environment: Path, name: str) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return environment / directory / f"{name}{suffix}"


def _run(arguments: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - arguments are constructed, never shell-parsed
        arguments,
        check=True,
        cwd=cwd,
        text=True,
    )


def verify_wheel(directory: Path) -> None:
    distribution_directory = directory.resolve()
    wheels = sorted(distribution_directory.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(
            f"Expected exactly one wheel in {distribution_directory}, found {len(wheels)}."
        )

    wheel = wheels[0]
    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="hhe-wheel-") as temporary:
        root = Path(temporary).resolve()
        environment = root / "environment"
        workdir = root / "workdir"
        workdir.mkdir()
        venv.EnvBuilder(with_pip=True).create(environment)
        python = _python(environment)

        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                str(wheel),
            ],
            cwd=workdir,
        )

        contract = """
from importlib import metadata
from pathlib import Path
import sys

import ha_history_exporter

repository = Path(sys.argv[1]).resolve()
module = Path(ha_history_exporter.__file__).resolve()
environment = Path(sys.prefix).resolve()
assert metadata.version("ha-history-exporter") == ha_history_exporter.__version__
assert module.is_relative_to(environment), (module, environment)
assert not module.is_relative_to(repository), (module, repository)
assert module.with_name("py.typed").is_file()
"""
        _run([str(python), "-c", contract, str(repository)], cwd=workdir)
        _run([str(python), "-m", PACKAGE_NAME, "--version"], cwd=workdir)
        _run([str(_entrypoint(environment, "hhe")), "--version"], cwd=workdir)
        _run(
            [str(_entrypoint(environment, "ha-history-exporter")), "--version"],
            cwd=workdir,
        )

        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                f"{DISTRIBUTION_NAME}[parquet] @ {wheel.as_uri()}",
            ],
            cwd=workdir,
        )
        _run([str(python), "-c", "import pyarrow"], cwd=workdir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path("dist"),
        help="Directory containing exactly one wheel (default: dist).",
    )
    args = parser.parse_args()
    verify_wheel(args.directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
