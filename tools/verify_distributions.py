#!/usr/bin/env python3
"""Check the sdist's content and install and smoke-test the wheel of a distribution directory."""

from __future__ import annotations

import argparse
import subprocess
import tarfile
import tempfile
import venv
from pathlib import Path, PurePosixPath

from _venv import program

DISTRIBUTION_NAME = "ha-history-exporter"
PACKAGE_NAME = "ha_history_exporter"

#: Paths below the sdist root that it must contain.
SDIST_REQUIRED = (
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "pyproject.toml",
    "src/ha_history_exporter/__init__.py",
    "src/ha_history_exporter/schemas/manifest-1.4.schema.json",
    "tests/conftest.py",
    "tests/contract/golden",
)
#: Path prefixes below the sdist root that belong to the repository only.
SDIST_FORBIDDEN = (".github", "tools", "tests/repo", "requirements-dev.txt")


def _single(directory: Path, pattern: str) -> Path:
    found = sorted(directory.glob(pattern))
    if len(found) != 1:
        raise RuntimeError(f"Expected exactly one {pattern} in {directory}, found {len(found)}.")
    return found[0]


def sdist_problems(names: list[str]) -> list[str]:
    """What is missing from or does not belong into an sdist with these member names."""
    relative = {PurePosixPath(*PurePosixPath(name).parts[1:]).as_posix() for name in names}
    problems = [f"missing {path}" for path in SDIST_REQUIRED if path not in relative]
    problems += [
        f"contains {path}"
        for path in sorted(relative)
        if any(path == prefix or path.startswith(f"{prefix}/") for prefix in SDIST_FORBIDDEN)
    ]
    return problems


def verify_sdist(directory: Path) -> None:
    with tarfile.open(_single(directory, "*.tar.gz"), "r:gz") as archive:
        problems = sdist_problems(archive.getnames())
    if problems:
        raise RuntimeError("The sdist is not as expected: " + "; ".join(problems))


def _run(arguments: list[str], *, cwd: Path) -> str:
    return subprocess.run(  # noqa: S603 - arguments are constructed, never shell-parsed
        arguments,
        check=True,
        cwd=cwd,
        text=True,
        capture_output=True,
    ).stdout


def verify_wheel(directory: Path) -> None:
    wheel = _single(directory, "*.whl")
    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="hhe-wheel-") as temporary:
        root = Path(temporary).resolve()
        environment = root / "environment"
        workdir = root / "workdir"
        workdir.mkdir()
        venv.EnvBuilder(with_pip=True).create(environment)
        python = program(environment, "python")
        install = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]

        _run([*install, str(wheel)], cwd=workdir)
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
assert module.with_name("schemas").joinpath("manifest-1.4.schema.json").is_file()
"""
        _run([str(python), "-c", contract, str(repository)], cwd=workdir)
        for name in ("hhe", "ha-history-exporter"):
            shown = _run([str(program(environment, name)), "--version"], cwd=workdir)
            if not shown.startswith(f"{name} "):
                raise RuntimeError(f"{name} --version names itself {shown.strip()!r}.")
        shown = _run([str(python), "-m", PACKAGE_NAME, "--version"], cwd=workdir)
        if not shown.startswith(f"python -m {PACKAGE_NAME} "):
            raise RuntimeError(f"python -m {PACKAGE_NAME} --version shows {shown.strip()!r}.")

        _run([*install, f"{DISTRIBUTION_NAME}[parquet] @ {wheel.as_uri()}"], cwd=workdir)
        _run([str(python), "-c", "import pyarrow"], cwd=workdir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path("dist"),
        help="Directory with exactly one sdist and one wheel (default: dist).",
    )
    args = parser.parse_args()
    directory = args.directory.resolve()
    verify_sdist(directory)
    verify_wheel(directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
