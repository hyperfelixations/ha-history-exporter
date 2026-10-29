"""The published JSON Schema and the manifest parser agree.

Every manifest an export writes is valid under both; every structural
violation in the matrix below is rejected by both. Rules that need the
calendar (boundaries of the day, entity counts that add up) are the
parser's alone.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from ha_history_exporter.domain.formats import Format
from ha_history_exporter.domain.manifest import ManifestStatus, parse_manifest
from ha_history_exporter.errors import ExportError
from tests.support import golden
from tests.support.harness import produce
from tests.support.manifests import DAY, identity, manifest14
from tests.support.scenarios import SCENARIOS

type Document = dict[str, Any]


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    text = resources.files("ha_history_exporter").joinpath("schemas/manifest-1.4.schema.json")
    schema = json.loads(text.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def accepted_by_parser(document: Document) -> bool:
    try:
        parse_manifest(document, identity(DAY))
    except ExportError:
        return False
    return True


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(
            scenario,
            id=scenario.name,
            marks=[pytest.mark.parquet] if "parquet" in scenario.formats else [],
        )
        for scenario in SCENARIOS
    ],
)
def test_every_written_manifest_is_valid(
    validator: Draft202012Validator, scenario: Any, tmp_path: Path
) -> None:
    production = produce(scenario, tmp_path)
    manifests = [
        path
        for relative, path in golden.contract_files(production.output_dir).items()
        if relative.endswith(".manifest.json")
    ]
    assert manifests
    for path in manifests:
        errors = [error.message for error in validator.iter_errors(json.loads(path.read_bytes()))]
        assert errors == [], path.name


@pytest.mark.parametrize("status", [ManifestStatus.OK, ManifestStatus.FAILED])
def test_the_model_writes_what_the_schema_accepts(
    validator: Draft202012Validator, status: ManifestStatus
) -> None:
    manifest = manifest14(status=status, formats=(Format.JSONL, Format.PARQUET))
    assert list(validator.iter_errors(manifest.document())) == []


def _set(path: str, value: Any) -> Callable[[Document], None]:
    *parents, name = path.split(".")

    def change(document: Document) -> None:
        target = document
        for parent in parents:
            target = target[parent]
        target[name] = value

    return change


def _drop(name: str) -> Callable[[Document], None]:
    return lambda document: document.pop(name)


def _without_artifact(document: Document) -> None:
    document["artifacts"]["jsonl"] = None
    document["output_files"]["jsonl"] = None


def _with_artifact(document: Document) -> None:
    ok = manifest14(formats=(Format.CSV,)).document()
    document["artifacts"]["csv"] = ok["artifacts"]["csv"]
    document["output_files"]["csv"] = ok["output_files"]["csv"]


BATCH = {"batch_index": 1, "entity_count": 1, "error_code": "export_error", "error": "x"}
OK, FAILED = ManifestStatus.OK, ManifestStatus.FAILED

#: A valid manifest of the status, and a change that breaks it structurally.
VIOLATIONS: dict[str, tuple[ManifestStatus, Callable[[Document], None]]] = {
    "missing field": (OK, _drop("retention")),
    "unknown field": (OK, _set("comment", "x")),
    "unknown status": (OK, _set("status", "done")),
    "other schema": (OK, _set("schema_version", "1.5")),
    "other source": (OK, _set("source", "websocket")),
    "negative count": (OK, _set("state_object_count", -1)),
    "boolean count": (OK, _set("request_count", True)),
    "zero batch size": (OK, _set("batch_size_entities", 0)),
    "missing option": (OK, lambda d: d["history_request_options"].pop("no_attributes")),
    "unknown profile": (OK, _set("capture_profile.response", "partial")),
    "unknown retention": (OK, _set("retention.status", "forever")),
    "known retention without days": (OK, _set("retention.purge_keep_days", None)),
    "file name with a folder": (OK, _set("output_files.jsonl", "../2026-07-28.jsonl")),
    "upper-case digest": (OK, _set("artifacts.jsonl.sha256", "A" * 64)),
    "short digest": (OK, _set("artifacts.jsonl.logical_sha256", "b" * 63)),
    "artifact field missing": (OK, lambda d: d["artifacts"]["jsonl"].pop("row_count")),
    "ok with an error": (OK, _set("error", "boom")),
    "ok without artifacts": (OK, _without_artifact),
    "ok with a failed batch": (OK, _set("failed_batches", [BATCH])),
    "empty entity name": (OK, _set("zero_history_entities", [""])),
    "naive timestamp": (OK, _set("export_started_at", "2026-07-29T03:30:01")),
    "negative duration": (OK, _set("duration_seconds", -1)),
    "zero batch index": (FAILED, _set("failed_batches", [{**BATCH, "batch_index": 0}])),
    "failed without a code": (FAILED, _set("error_code", None)),
    "failed with an artifact": (FAILED, _with_artifact),
}


@pytest.mark.parametrize(("status", "change"), VIOLATIONS.values(), ids=VIOLATIONS.keys())
def test_both_reject_every_structural_violation(
    validator: Draft202012Validator, status: ManifestStatus, change: Callable[[Document], None]
) -> None:
    document = manifest14(status=status).document()
    assert accepted_by_parser(document)
    assert list(validator.iter_errors(document)) == []
    broken = copy.deepcopy(document)
    change(broken)
    assert not accepted_by_parser(broken)
    assert list(validator.iter_errors(broken)) != []
