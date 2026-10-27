from __future__ import annotations

import json
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ha_history_exporter.domain.formats import FORMAT_ORDER, Format
from ha_history_exporter.domain.manifest import (
    FIELDS_1_4,
    FailedBatch,
    LegacyManifest,
    Manifest14,
    ManifestStatus,
    Retention,
    RetentionStatus,
    decode_manifest,
    parse_manifest,
)
from ha_history_exporter.domain.query import HistoryQuery
from ha_history_exporter.errors import ExportError
from tests.support.manifests import DAY, identity, manifest14

LEGACY = Path(__file__).parents[2] / "contract" / "fixtures" / "legacy_manifests"

type Mutation = Callable[[dict[str, Any]], None]


def document(**changes: Any) -> dict[str, Any]:
    return deepcopy(manifest14(**changes).document())


def rejects(data: object, reason: str) -> None:
    with pytest.raises(
        ExportError, match=re.escape(f"The manifest for {DAY} is invalid: {reason}")
    ):
        parse_manifest(data, identity())


# ── round trips ───────────────────────────────────────────────────────────────


def test_document_lists_every_field_in_contract_order() -> None:
    assert list(manifest14().document()) == list(FIELDS_1_4)


@pytest.mark.parametrize("status", [ManifestStatus.OK, ManifestStatus.FAILED])
def test_parse_returns_what_was_encoded(status: ManifestStatus) -> None:
    manifest = manifest14(status=status, formats=FORMAT_ORDER)
    assert decode_manifest(manifest.encode(), identity()) == manifest


@given(
    st.sampled_from(list(ManifestStatus)).filter(lambda s: s is not ManifestStatus.PARTIAL),
    st.sets(st.sampled_from(FORMAT_ORDER), min_size=1),
    st.integers(1, 10**9),
    st.builds(HistoryQuery, st.booleans(), st.booleans(), st.booleans(), st.booleans()),
)
def test_parse_inverts_encode(
    status: ManifestStatus, formats: set[Format], rows: int, query: HistoryQuery
) -> None:
    manifest = replace(
        manifest14(status=status, formats=formats, state_object_count=rows), query=query
    )
    assert decode_manifest(manifest.encode(), identity()) == manifest


def test_encoding_is_two_space_indented_unicode_without_trailing_newline() -> None:
    manifest = replace(manifest14(), zero_history_entities=("sensor.küche",))
    text = manifest.encode().decode("utf-8")
    assert text == json.dumps(manifest.document(), indent=2, ensure_ascii=False)
    assert "sensor.küche" in text
    assert not text.endswith("\n")


def test_output_files_derive_from_the_artifacts() -> None:
    manifest = manifest14(formats=[Format.CSV])
    assert manifest.output_files == {
        Format.JSONL: None,
        Format.CSV: f"{DAY}.csv",
        Format.PARQUET: None,
    }


def test_encode_refuses_a_manifest_its_parser_would_reject() -> None:
    contradictory = replace(manifest14(), error="an error on a successful day")
    with pytest.raises(ExportError, match="successful manifest contains an error"):
        contradictory.encode()


# ── legacy schemata ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("schema", ["1.1", "1.2", "1.3"])
def test_legacy_manifests_are_read_without_inventing_fields(schema: str) -> None:
    raw = (LEGACY / f"schema-{schema}" / f"{DAY}.manifest.json").read_bytes()
    manifest = decode_manifest(raw, identity())
    assert isinstance(manifest, LegacyManifest)
    assert manifest.schema_version == schema
    assert manifest.status is ManifestStatus.OK
    assert manifest.output_files[Format.JSONL] == f"{DAY}.jsonl"
    assert manifest.state_object_count == 4


def test_a_legacy_manifest_must_not_carry_schema_1_4_fields() -> None:
    raw = (LEGACY / "schema-1.3" / f"{DAY}.manifest.json").read_bytes()
    data = json.loads(raw)
    data["retention"] = {"status": "unknown", "purge_keep_days": None}
    rejects(data, "unknown field is present")


def test_a_legacy_manifest_has_three_request_options() -> None:
    data = json.loads((LEGACY / "schema-1.1" / f"{DAY}.manifest.json").read_bytes())
    data["history_request_options"]["skip_initial_state"] = True
    rejects(data, "history_request_options has the wrong fields")


# ── the fail-closed contract ──────────────────────────────────────────────────


def _set(path: str, value: Any) -> Mutation:
    *parents, last = path.split(".")

    def mutate(data: dict[str, Any]) -> None:
        target = data
        for name in parents:
            target = target[name]
        target[last] = value

    return mutate


def _drop(name: str) -> Mutation:
    return lambda data: data.pop(name)


_ARTIFACT = "artifacts.jsonl"

CONTRACT_VIOLATIONS: list[tuple[Mutation, str]] = [
    (_set("schema_version", "9.9"), "schema version is not supported"),
    (_drop("error_code"), "required fields are missing"),
    (_set("future_field", 42), "unknown field is present"),
    (_set("status", "invented"), "status is not supported"),
    (_set("source", "elsewhere"), "source is not supported"),
    (_set("date", "2026-07-27"), "date does not match the target day"),
    (_set("timezone", "UTC"), "timezone does not match the export"),
    (_set("start_local", 5), "start_local must be a timestamp"),
    (_set("start_local", "noon"), "start_local must be valid ISO 8601"),
    (_set("start_local", "2026-07-28T00:00:00"), "start_local needs a timezone"),
    (_set("start_utc", "2026-07-27T23:00:00+00:00"), "start_local and start_utc disagree"),
    (_set("end_utc", "2026-07-28T23:00:00+00:00"), "end_local and end_utc disagree"),
    (
        lambda d: d.update(
            start_local="2026-07-28T00:00:00+01:00", start_utc="2026-07-27T23:00:00+00:00"
        ),
        "start boundary does not match the target day",
    ),
    (
        lambda d: d.update(
            end_local="2026-07-28T20:00:00+02:00", end_utc="2026-07-28T18:00:00+00:00"
        ),
        "end boundary does not match a complete day",
    ),
    (_set("export_started_at", "yesterday"), "export_started_at must be valid ISO 8601"),
    (_set("duration_seconds", -1), "duration_seconds must be non-negative or null"),
    (_set("duration_seconds", True), "duration_seconds must be non-negative or null"),
    (_set("state_object_count", True), "state_object_count must be a non-negative integer"),
    (_set("request_count", -1), "request_count must be a non-negative integer"),
    (_set("batch_size_entities", 0), "batch_size_entities must be greater than zero"),
    (_set("history_request_options", []), "history_request_options must be an object"),
    (
        lambda d: d["history_request_options"].pop("skip_initial_state"),
        "history_request_options has the wrong fields",
    ),
    (
        _set("history_request_options.skip_initial_state", "yes"),
        "history_request_options.skip_initial_state must be boolean",
    ),
    (_set("output_files", {"jsonl": None}), "output_files has the wrong fields"),
    (
        _set("output_files.jsonl", "../outside.jsonl"),
        "output_files must contain safe day basenames",
    ),
    (
        _set("output_files.jsonl", "2026-07-27.jsonl"),
        "output_files must contain safe day basenames",
    ),
    (_set("zero_history_entities", [""]), "zero_history_entities must be a string list"),
    (_set("failed_batches", ["x"]), "failed_batches must be an object list"),
    (_set("skipped_reason", 3), "skipped_reason must be a string or null"),
    (_set("script_version", ""), "script_version must be a non-empty string"),
    (
        _set("capture_profile.response", "minimal"),
        "capture_profile contradicts history_request_options",
    ),
    (_set("retention", {"status": "unknown"}), "retention has the wrong fields"),
    (_set("retention.status", "forever"), "retention.status is not supported"),
    (_set("retention.purge_keep_days", 0), "retention.purge_keep_days must be positive or null"),
    (_set("retention.purge_keep_days", None), "known_safe retention needs purge_keep_days"),
    (
        _set("retention", {"status": "unknown", "purge_keep_days": 14}),
        "unknown retention cannot declare purge_keep_days",
    ),
    (_set("artifacts", {"jsonl": None}), "artifacts has the wrong fields"),
    (_set(_ARTIFACT, None), "artifacts.jsonl contradicts output_files"),
    (_set("output_files.jsonl", None), "artifacts.jsonl contradicts output_files"),
    (_set(_ARTIFACT, {"filename": "x"}), "artifacts.jsonl has the wrong fields"),
    (
        _set(f"{_ARTIFACT}.filename", "2026-07-28.csv"),
        "artifacts.jsonl.filename must contain safe day basenames",
    ),
    (_set(f"{_ARTIFACT}.size_bytes", -1), "artifacts.jsonl.size_bytes must be non-negative"),
    (_set(f"{_ARTIFACT}.sha256", "A" * 64), "artifacts.jsonl.sha256 must be SHA-256"),
    (
        _set(f"{_ARTIFACT}.logical_sha256", "b" * 63),
        "artifacts.jsonl.logical_sha256 must be SHA-256",
    ),
    (_set(f"{_ARTIFACT}.row_count", 5), "artifacts.jsonl.row_count is inconsistent"),
    (_set("error", "failure"), "successful manifest contains an error"),
    (_set("entity_count_requested", 9), "entity outcome counts are inconsistent"),
    (
        _set("failed_batches", [{"batch_index": 1, "entities": [], "error": "x"}]),
        "failed_batches entry has the wrong fields",
    ),
    (
        _set(
            "failed_batches",
            [{"batch_index": 0, "entity_count": 1, "error_code": "x", "error": "y"}],
        ),
        "failed_batches entry has invalid values",
    ),
    (
        _set(
            "failed_batches",
            [{"batch_index": 1, "entity_count": 1, "error_code": "x", "error": "y"}],
        ),
        "successful manifest contains failed batches",
    ),
    (_set("error_code", 7), "error_code must be a string or null"),
]


@pytest.mark.parametrize(("mutation", "reason"), CONTRACT_VIOLATIONS)
def test_contract_violations_fail_closed(mutation: Mutation, reason: str) -> None:
    data = document()
    mutation(data)
    rejects(data, reason)


def test_artifact_digests_must_agree() -> None:
    data = document(formats=[Format.JSONL, Format.CSV])
    data["artifacts"]["csv"]["logical_sha256"] = "c" * 64
    rejects(data, "artifact logical digests differ")


def test_a_successful_manifest_needs_an_artifact() -> None:
    data = document(formats=[])
    rejects(data, "successful manifest has no output artifacts")


def test_an_empty_successful_day_needs_a_known_retention() -> None:
    manifest = replace(
        manifest14(state_object_count=0), retention=Retention(RetentionStatus.UNKNOWN, None)
    )
    rejects(manifest.document(), "empty successful manifest has unknown retention")


def test_a_failed_manifest_needs_an_error_and_publishes_nothing() -> None:
    rejects(document(status=ManifestStatus.FAILED) | {"error_code": None}, "failed manifest needs")
    data = document(status=ManifestStatus.FAILED)
    data["output_files"]["jsonl"] = f"{DAY}.jsonl"
    data["artifacts"]["jsonl"] = manifest14().document()["artifacts"]["jsonl"]
    data["artifacts"]["jsonl"]["row_count"] = 0
    rejects(data, "failed manifest cannot publish artifacts")


def test_a_partial_manifest_may_end_before_midnight() -> None:
    data = document()
    data.update(
        status="partial",
        end_local="2026-07-28T14:30:15+02:00",
        end_utc="2026-07-28T12:30:15+00:00",
    )
    data["retention"] = {"status": "partial_current", "purge_keep_days": 14}
    manifest = parse_manifest(data, identity())
    assert manifest.status is ManifestStatus.PARTIAL


def test_a_boundary_beyond_the_day_is_rejected() -> None:
    data = document(status=ManifestStatus.FAILED)
    data.update(end_local="2026-07-29T01:00:00+02:00", end_utc="2026-07-28T23:00:00+00:00")
    rejects(data, "end boundary is outside the target day")


def test_failed_batches_are_parsed() -> None:
    batch = FailedBatch(batch_index=2, entity_count=1, error_code="api", error="HTTP 500.")
    manifest = replace(manifest14(status=ManifestStatus.FAILED), failed_batches=(batch,))
    parsed = decode_manifest(manifest.encode(), identity())
    assert isinstance(parsed, Manifest14)
    assert parsed.failed_batches == (batch,)


@pytest.mark.parametrize("raw", [b"{broken", b"\xff\xfe", b"[]"])
def test_undecodable_files_fail_closed(raw: bytes) -> None:
    reason = "top level must be an object" if raw == b"[]" else "file is not valid JSON"
    with pytest.raises(ExportError, match=reason):
        decode_manifest(raw, identity())
