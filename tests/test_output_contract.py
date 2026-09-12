"""The export result contract, pinned against golden fixtures.

Everything this module asserts must survive every refactoring: the directory
layout, the file names, the byte content of JSONL and CSV, the Parquet schema
and values, the manifest field set, and the run log. Home Assistant is
represented by an in-memory substitute, so the whole contract is reproducible
without a network.

The export is driven through :func:`exporter.run_export` rather than through the
command line, so that a change to the command grammar or to configuration key
names cannot require an edit here. The single point of adaptation is
:func:`tests.helpers.make_config`.

Regenerating the fixtures is a deliberate act: it means the produced files
changed. Use ``tools/refresh_golden.py`` only after that change was reviewed and
approved, and inspect the resulting diff line by line.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.exporter import run_export
from ha_history_exporter.planner import DayDecision, ExportPlan
from tests.helpers import FakeHomeAssistantClient, day_path, make_config

GOLDEN = Path(__file__).parent / "golden"
BERLIN = ZoneInfo("Europe/Berlin")

#: A normal 24-hour day and the 25-hour day of the autumn DST fallback. On
#: 2026-10-25 Europe/Berlin leaves CEST at 01:00 UTC, so rows on that day carry
#: two different offsets.
NORMAL_DAY = date(2026, 10, 24)
DST_DAY = date(2026, 10, 25)

ENTITY_IDS = [
    "sensor.kitchen_temperature",
    "sensor.status_note",
    "binary_sensor.front_door",
    "sensor.unicode_payload",
    "sensor.never_recorded",
]

#: Entities present in /api/states before optional excludes were applied.
ENTITY_COUNT_TOTAL = 7

BATCH_SIZE = 2

MANIFEST_FIELDS = {
    "schema_version",
    "status",
    "source",
    "date",
    "timezone",
    "start_local",
    "end_local",
    "start_utc",
    "end_utc",
    "export_started_at",
    "export_finished_at",
    "duration_seconds",
    "entity_count_current",
    "entity_count_requested",
    "entity_count_with_history",
    "entity_count_zero_history",
    "state_object_count",
    "batch_size_entities",
    "request_count",
    "failed_request_count",
    "retried_request_count",
    "history_request_options",
    "capture_profile",
    "retention",
    "output_files",
    "artifacts",
    "zero_history_entities",
    "failed_batches",
    "skipped_reason",
    "error",
    "error_code",
    "script_version",
}

JSONL_FIELDS = [
    "entity_id",
    "state",
    "last_changed",
    "last_updated",
    "attributes",
    "local_offset",
]

CSV_HEADER = [
    "entity_id",
    "state",
    "last_changed",
    "last_updated",
    "attributes_json",
    "local_offset",
    "extra_json",
]

#: Fields whose value depends on when the run happened, not on what it produced.
VOLATILE_MANIFEST_FIELDS = (
    "export_started_at",
    "export_finished_at",
    "duration_seconds",
    "script_version",
)
VOLATILE_RUN_LOG_FIELDS = ("started_at", "finished_at", "duration_seconds")

_TEMPERATURE_ATTRS = {
    "friendly_name": "Küche Temperatur — über dem Herd",
    "unit_of_measurement": "°C",
    "device_class": "temperature",
}
_NOTE_ATTRS = {
    "friendly_name": "Status note",
    "description": "line one\nline two, with a comma",
}
_DOOR_ATTRS = {"friendly_name": "Front door", "device_class": "door"}
_UNICODE_ATTRS = {
    "friendly_name": "Ünïcödé \U0001f321",
    "nested": {"values": [1, 2, None], "note": 'he said "yes"'},
}


def _row(
    entity_id: str,
    state: str,
    changed: str,
    attributes: dict[str, Any],
    updated: str | None = None,
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": state,
        "last_changed": changed,
        "last_updated": updated or changed,
        "attributes": attributes,
    }


#: One entry per history request, in call order: three batches of two, two, and
#: one entity, for each of the two days. An empty list is a batch in which no
#: requested entity had any recorded history.
HISTORY_RESPONSES: list[list[list[dict[str, Any]]]] = [
    # ── 2026-10-24, a normal CEST day ────────────────────────────────────────
    [
        [
            _row(
                "sensor.kitchen_temperature",
                "21.5",
                "2026-10-24T08:30:00+00:00",
                _TEMPERATURE_ATTRS,
            ),
            _row(
                "sensor.kitchen_temperature",
                "22.0",
                "2026-10-24T09:15:42.123456+00:00",
                _TEMPERATURE_ATTRS,
                updated="2026-10-24T09:15:43.500000+00:00",
            ),
        ],
        [
            _row(
                "sensor.status_note",
                'Alarm, "high" level',
                "2026-10-24T10:00:00+00:00",
                _NOTE_ATTRS,
            ),
        ],
    ],
    [
        [
            _row(
                "binary_sensor.front_door",
                "on",
                "2026-10-24T11:00:00+00:00",
                _DOOR_ATTRS,
            ),
        ],
        [
            _row(
                "sensor.unicode_payload",
                "zwölf",
                "2026-10-24T12:00:00+00:00",
                _UNICODE_ATTRS,
            ),
        ],
    ],
    [],
    # ── 2026-10-25, the 25-hour DST fallback day ─────────────────────────────
    [
        [
            # Local 01:30 CEST, still +02:00.
            _row(
                "sensor.kitchen_temperature",
                "19.5",
                "2026-10-24T23:30:00+00:00",
                _TEMPERATURE_ATTRS,
            ),
            # Local 02:30 CET, already +01:00.
            _row(
                "sensor.kitchen_temperature",
                "18.0",
                "2026-10-25T01:30:00+00:00",
                _TEMPERATURE_ATTRS,
            ),
        ],
    ],
    [
        [
            # One second before the changeover, updated two seconds after it.
            # local_offset follows the actual Recorder event time
            # (last_updated), which is already in +01:00.
            _row(
                "binary_sensor.front_door",
                "off",
                "2026-10-25T00:59:59+00:00",
                _DOOR_ATTRS,
                updated="2026-10-25T01:00:01+00:00",
            ),
        ],
        [
            # The exact changeover instant.
            _row(
                "sensor.unicode_payload",
                "unavailable",
                "2026-10-25T01:00:00+00:00",
                _UNICODE_ATTRS,
            ),
        ],
    ],
    [],
]


# ── producing the reference export ────────────────────────────────────────────

def produce(root: Path) -> Path:
    """Run the reference export into *root* and return the output directory."""
    cfg = make_config(root, jsonl=True, csv=True, parquet=True, batch_size=BATCH_SIZE)
    client = FakeHomeAssistantClient(HISTORY_RESPONSES)
    plan = ExportPlan(
        requested_start=NORMAL_DAY,
        requested_end=DST_DAY,
        today=date(2026, 10, 27),
        latest_complete=date(2026, 10, 26),
        decisions=[
            DayDecision(day=NORMAL_DAY, action="export"),
            DayDecision(day=DST_DAY, action="export"),
        ],
    )

    exit_code = run_export(
        cfg=cfg,
        plan=plan,
        entity_ids=list(ENTITY_IDS),
        entity_count_total=ENTITY_COUNT_TOTAL,
        client=client,
        tz=BERLIN,
        dry_run=False,
        sleep=lambda _seconds: None,
    )
    assert exit_code == 0
    return Path(cfg.export.output_dir)


@pytest.fixture(scope="module")
def produced(tmp_path_factory) -> Path:
    """The reference export, produced once for the whole module."""
    return produce(tmp_path_factory.mktemp("golden"))


def day_file(output: Path, day: date, suffix: str) -> Path:
    return (
        output
        / "exports"
        / "daily"
        / f"{day.year}"
        / f"{day.month:02d}"
        / f"{day}.{suffix}"
    )


# ── normalisation helpers ─────────────────────────────────────────────────────

def stable_manifest(path: Path) -> dict[str, Any]:
    """Manifest content with the run-dependent values blanked, keys kept."""
    data = json.loads(path.read_text(encoding="utf-8"))
    for field in VOLATILE_MANIFEST_FIELDS:
        assert field in data, f"manifest lost the field {field}"
        data[field] = None
    return data


def stable_run_log(path: Path) -> list[dict[str, Any]]:
    entries = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    for entry in entries:
        for field in VOLATILE_RUN_LOG_FIELDS:
            assert field in entry, f"run log lost the field {field}"
            entry[field] = None
    return entries


def parquet_profile(path: Path) -> dict[str, Any]:
    """Everything about a Parquet file that the contract fixes.

    The bytes themselves are not reproducible across pyarrow versions, so the
    schema, the row-group layout, the codec, and every value are compared
    instead.
    """
    import pyarrow.parquet as pq

    handle = pq.ParquetFile(path)
    table = handle.read()
    rows = []
    for row in table.to_pylist():
        rows.append(
            {
                key: value.isoformat() if isinstance(value, datetime) else value
                for key, value in row.items()
            }
        )
    return {
        "schema": [
            [field.name, str(field.type), field.nullable] for field in table.schema
        ],
        "num_row_groups": handle.num_row_groups,
        "num_rows": table.num_rows,
        "compression": handle.metadata.row_group(0).column(0).compression.lower(),
        "rows": rows,
    }


def read_golden_json(name: str) -> Any:
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


# ── the golden comparisons ────────────────────────────────────────────────────

@pytest.mark.parametrize("day", [NORMAL_DAY, DST_DAY], ids=["normal-day", "dst-day"])
def test_jsonl_bytes_match_the_golden_fixture(produced: Path, day: date):
    """Byte-for-byte, once the platform line terminator is normalised.

    The fixture is stored with LF. The terminator itself is a separate,
    deliberately platform-dependent contract; see the test below.
    """
    produced_bytes = day_file(produced, day, "jsonl").read_bytes()
    assert produced_bytes.replace(b"\r\n", b"\n") == (
        GOLDEN / f"{day}.jsonl"
    ).read_bytes()


def test_jsonl_uses_the_platform_line_terminator(produced: Path):
    """Pinned as it is, not as it ideally would be.

    ``JsonlWriter`` opens its file in text mode without ``newline``, so Python
    translates each ``\\n`` into ``os.linesep``: CRLF on Windows, LF elsewhere.
    Existing archives were produced on Windows and therefore use CRLF. Changing
    this would rewrite the line terminator of every future file relative to
    those archives, so the behaviour is recorded rather than corrected. See the
    internal dev doc, Ausgabedateien.
    """
    raw = day_file(produced, NORMAL_DAY, "jsonl").read_bytes()
    terminator = os.linesep.encode("ascii")
    assert raw.endswith(terminator)
    assert raw.count(terminator) == 5
    if terminator == b"\n":
        assert b"\r" not in raw


@pytest.mark.parametrize("day", [NORMAL_DAY, DST_DAY], ids=["normal-day", "dst-day"])
def test_csv_bytes_match_the_golden_fixture(produced: Path, day: date):
    """Byte-for-byte on every platform.

    ``CsvWriter`` opens with ``newline=""``, so the csv dialect writes CRLF
    itself and no platform translation happens.
    """
    produced_bytes = day_file(produced, day, "csv").read_bytes()
    assert produced_bytes == (GOLDEN / f"{day}.csv").read_bytes()


@pytest.mark.parametrize("day", [NORMAL_DAY, DST_DAY], ids=["normal-day", "dst-day"])
def test_parquet_profile_matches_the_golden_fixture(produced: Path, day: date):
    profile = parquet_profile(day_file(produced, day, "parquet"))
    assert profile == read_golden_json(f"{day}.parquet.json")


@pytest.mark.parametrize("day", [NORMAL_DAY, DST_DAY], ids=["normal-day", "dst-day"])
def test_manifest_matches_the_golden_fixture(produced: Path, day: date):
    manifest = stable_manifest(day_file(produced, day, "manifest.json"))
    assert manifest == read_golden_json(f"{day}.manifest.json")


def test_run_log_matches_the_golden_fixture(produced: Path):
    entries = stable_run_log(produced / "metadata" / "export_runs.jsonl")
    assert entries == read_golden_json("export_runs.json")


# ── the contract stated independently of the fixtures ─────────────────────────
#
# These assertions repeat parts of the golden comparison on purpose. A fixture
# that was regenerated by mistake would still pass the comparisons above; these
# name the contract in the test itself, so a rename or a reordering fails here
# as well.

def test_daily_file_paths_are_unchanged(produced: Path):
    for day in (NORMAL_DAY, DST_DAY):
        for suffix in ("jsonl", "csv", "parquet", "manifest.json"):
            expected = (
                produced / "exports" / "daily" / "2026" / "10" / f"{day}.{suffix}"
            )
            assert expected.is_file(), f"missing {expected}"
    assert (produced / "metadata" / "export_runs.jsonl").is_file()


@pytest.mark.parametrize(
    ("day", "suffix", "expected"),
    [
        (date(2026, 1, 5), "jsonl", "exports/daily/2026/01/2026-01-05.jsonl"),
        (date(2026, 9, 30), "csv", "exports/daily/2026/09/2026-09-30.csv"),
        (date(2026, 10, 24), "parquet", "exports/daily/2026/10/2026-10-24.parquet"),
        (
            date(2027, 12, 31),
            "manifest.json",
            "exports/daily/2027/12/2027-12-31.manifest.json",
        ),
    ],
)
def test_day_file_paths_are_zero_padded(tmp_path, day: date, suffix: str, expected: str):
    """Month directories are two digits, so they sort and glob correctly.

    The produced fixtures only cover October, where padding is invisible.
    """
    cfg = make_config(tmp_path)
    path = day_path(cfg, day, suffix)
    relative = path.relative_to(Path(cfg.export.output_dir)).as_posix()
    assert relative == expected


def test_manifest_field_set_matches_schema_1_4(produced: Path):
    manifest = json.loads(
        day_file(produced, NORMAL_DAY, "manifest.json").read_text(encoding="utf-8")
    )
    assert set(manifest) == MANIFEST_FIELDS
    assert manifest["schema_version"] == "1.4"
    assert manifest["source"] == "home_assistant_rest_history"
    assert manifest["status"] == "ok"
    # Read by the internal data-analysis documentation; the keys must stay.
    assert list(manifest["output_files"]) == ["jsonl", "csv", "parquet"]
    assert list(manifest["history_request_options"]) == [
        "minimal_response",
        "no_attributes",
        "significant_changes_only",
        "skip_initial_state",
    ]


def test_jsonl_row_shape_is_unchanged(produced: Path):
    lines = (
        day_file(produced, NORMAL_DAY, "jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    for line in lines:
        assert list(json.loads(line)) == JSONL_FIELDS
    first = json.loads(lines[0])
    assert first["last_changed"].endswith("+00:00")
    assert first["local_offset"] == "+02:00"


def test_csv_header_and_line_terminator_are_unchanged(produced: Path):
    raw = day_file(produced, NORMAL_DAY, "csv").read_bytes()
    header, _, rest = raw.partition(b"\r\n")
    assert header.decode("utf-8").split(",") == CSV_HEADER
    assert rest, "the CSV file holds no data rows"


def test_local_offset_follows_the_dst_change_within_one_file(produced: Path):
    rows = [
        json.loads(line)
        for line in day_file(produced, DST_DAY, "jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    offsets = {row["last_changed"]: row["local_offset"] for row in rows}
    assert offsets["2026-10-24T23:30:00+00:00"] == "+02:00"
    assert offsets["2026-10-25T00:59:59+00:00"] == "+01:00"
    assert offsets["2026-10-25T01:00:00+00:00"] == "+01:00"
    assert offsets["2026-10-25T01:30:00+00:00"] == "+01:00"


def test_local_offset_is_derived_from_last_updated(produced: Path):
    """The one row whose two timestamps sit on opposite sides of the change."""
    rows = [
        json.loads(line)
        for line in day_file(produced, DST_DAY, "jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    straddling = next(
        row for row in rows if row["entity_id"] == "binary_sensor.front_door"
    )
    assert straddling["last_changed"] == "2026-10-25T00:59:59+00:00"
    assert straddling["last_updated"] == "2026-10-25T01:00:01+00:00"
    assert straddling["local_offset"] == "+01:00"


def test_the_dst_day_spans_twenty_five_hours(produced: Path):
    manifest = json.loads(
        day_file(produced, DST_DAY, "manifest.json").read_text(encoding="utf-8")
    )
    start = datetime.fromisoformat(manifest["start_utc"])
    end = datetime.fromisoformat(manifest["end_utc"])
    assert (end - start).total_seconds() == 25 * 3600


def test_entities_without_history_are_recorded_not_failed(produced: Path):
    normal = json.loads(
        day_file(produced, NORMAL_DAY, "manifest.json").read_text(encoding="utf-8")
    )
    dst = json.loads(
        day_file(produced, DST_DAY, "manifest.json").read_text(encoding="utf-8")
    )
    assert normal["zero_history_entities"] == ["sensor.never_recorded"]
    assert dst["zero_history_entities"] == [
        "sensor.never_recorded",
        "sensor.status_note",
    ]
    assert normal["failed_batches"] == [] and dst["failed_batches"] == []
    assert normal["entity_count_current"] == ENTITY_COUNT_TOTAL
    assert normal["entity_count_requested"] == len(ENTITY_IDS)
