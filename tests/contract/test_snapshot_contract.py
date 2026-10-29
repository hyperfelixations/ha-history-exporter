"""The entity snapshot: file name pattern and content shape."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.support import golden
from tests.support.harness import produce_snapshot
from tests.support.scenarios import SNAPSHOT_STATES

#: ``entity_snapshot_<local YYYYMMDDTHHMMSS>_<run id>.json``; the run id is
#: ``<UTC YYYYMMDDTHHMMSS>-<pid>-<six lowercase letters or digits>``.
SNAPSHOT_NAME = re.compile(r"entity_snapshot_\d{8}T\d{6}_\d{8}T\d{6}-\d+-[a-z0-9]{6}\.json")


def test_snapshot_name_and_content_match_the_contract(tmp_path: Path) -> None:
    path = produce_snapshot(SNAPSHOT_STATES, tmp_path / "output")

    assert path.parent == tmp_path / "output" / "metadata"
    assert SNAPSHOT_NAME.fullmatch(path.name), path.name

    text = golden.text_of(path)
    assert text == json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    assert golden.stable_snapshot(text) == golden.SNAPSHOT_GOLDEN.read_text("utf-8")
