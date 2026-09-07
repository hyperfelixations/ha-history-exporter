#!/usr/bin/env python3
"""Regenerate the golden fixtures in tests/golden/.

The fixtures record what an export run actually produces. Regenerating them
declares that the produced files changed on purpose, so run this only after
that change was reviewed, and inspect the resulting diff line by line before
committing it.

    python tools/refresh_golden.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import test_output_contract as contract  # noqa: E402


def main() -> int:
    contract.GOLDEN.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="hhe-golden-"))
    try:
        output = contract.produce(workdir)

        for day in (contract.NORMAL_DAY, contract.DST_DAY):
            # JSONL is stored with LF: its terminator is os.linesep and would
            # otherwise make the fixture depend on the generating platform. CSV
            # is stored exactly as produced, because the csv dialect writes CRLF
            # everywhere.
            _write(
                contract.GOLDEN / f"{day}.jsonl",
                contract.day_file(output, day, "jsonl")
                .read_bytes()
                .replace(b"\r\n", b"\n"),
            )
            _write(
                contract.GOLDEN / f"{day}.csv",
                contract.day_file(output, day, "csv").read_bytes(),
            )

            _dump(
                contract.GOLDEN / f"{day}.parquet.json",
                contract.parquet_profile(contract.day_file(output, day, "parquet")),
            )
            _dump(
                contract.GOLDEN / f"{day}.manifest.json",
                contract.stable_manifest(
                    contract.day_file(output, day, "manifest.json")
                ),
            )

        _dump(
            contract.GOLDEN / "export_runs.json",
            contract.stable_run_log(output / "metadata" / "export_runs.jsonl"),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\nInspect the diff before committing:  git diff tests/golden/")
    return 0


def _dump(path: Path, payload: object) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
    _write(path, (text + "\n").encode("utf-8"))


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    print(f"wrote {path.name} ({len(data)} bytes)")


if __name__ == "__main__":
    raise SystemExit(main())
