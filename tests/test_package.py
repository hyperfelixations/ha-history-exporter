from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from ha_history_exporter import __version__
from ha_history_exporter.manifest import SCHEMA_VERSION, SCRIPT_VERSION

ROOT = Path(__file__).parents[1]

#: Repository files outside the package that the test suite reads.
SUITE_INPUTS = (
    ".gitattributes",
    ".gitignore",
    ".github/workflows/release.yml",
    ".github/workflows/tests.yml",
    "tools/refresh_golden.py",
)


def test_version_constants_are_consistent():
    assert __version__ == "1.4.0"
    assert __version__ == SCRIPT_VERSION
    assert SCHEMA_VERSION == "1.3"


def manifest_covers(manifest: str, relative_path: str) -> bool:
    """Would MANIFEST.in put `relative_path` into the source distribution?"""
    for line in manifest.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "include" and relative_path in parts[1:]:
            return True
        if parts[0] == "recursive-include" and len(parts) >= 3:
            directory = parts[1].replace("\\", "/").rstrip("/")
            prefix = f"{directory}/"
            if relative_path.startswith(prefix):
                name = relative_path[len(prefix):]
                if any(fnmatch(name, pattern) for pattern in parts[2:]):
                    return True
    return False


def test_the_source_distribution_ships_what_the_test_suite_reads():
    """MANIFEST.in promises an sdist that can be verified on its own.

    Several tests read repository files that live outside the package. Left
    out of the distribution, they turn into failures for anyone who builds
    from the sdist instead of the checkout.
    """
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    for relative_path in SUITE_INPUTS:
        assert (ROOT / relative_path).exists(), f"{relative_path} is missing"
        assert manifest_covers(manifest, relative_path), (
            f"MANIFEST.in does not ship {relative_path}, so the test suite "
            "cannot run from an unpacked source distribution"
        )
