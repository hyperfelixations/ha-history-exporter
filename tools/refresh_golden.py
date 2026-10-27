#!/usr/bin/env python3
"""Regenerate the golden fixtures in tests/contract/golden/.

Regenerating declares that the produced output changed on purpose. Run it only
for a reviewed change and inspect the resulting diff file by file before
committing it.

    python tools/refresh_golden.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.support import golden  # noqa: E402
from tests.support.harness import produce, produce_snapshot  # noqa: E402
from tests.support.scenarios import SCENARIOS, SNAPSHOT_STATES  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="hhe-golden-") as workdir:
        for scenario in SCENARIOS:
            production = produce(scenario, Path(workdir) / scenario.name)
            for path in golden.write_golden(scenario.name, production.output_dir):
                print(f"wrote {path.relative_to(ROOT).as_posix()}")
        snapshot = produce_snapshot(SNAPSHOT_STATES, Path(workdir) / "snapshot")
        path = golden.write_snapshot_golden(snapshot)
        print(f"wrote {path.relative_to(ROOT).as_posix()}")
    print("\nInspect the diff before committing:  git diff tests/contract/golden/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
