from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.domain.formats import Format
from ha_history_exporter.domain.records import HistoryRecord, LogicalDigest
from ha_history_exporter.domain.stored import RecordContract
from ha_history_exporter.errors import ValidationError
from ha_history_exporter.formats import FORMATS
from ha_history_exporter.formats.validation import (
    CaptureSummary,
    inspect_artifact,
    validate_artifacts,
)
from tests.support.records import make_record

CONTRACT = RecordContract(
    requested_entity_ids=frozenset({"sensor.one", "sensor.two"}),
    start=datetime(2026, 7, 27, 22, tzinfo=UTC),
    end=datetime(2026, 7, 28, 22, tzinfo=UTC),
    allow_start_state=False,
    timezone=ZoneInfo("Europe/Berlin"),
)
RECORDS = [make_record(), make_record("sensor.two", "2")]


def _write(path: Path, fmt: Format, records: list[HistoryRecord]) -> Path:
    with FORMATS[fmt].open_writer(path) as writer:
        for record in records:
            writer.write(record)
    return path


def _summary(records: list[HistoryRecord]) -> CaptureSummary:
    digest = LogicalDigest()
    for record in records:
        digest.add(record)
    return CaptureSummary(rows=digest.rows, logical_sha256=digest.hexdigest())


@pytest.mark.parametrize("fmt", [Format.JSONL, Format.CSV], ids=str)
def test_inspection_hashes_the_bytes_on_disk(tmp_path: Path, fmt: Format) -> None:
    path = _write(tmp_path / f"day.{fmt.value}", fmt, RECORDS)
    metadata = inspect_artifact(path, fmt, contract=CONTRACT)
    assert metadata.filename == path.name
    assert metadata.size_bytes == path.stat().st_size
    assert metadata.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert metadata.row_count == 2
    assert metadata.logical_sha256 == _summary(RECORDS).logical_sha256


def test_parquet_inspection_hashes_the_bytes_on_disk(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    path = _write(tmp_path / "day.parquet", Format.PARQUET, RECORDS)
    metadata = inspect_artifact(path, Format.PARQUET, contract=CONTRACT)
    assert metadata.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert metadata.logical_sha256 == _summary(RECORDS).logical_sha256


def test_every_artifact_must_hold_the_captured_records(tmp_path: Path) -> None:
    jsonl = _write(tmp_path / "day.jsonl", Format.JSONL, RECORDS)
    csv = _write(tmp_path / "day.csv", Format.CSV, RECORDS)
    artifacts = validate_artifacts(
        {Format.JSONL: jsonl, Format.CSV: csv}, contract=CONTRACT, expected=_summary(RECORDS)
    )
    assert set(artifacts) == {Format.JSONL, Format.CSV}
    assert artifacts[Format.JSONL].logical_sha256 == artifacts[Format.CSV].logical_sha256


def test_a_missing_row_is_a_count_mismatch(tmp_path: Path) -> None:
    path = _write(tmp_path / "day.csv", Format.CSV, RECORDS[:1])
    with pytest.raises(
        ValidationError, match=re.escape("CSV row count mismatch in day.csv: expected 2")
    ):
        validate_artifacts({Format.CSV: path}, contract=CONTRACT, expected=_summary(RECORDS))


def test_a_changed_value_is_a_digest_mismatch(tmp_path: Path) -> None:
    changed = [RECORDS[0], replace(RECORDS[1], state="other")]
    path = _write(tmp_path / "day.jsonl", Format.JSONL, changed)
    with pytest.raises(
        ValidationError, match=re.escape("day.jsonl does not hold the captured records")
    ):
        validate_artifacts({Format.JSONL: path}, contract=CONTRACT, expected=_summary(RECORDS))


def test_records_outside_the_request_are_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "day.jsonl", Format.JSONL, [make_record("sensor.foreign")])
    with pytest.raises(ValidationError, match=r"day\.jsonl, record 1: .* not requested"):
        inspect_artifact(path, Format.JSONL, contract=CONTRACT)


def test_missing_and_undecodable_files_are_validation_errors(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match=re.escape("JSONL file missing.jsonl does not exist")):
        inspect_artifact(tmp_path / "missing.jsonl", Format.JSONL)
    broken = tmp_path / "broken.jsonl"
    broken.write_bytes(b'{"entity_id": "\xff"}\n')
    with pytest.raises(ValidationError, match=re.escape("broken.jsonl is not valid UTF-8")):
        inspect_artifact(broken, Format.JSONL)


def test_an_unreadable_path_is_a_validation_error(tmp_path: Path) -> None:
    directory = tmp_path / "day.jsonl"
    directory.mkdir()
    with pytest.raises(ValidationError, match=re.escape("Cannot read JSONL file day.jsonl")):
        inspect_artifact(directory, Format.JSONL)
