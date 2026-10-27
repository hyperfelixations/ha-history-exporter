"""The archive's metadata directory: entity snapshots and the run log."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import fsio
from .layout import ArchiveLayout


def write_snapshot(
    layout: ArchiveLayout, snapshot: Mapping[str, Any], created_local: datetime, run_id: str
) -> Path:
    path = layout.snapshot_file(created_local, run_id)
    fsio.atomic_write(path, json.dumps(snapshot, indent=2, ensure_ascii=False).encode("utf-8"))
    return path


def append_run_log(layout: ArchiveLayout, entry: Mapping[str, Any]) -> None:
    """Append one JSON line; a truncated previous line is terminated first."""
    path = layout.run_log
    line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with path.open("rb") as handle:
            handle.seek(-1, 2)
            if handle.read(1) != b"\n":
                line = b"\n" + line
    except OSError:  # missing or empty: nothing to terminate
        pass
    fsio.append_durable(path, line)
