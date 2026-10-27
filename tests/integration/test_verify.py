"""``verify`` and ``status`` read an archive as it is, without the lock."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.app.results import ArtifactStatus, DayState
from ha_history_exporter.app.status import archive_status
from ha_history_exporter.app.verify import Depth, verify_archive
from ha_history_exporter.archive.layout import ArchiveLayout
from ha_history_exporter.archive.lock import FileLock
from ha_history_exporter.domain.formats import Format
from ha_history_exporter.domain.plan import DaySelection
from ha_history_exporter.errors import ExitCode
from ha_history_exporter.settings import Config
from tests.support.builders import make_config
from tests.support.clock import FixedClock
from tests.support.harness import produce
from tests.support.scenarios import DST_DAY, NORMAL_DAY, REFERENCE

BERLIN = ZoneInfo("Europe/Berlin")
LATER = FixedClock(datetime(2026, 10, 27, 13, 0, tzinfo=BERLIN))
LEGACY = Path(__file__).parents[1] / "contract" / "fixtures" / "legacy_manifests"


@pytest.fixture
def archive(tmp_path: Path) -> Config:
    """The reference days in JSONL and CSV, without hhe-archive.json's help."""
    produce(replace(REFERENCE, formats=frozenset({"jsonl", "csv"})), tmp_path)
    return make_config(tmp_path)


def layout(cfg: Config) -> ArchiveLayout:
    return ArchiveLayout(cfg.export.output_dir)


def statuses(cfg: Config, depth: Depth = Depth.CHECKSUMS) -> dict[tuple[date, str], str]:
    result = verify_archive(cfg, None, depth=depth, clock=LATER)
    return {
        (day.day, check.format.value): check.status.value
        for day in result.days
        for check in day.artifacts
    }


def test_an_intact_archive_verifies(archive: Config) -> None:
    result = verify_archive(archive, None, depth=Depth.CHECKSUMS, clock=LATER)
    assert result.exit_code is ExitCode.OK
    assert [day.day for day in result.days] == [NORMAL_DAY, DST_DAY]
    assert result.count(ArtifactStatus.OK) == 4
    assert result.archive.described
    assert result.archive.pending_transactions == 0
    assert result.archive.running is None


@pytest.mark.parametrize("depth", [Depth.CHECKSUMS, Depth.DECODED])
def test_a_changed_byte_is_a_mismatch(archive: Config, depth: Depth) -> None:
    path = layout(archive).day_file(NORMAL_DAY, "csv")
    data = bytearray(path.read_bytes())
    data[-3] ^= 1
    path.write_bytes(bytes(data))
    assert statuses(archive, depth)[(NORMAL_DAY, "csv")] in {"mismatch", "unreadable"}
    assert verify_archive(archive, None, depth=depth, clock=LATER).exit_code is ExitCode.FAILED


def test_decoding_finds_what_a_matching_checksum_cannot(archive: Config) -> None:
    """A manifest rewritten to match edited records still fails the logical digest."""
    path = layout(archive).day_file(NORMAL_DAY, "jsonl")
    edited = path.read_bytes().replace(b'"state": "21.5"', b'"state": "99.9"', 1)
    assert edited != path.read_bytes()
    path.write_bytes(edited)
    manifest_path = layout(archive).manifest_file(NORMAL_DAY)
    manifest = json.loads(manifest_path.read_bytes())
    manifest["artifacts"]["jsonl"]["sha256"] = hashlib.sha256(edited).hexdigest()
    manifest["artifacts"]["jsonl"]["size_bytes"] = len(edited)
    manifest_path.write_bytes(json.dumps(manifest, indent=2, ensure_ascii=False).encode())

    assert statuses(archive)[(NORMAL_DAY, "jsonl")] == "ok"
    assert statuses(archive, Depth.DECODED)[(NORMAL_DAY, "jsonl")] == "mismatch"


def test_a_missing_file_is_reported_but_allowed(archive: Config) -> None:
    layout(archive).day_file(DST_DAY, "csv").unlink()
    result = verify_archive(archive, None, depth=Depth.CHECKSUMS, clock=LATER)
    assert statuses(archive)[(DST_DAY, "csv")] == "missing"
    assert result.exit_code is ExitCode.OK


def test_a_file_no_manifest_declares_is_unexpected(archive: Config) -> None:
    """A Parquet file of an earlier generation, and a day file without any manifest."""
    target = layout(archive)
    stale = target.day_file(NORMAL_DAY, "parquet")
    stale.write_bytes(b"left over")
    orphan = target.day_file(date(2026, 10, 20), "jsonl")
    orphan.write_bytes(b"")
    (target.day_dir(NORMAL_DAY) / "notes.csv").write_text("mine", encoding="utf-8")

    result = verify_archive(archive, None, depth=Depth.CHECKSUMS, clock=LATER)

    found = statuses(archive)
    assert found[(NORMAL_DAY, "parquet")] == "unexpected"
    assert found[(date(2026, 10, 20), "jsonl")] == "unexpected"
    assert len(found) == 6
    assert result.exit_code is ExitCode.FAILED


def test_an_invalid_manifest_fails_its_day_only(archive: Config) -> None:
    layout(archive).manifest_file(DST_DAY).write_bytes(b"{")
    result = verify_archive(archive, None, depth=Depth.CHECKSUMS, clock=LATER)
    assert result.invalid_days == [DST_DAY]
    assert result.count(ArtifactStatus.OK) == 2
    assert result.exit_code is ExitCode.FAILED


def test_a_selection_limits_the_days(archive: Config) -> None:
    result = verify_archive(
        archive, DaySelection.single(DST_DAY), depth=Depth.CHECKSUMS, clock=LATER
    )
    assert [day.day for day in result.days] == [DST_DAY]


def test_legacy_manifests_are_found_present_and_counted_when_decoded(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    target = layout(cfg)
    day = date(2026, 7, 28)
    target.day_dir(day).mkdir(parents=True)
    shutil.copy2(LEGACY / "schema-1.3" / f"{day}.manifest.json", target.manifest_file(day))
    rows = [
        {
            "entity_id": "sensor.legacy",
            "state": str(n),
            "last_changed": f"2026-07-28T1{n}:00:00+00:00",
            "last_updated": f"2026-07-28T1{n}:00:00+00:00",
            "attributes": {},
            "local_offset": "+02:00",
        }
        for n in range(4)
    ]
    jsonl = target.day_file(day, "jsonl")
    jsonl.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def found(depth: Depth) -> dict[str, ArtifactStatus]:
        result = verify_archive(cfg, None, depth=depth, clock=LATER)
        assert result.days[0].schema_version == "1.3"
        assert not result.archive.described
        return {check.format.value: check.status for check in result.days[0].artifacts}

    assert found(Depth.CHECKSUMS) == {
        "jsonl": ArtifactStatus.PRESENT,
        "parquet": ArtifactStatus.MISSING,
    }
    assert found(Depth.DECODED)["jsonl"] is ArtifactStatus.OK
    jsonl.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    assert found(Depth.DECODED)["jsonl"] is ArtifactStatus.MISMATCH


def test_reading_neither_locks_nor_recovers_nor_adopts(archive: Config) -> None:
    target = layout(archive)
    target.descriptor_file.unlink()
    journal = target.journal_root / "run-2026-10-24.json"
    journal.parent.mkdir()
    journal.write_text("{}", encoding="utf-8")
    with FileLock(target.lock_file, "run_id: other\npid: 1\n"):
        result = verify_archive(archive, None, depth=Depth.CHECKSUMS, clock=LATER)
        status = archive_status(archive, None, clock=LATER)

    for state in (result.archive, status.archive):
        assert state.running == "run_id: other, pid: 1"
        assert state.pending_transactions == 1
        assert not state.described
        assert state.zone is not None
        assert state.zone.zone == BERLIN
    assert journal.exists()
    assert not target.descriptor_file.exists()


def test_status_lists_every_day_from_the_first_to_the_last(archive: Config) -> None:
    target = layout(archive)
    first = date(2026, 10, 20)
    target.day_dir(first).mkdir(parents=True, exist_ok=True)
    shutil.copy2(target.manifest_file(NORMAL_DAY), target.manifest_file(first))

    result = archive_status(archive, None, clock=LATER)

    states = {status.day: status.state for status in result.days}
    assert states[first] is DayState.INVALID  # its manifest names another day
    assert [states[date(2026, 10, n)] for n in (21, 22, 23)] == [DayState.MISSING] * 3
    assert states[NORMAL_DAY] is DayState.OK
    assert max(states) == DST_DAY
    assert result.exit_code is ExitCode.OK
    assert [month for month, _ in result.months()] == ["2026-10"]


def test_status_of_a_selection_shows_its_gaps(archive: Config) -> None:
    result = archive_status(
        archive, DaySelection(start=date(2026, 10, 23), end=date(2026, 10, 26)), clock=LATER
    )
    assert [status.state for status in result.days] == [
        DayState.MISSING,
        DayState.OK,
        DayState.OK,
        DayState.MISSING,
    ]


def test_status_of_an_empty_archive_is_empty(tmp_path: Path) -> None:
    result = archive_status(make_config(tmp_path), None, clock=LATER)
    assert result.days == ()
    assert result.to_json()["start"] is None


def test_the_json_documents_name_their_command_and_exit_code(archive: Config) -> None:
    result = verify_archive(archive, None, depth=Depth.CHECKSUMS, clock=LATER)
    document = json.loads(json.dumps(result.to_json()))
    assert document["command"] == "verify"
    assert document["exit_code"] == 0
    assert document["summary"]["ok"] == 4
    assert document["days"][0]["artifacts"][0]["format"] == Format.JSONL.value
    status = archive_status(archive, None, clock=LATER).to_json()
    assert status["months"] == [
        {
            "month": "2026-10",
            "ok": ["2026-10-24", "2026-10-25"],
            "partial": [],
            "failed": [],
            "pending": [],
            "invalid": [],
            "missing": [],
        }
    ]
