"""The archive descriptor: one time zone per archive, recorded once."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.archive import descriptor
from ha_history_exporter.archive.descriptor import (
    CREATED_BY,
    ArchiveDescriptor,
    ZoneSource,
    determine,
    establish,
    load,
)
from ha_history_exporter.archive.layout import ArchiveLayout
from ha_history_exporter.errors import ConfigError, ExportError
from tests.support.manifests import DAY, manifest14, write_manifest

BERLIN = ZoneInfo("Europe/Berlin")
NEW_YORK = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 27, 20, 15, 30, tzinfo=UTC)
LEGACY = Path(__file__).parents[2] / "contract" / "fixtures" / "legacy_manifests"


@pytest.fixture
def layout(tmp_path: Path) -> ArchiveLayout:
    return ArchiveLayout(tmp_path / "archive")


def write(layout: ArchiveLayout, data: object) -> None:
    layout.root.mkdir(parents=True, exist_ok=True)
    layout.descriptor_file.write_text(json.dumps(data), encoding="utf-8")


def test_a_descriptor_round_trips(layout: ArchiveLayout) -> None:
    assert load(layout) is None
    written = establish(layout, None, configured=None, home_assistant=BERLIN, now=NOW)
    assert written == ArchiveDescriptor(BERLIN, "2026-10-27T21:15:30+01:00", CREATED_BY)
    assert load(layout) == written
    assert layout.descriptor_file.read_bytes() == (
        b'{\n  "format_version": 1,\n  "timezone": "Europe/Berlin",\n'
        b'  "created_at": "2026-10-27T21:15:30+01:00",\n'
        b'  "created_by": "' + CREATED_BY.encode() + b'"\n}\n'
    )


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "top level must be an object"),
        ({"format_version": 1, "timezone": "Europe/Berlin"}, "fields are not the expected"),
        ({"format_version": 0, "timezone": "UTC", "created_at": "x", "created_by": "y"}, "fields"),
        ({"format_version": 1, "timezone": 1, "created_at": "x", "created_by": "y"}, "strings"),
        (
            {"format_version": 1, "timezone": "Mars/Olympus", "created_at": "x", "created_by": "y"},
            "not known on this machine",
        ),
    ],
)
def test_an_unusable_descriptor_stops_every_writer(
    layout: ArchiveLayout, data: object, message: str
) -> None:
    write(layout, data)
    with pytest.raises(ExportError, match=message):
        load(layout)


def test_a_descriptor_that_is_not_json_is_invalid(layout: ArchiveLayout) -> None:
    layout.root.mkdir(parents=True)
    layout.descriptor_file.write_bytes(b"{")
    with pytest.raises(ExportError, match="not valid JSON"):
        load(layout)


def test_a_newer_archive_format_is_refused(layout: ArchiveLayout) -> None:
    write(layout, {"format_version": 2, "timezone": "UTC"})
    with pytest.raises(ExportError, match="newer version of HHE") as caught:
        load(layout)
    assert caught.value.remedies[0].command == "pipx upgrade ha-history-exporter"


def test_a_new_archive_takes_the_configured_zone_before_home_assistants(
    layout: ArchiveLayout,
) -> None:
    configured = determine(layout, None, configured=NEW_YORK, home_assistant=BERLIN)
    assert (configured.zone, configured.source) == (NEW_YORK, ZoneSource.CONFIGURED)
    from_ha = determine(layout, None, configured=None, home_assistant=BERLIN)
    assert (from_ha.zone, from_ha.source) == (BERLIN, ZoneSource.HOME_ASSISTANT)
    assert not layout.root.exists()


def test_an_existing_archive_is_adopted_from_all_its_manifests(layout: ArchiveLayout) -> None:
    for schema, day in (("1.1", DAY), ("1.2", DAY), ("1.3", DAY)):
        target = layout.manifest_file(day).with_name(f"{day}.manifest.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LEGACY / f"schema-{schema}" / f"{day}.manifest.json", target)
    write_manifest(layout.root, manifest14(date(2026, 7, 29)))
    archive = determine(layout, None, configured=None, home_assistant=NEW_YORK)
    assert (archive.zone, archive.source, archive.adopted_days) == (
        BERLIN,
        ZoneSource.ADOPTED,
        2,
    )
    written = establish(layout, None, configured=None, home_assistant=NEW_YORK, now=NOW)
    assert written.timezone == BERLIN


def test_an_archive_of_several_zones_is_not_adopted(layout: ArchiveLayout) -> None:
    write_manifest(layout.root, manifest14(DAY))
    write_manifest(layout.root, manifest14(date(2026, 7, 29), timezone="America/New_York"))
    with pytest.raises(ExportError, match="more than one time zone") as caught:
        determine(layout, None, configured=None, home_assistant=BERLIN)
    assert "America/New_York (1 days), Europe/Berlin (1 days)" in (caught.value.details or "")


def test_an_invalid_manifest_stops_the_adoption(layout: ArchiveLayout) -> None:
    write_manifest(layout.root, manifest14(DAY)).write_bytes(b'{"timezone": "Europe/Berlin"}')
    with pytest.raises(ExportError, match="manifest for 2026-07-28 is invalid"):
        determine(layout, None, configured=None, home_assistant=BERLIN)
    write_manifest(layout.root, manifest14(DAY)).write_bytes(b"{}")
    with pytest.raises(ExportError, match="names no time zone"):
        determine(layout, None, configured=None, home_assistant=BERLIN)


def test_files_that_are_not_day_manifests_are_not_surveyed(layout: ArchiveLayout) -> None:
    month = layout.day_dir(DAY)
    month.mkdir(parents=True)
    (month / "notes.manifest.json").write_text("{", encoding="utf-8")
    (month / "2026-08-01.manifest.json").write_text("{", encoding="utf-8")
    assert descriptor.survey(layout) == {}


@pytest.mark.parametrize("recorded", [True, False])
def test_a_configured_zone_must_be_the_archives(layout: ArchiveLayout, recorded: bool) -> None:
    write_manifest(layout.root, manifest14(DAY))
    existing = establish(layout, None, configured=None, home_assistant=BERLIN, now=NOW)
    if not recorded:
        layout.descriptor_file.unlink()
    with pytest.raises(ConfigError, match=r"export\.timezone is America/New_York") as caught:
        determine(
            layout, existing if recorded else None, configured=NEW_YORK, home_assistant=BERLIN
        )
    assert caught.value.remedies[0].command == "hhe config unset export.timezone"


def test_a_recorded_descriptor_is_never_rewritten(layout: ArchiveLayout) -> None:
    recorded = ArchiveDescriptor(BERLIN, "2020-01-01T00:00:00+01:00", "ha-history-exporter 0.9")
    assert establish(layout, recorded, configured=BERLIN, home_assistant=NEW_YORK, now=NOW) == (
        recorded
    )
    assert not layout.descriptor_file.exists()
