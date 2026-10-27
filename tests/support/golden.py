"""Golden fixtures: what an export produced, reduced to its contract.

Each scenario owns ``tests/contract/golden/<scenario>/``, which mirrors the
contract part of the output directory: every day file below ``exports/`` and
``metadata/export_runs.jsonl``. Text artifacts are stored as produced; values
that depend on the moment of the run or on the Parquet encoder are replaced by
``VOLATILE``. A Parquet file is stored as its semantic profile.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

GOLDEN_ROOT = Path(__file__).resolve().parents[1] / "contract" / "golden"

VOLATILE = "<volatile>"
VOLATILE_MANIFEST_FIELDS = (
    "export_started_at",
    "export_finished_at",
    "duration_seconds",
    "script_version",
)
VOLATILE_RUN_LOG_FIELDS = ("started_at", "finished_at", "duration_seconds")

#: Artifacts whose bytes depend on the encoder version. Their size and SHA-256
#: are verified against the produced file instead.
ENCODER_DEPENDENT_ARTIFACTS = frozenset({"parquet"})

RUN_LOG = "metadata/export_runs.jsonl"
SNAPSHOT_GOLDEN = GOLDEN_ROOT / "snapshot" / "entity_snapshot.json"
PROFILE_SUFFIX = ".profile.json"


def contract_files(output_dir: Path) -> dict[str, Path]:
    """Every produced file that belongs to the output contract, by relative path."""
    files = {
        path.relative_to(output_dir).as_posix(): path
        for path in sorted((output_dir / "exports").rglob("*"))
        if path.is_file()
    }
    run_log = output_dir / RUN_LOG
    if run_log.is_file():
        files[RUN_LOG] = run_log
    return files


def text_of(path: Path) -> str:
    """Decode a JSON or JSONL artifact; its line terminator is LF only."""
    raw = path.read_bytes()
    assert b"\r" not in raw, f"{path.name} contains a carriage return"
    return raw.decode("utf-8")


def stable_manifest(text: str) -> str:
    """A manifest with its volatile values replaced, in its original key order."""
    data = json.loads(text)
    for name in VOLATILE_MANIFEST_FIELDS:
        data[name] = VOLATILE
    for name in ENCODER_DEPENDENT_ARTIFACTS:
        artifact = data["artifacts"].get(name)
        if artifact is not None:
            artifact["size_bytes"] = VOLATILE
            artifact["sha256"] = VOLATILE
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def stable_run_log(text: str) -> str:
    lines = []
    for line in text.splitlines():
        entry = json.loads(line)
        for name in VOLATILE_RUN_LOG_FIELDS:
            entry[name] = VOLATILE
        lines.append(json.dumps(entry, ensure_ascii=False))
    return "\n".join(lines) + "\n"


def stable_snapshot(text: str) -> str:
    data = json.loads(text)
    data["created_at"] = VOLATILE
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def parquet_profile(path: Path) -> dict[str, Any]:
    """Schema, row-group layout, codec and every value of a Parquet file."""
    import pyarrow.parquet as pq

    handle = pq.ParquetFile(path)
    table = handle.read()
    rows = [
        {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in row.items()
        }
        for row in table.to_pylist()
    ]
    return {
        "schema": [[field.name, str(field.type), field.nullable] for field in table.schema],
        "num_row_groups": handle.num_row_groups,
        "num_rows": table.num_rows,
        "compression": handle.metadata.row_group(0).column(0).compression.lower(),
        "rows": rows,
    }


def golden_view(output_dir: Path) -> dict[str, bytes]:
    """The produced contract files as they are stored in a golden directory."""
    view: dict[str, bytes] = {}
    for relative, path in contract_files(output_dir).items():
        if relative == RUN_LOG:
            view[relative] = stable_run_log(text_of(path)).encode("utf-8")
        elif relative.endswith(".manifest.json"):
            view[relative] = stable_manifest(text_of(path)).encode("utf-8")
        elif relative.endswith(".jsonl"):
            view[relative] = text_of(path).encode("utf-8")
        elif relative.endswith(".parquet"):
            profile = json.dumps(parquet_profile(path), indent=2, ensure_ascii=False) + "\n"
            view[relative + PROFILE_SUFFIX] = profile.encode("utf-8")
        else:
            view[relative] = path.read_bytes()
    return view


def load_golden(scenario: str) -> dict[str, bytes]:
    root = GOLDEN_ROOT / scenario
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def write_golden(scenario: str, output_dir: Path) -> list[Path]:
    """Replace the golden directory of *scenario* with the produced view."""
    root = GOLDEN_ROOT / scenario
    shutil.rmtree(root, ignore_errors=True)
    written = []
    for relative, data in golden_view(output_dir).items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(target)
    return written


def write_snapshot_golden(snapshot: Path) -> Path:
    SNAPSHOT_GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_GOLDEN.write_bytes(stable_snapshot(text_of(snapshot)).encode("utf-8"))
    return SNAPSHOT_GOLDEN
