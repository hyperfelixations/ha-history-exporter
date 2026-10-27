"""Formats that ``convert`` derives are the formats an export writes, byte for byte.

An archive exported in one format and converted into the others must equal
the reference golden of an export into all three, manifests included: the
manifest gains the new artifacts and changes in nothing else.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.app.results import ArtifactStatus, ConversionOutcome
from ha_history_exporter.app.verify import Depth, verify_archive
from tests.helpers import make_config
from tests.support import golden
from tests.support.clock import FixedClock
from tests.support.harness import derive, produce
from tests.support.scenarios import REFERENCE

pytestmark = pytest.mark.parquet

LATER = datetime(2026, 10, 27, 13, 0, tzinfo=ZoneInfo("Europe/Berlin"))
ALL = frozenset({"jsonl", "csv", "parquet"})


@pytest.mark.parametrize("source", sorted(ALL))
def test_a_converted_archive_equals_the_reference_export(tmp_path: Path, source: str) -> None:
    pytest.importorskip("pyarrow")
    production = produce(replace(REFERENCE, formats=frozenset({source})), tmp_path)

    result = derive(tmp_path, ALL - {source}, now=LATER)

    assert [day.outcome for day in result.days] == [ConversionOutcome.CONVERTED] * 2
    assert golden.golden_view(production.output_dir) == golden.load_golden(REFERENCE.name)
    verified = verify_archive(
        make_config(tmp_path), None, depth=Depth.DECODED, clock=FixedClock(LATER)
    )
    statuses = {check.status for day in verified.days for check in day.artifacts}
    assert statuses == {ArtifactStatus.OK}


def test_a_second_conversion_has_nothing_to_do(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    production = produce(replace(REFERENCE, formats=frozenset({"jsonl"})), tmp_path)
    derive(tmp_path, ALL, now=LATER)
    before = golden.golden_view(production.output_dir)

    again = derive(tmp_path, ALL, now=LATER)

    assert [day.outcome for day in again.days] == [ConversionOutcome.PRESENT] * 2
    assert golden.golden_view(production.output_dir) == before
