"""``convert`` derives only from a proven source and never asks Home Assistant."""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.app.convert import ConvertRequest, run_convert
from ha_history_exporter.app.results import ConversionOutcome
from ha_history_exporter.archive.layout import ArchiveLayout
from ha_history_exporter.archive.store import load_manifest
from ha_history_exporter.domain.formats import Format
from ha_history_exporter.domain.manifest import Manifest14
from ha_history_exporter.domain.plan import DaySelection
from ha_history_exporter.domain.window import DayWindow
from ha_history_exporter.errors import ConfigError, ExitCode
from ha_history_exporter.settings import Config
from tests.support.builders import make_config
from tests.support.clock import FixedClock
from tests.support.harness import derive, produce
from tests.support.scenarios import DST_DAY, FAILED_DAY, NORMAL_DAY, REFERENCE

BERLIN = ZoneInfo("Europe/Berlin")
LATER = datetime(2026, 10, 27, 13, 0, tzinfo=BERLIN)
CSV = frozenset({"csv"})
LEGACY = Path(__file__).parents[1] / "contract" / "fixtures" / "legacy_manifests"


@pytest.fixture
def archive(tmp_path: Path) -> Config:
    produce(replace(REFERENCE, formats=frozenset({"jsonl"})), tmp_path)
    return make_config(tmp_path)


def layout(cfg: Config) -> ArchiveLayout:
    return ArchiveLayout(cfg.export.output_dir)


def manifest(cfg: Config, day: date) -> Manifest14:
    loaded = load_manifest(layout(cfg), DayWindow.full(day, BERLIN).identity())
    assert isinstance(loaded, Manifest14)
    return loaded


def convert(cfg: Config, selection: DaySelection | None = None) -> list[tuple[date, str]]:
    result = run_convert(
        ConvertRequest(frozenset({Format.CSV}), selection), cfg, clock=FixedClock(LATER)
    )
    return [(day.day, day.outcome.value) for day in result.days]


def test_the_manifest_gains_the_new_artifact_and_nothing_else(archive: Config) -> None:
    before = manifest(archive, NORMAL_DAY)
    result = derive(archive.export.output_dir.parent, CSV, now=LATER)
    after = manifest(archive, NORMAL_DAY)

    assert result.exit_code is ExitCode.OK
    assert set(after.artifacts) == {Format.JSONL, Format.CSV}
    assert replace(after, artifacts=before.artifacts) == before
    assert (
        after.artifacts[Format.CSV].logical_sha256 == before.artifacts[Format.JSONL].logical_sha256
    )
    assert result.log_file == layout(archive).log_file(LATER)
    assert not layout(archive).journal_root.exists()
    assert not layout(archive).staging_root.exists()


def test_a_selection_converts_only_its_days(archive: Config) -> None:
    assert convert(archive, DaySelection.single(DST_DAY)) == [(DST_DAY, "converted")]
    assert Format.CSV not in manifest(archive, NORMAL_DAY).artifacts


def test_a_source_that_differs_from_its_manifest_blocks_the_day(archive: Config) -> None:
    source = layout(archive).day_file(NORMAL_DAY, "jsonl")
    source.write_bytes(source.read_bytes() + b"\n")
    missing = layout(archive).day_file(DST_DAY, "jsonl")
    missing.unlink()

    result = run_convert(ConvertRequest(frozenset({Format.CSV})), archive, clock=FixedClock(LATER))

    assert [(day.outcome, day.reason) for day in result.days] == [
        (ConversionOutcome.BLOCKED, "the jsonl source differs from the manifest"),
        (ConversionOutcome.BLOCKED, "the jsonl source is missing"),
    ]
    assert result.exit_code is ExitCode.FAILED
    assert not layout(archive).day_file(NORMAL_DAY, "csv").exists()
    assert Format.CSV not in manifest(archive, NORMAL_DAY).artifacts


def test_an_undecodable_source_blocks_the_day(archive: Config) -> None:
    source = layout(archive).day_file(NORMAL_DAY, "jsonl")
    source.write_bytes(b"{not json}\n")
    result = run_convert(ConvertRequest(frozenset({Format.CSV})), archive, clock=FixedClock(LATER))
    assert result.days[0].outcome is ConversionOutcome.BLOCKED
    assert (result.days[0].reason or "").startswith("the jsonl source cannot be read")


def test_days_without_a_successful_manifest_are_skipped(tmp_path: Path) -> None:
    produce(FAILED_DAY, tmp_path)
    cfg = make_config(tmp_path)
    assert convert(cfg) == [(FAILED_DAY.days[0], "skipped")]


def test_legacy_days_are_blocked_and_never_rewritten(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    day = date(2026, 7, 28)
    target = layout(cfg)
    target.day_dir(day).mkdir(parents=True)
    legacy = target.manifest_file(day)
    shutil.copy2(LEGACY / "schema-1.3" / f"{day}.manifest.json", legacy)
    before = legacy.read_bytes()

    result = run_convert(ConvertRequest(frozenset({Format.CSV})), cfg, clock=FixedClock(LATER))

    assert result.days[0].outcome is ConversionOutcome.BLOCKED
    assert "schema 1.3" in (result.days[0].reason or "")
    assert legacy.read_bytes() == before
    assert target.descriptor_file.is_file()  # converting adopts the archive


def test_an_empty_archive_has_nothing_to_convert(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    assert convert(cfg) == []
    assert not cfg.export.output_dir.exists()


def test_no_format_is_a_configuration_error(archive: Config) -> None:
    with pytest.raises(ConfigError, match="at least one format"):
        run_convert(ConvertRequest(frozenset()), archive, clock=FixedClock(LATER))


def test_parquet_needs_pyarrow(archive: Config, without_pyarrow: None) -> None:
    with pytest.raises(ConfigError, match="pyarrow"):
        run_convert(ConvertRequest(frozenset({Format.PARQUET})), archive, clock=FixedClock(LATER))


@pytest.mark.parquet
def test_a_day_whose_only_source_cannot_be_read_here_is_blocked(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    pytest.importorskip("pyarrow")
    produce(replace(REFERENCE, formats=frozenset({"parquet"})), tmp_path)
    cfg = make_config(tmp_path)
    request.getfixturevalue("without_pyarrow")
    result = run_convert(ConvertRequest(frozenset({Format.CSV})), cfg, clock=FixedClock(LATER))
    assert {(day.outcome, day.reason) for day in result.days} == {
        (ConversionOutcome.BLOCKED, "no declared artifact can be read here")
    }
