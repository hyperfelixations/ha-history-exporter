"""Programs inside a virtual environment, on every platform."""

from __future__ import annotations

import os
from pathlib import Path


def program(environment: Path, name: str) -> Path:
    """The interpreter (``python``) or an installed console program of *environment*."""
    if os.name == "nt":
        return environment / "Scripts" / f"{name}.exe"
    return environment / "bin" / name
