"""Every format returns exactly the records written to it."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ha_history_exporter.domain.formats import Format
from ha_history_exporter.domain.records import HistoryRecord, LogicalDigest
from ha_history_exporter.formats import FORMATS
from ha_history_exporter.formats.validation import inspect_artifact
from tests.support.records import records

TEXT_FORMATS = [Format.JSONL, Format.CSV]


def _round_trip(fmt: Format, batch: list[HistoryRecord]) -> tuple[list[HistoryRecord], str, str]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"day.{fmt.value}"
        with FORMATS[fmt].open_writer(path) as writer:
            for record in batch:
                writer.write(record)
        with path.open("rb") as stream:
            read_back = list(FORMATS[fmt].read(stream, path.name))
        digest = LogicalDigest()
        for record in batch:
            digest.add(record)
        return read_back, digest.hexdigest(), inspect_artifact(path, fmt).logical_sha256


@pytest.mark.parametrize("fmt", TEXT_FORMATS, ids=str)
@settings(max_examples=60, suppress_health_check=[HealthCheck.too_slow])
@given(batch=st.lists(records(), max_size=5))
def test_text_formats_round_trip(fmt: Format, batch: list[HistoryRecord]) -> None:
    read_back, written, inspected = _round_trip(fmt, batch)
    assert read_back == batch
    assert inspected == written


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(batch=st.lists(records(), max_size=5))
def test_parquet_round_trips(batch: list[HistoryRecord]) -> None:
    pytest.importorskip("pyarrow")
    read_back, written, inspected = _round_trip(Format.PARQUET, batch)
    assert read_back == batch
    assert inspected == written
