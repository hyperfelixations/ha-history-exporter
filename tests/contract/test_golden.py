"""Every scenario's output against its golden directory.

A difference here means the produced files changed. Regenerate the fixtures
with ``tools/refresh_golden.py`` only for a reviewed, intended change.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from ha_history_exporter.errors import AuthError, HAConnectionError
from tests.support import golden
from tests.support.harness import Production, Scenario, produce
from tests.support.scenarios import SCENARIOS

#: How each scenario ends: the exit code, or the exception that stops the run.
OUTCOMES: dict[str, int | type[BaseException]] = {
    "reference": 0,
    "failed_day": 1,
    "unreachable": HAConnectionError,
    "auth_abort": AuthError,
    "partial_day": 0,
    "carry_in": 0,
    "midnight": 0,
}


class _Productions(dict[str, Production]):
    """Each scenario produced once, on first use; Parquet scenarios need pyarrow."""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root

    def __missing__(self, name: str) -> Production:
        scenario = next(scenario for scenario in SCENARIOS if scenario.name == name)
        self[name] = produce(scenario, self.root / name)
        return self[name]


@pytest.fixture(scope="module")
def productions(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Production]:
    return _Productions(tmp_path_factory.mktemp("scenarios"))


def _params(scenarios: tuple[Scenario, ...]) -> list[Any]:
    return [
        pytest.param(
            scenario,
            id=scenario.name,
            marks=[pytest.mark.parquet] if "parquet" in scenario.formats else [],
        )
        for scenario in scenarios
    ]


def test_every_scenario_has_an_outcome_and_a_golden_directory() -> None:
    assert set(OUTCOMES) == {scenario.name for scenario in SCENARIOS}
    for scenario in SCENARIOS:
        assert (golden.GOLDEN_ROOT / scenario.name).is_dir(), scenario.name


@pytest.mark.parametrize("scenario", _params(SCENARIOS))
def test_scenario_ends_as_specified(productions: dict[str, Production], scenario: Scenario) -> None:
    production = productions[scenario.name]
    expected = OUTCOMES[scenario.name]
    if isinstance(expected, int):
        assert production.error is None
        assert production.exit_code == expected
    else:
        assert production.error is expected


@pytest.mark.parametrize("scenario", _params(SCENARIOS))
def test_contract_files_match_the_golden_directory(
    productions: dict[str, Production], scenario: Scenario
) -> None:
    produced = golden.golden_view(productions[scenario.name].output_dir)
    expected = golden.load_golden(scenario.name)
    assert sorted(produced) == sorted(expected), "the set of produced files changed"
    for relative, data in expected.items():
        assert produced[relative] == data, f"{relative} differs from its golden fixture"


@pytest.mark.parametrize("scenario", _params(SCENARIOS))
def test_manifests_and_run_log_keep_their_serialisation(
    productions: dict[str, Production], scenario: Scenario
) -> None:
    """Two-space indentation, raw Unicode, no trailing newline, one object per line."""
    for relative, path in golden.contract_files(productions[scenario.name].output_dir).items():
        if relative.endswith(".manifest.json"):
            text = golden.text_of(path)
            assert text == json.dumps(json.loads(text), indent=2, ensure_ascii=False), relative
        elif relative == golden.RUN_LOG:
            text = golden.text_of(path)
            assert text.endswith("\n")
            for line in text.splitlines():
                assert line == json.dumps(json.loads(line), ensure_ascii=False)


@pytest.mark.parametrize("scenario", _params(SCENARIOS))
def test_manifest_physical_metadata_matches_the_produced_artifacts(
    productions: dict[str, Production], scenario: Scenario
) -> None:
    files = golden.contract_files(productions[scenario.name].output_dir)
    for relative, path in files.items():
        if not relative.endswith(".manifest.json"):
            continue
        manifest = json.loads(golden.text_of(path))
        for name, artifact in manifest["artifacts"].items():
            if artifact is None:
                continue
            artifact_path: Path = path.with_name(artifact["filename"])
            assert artifact["filename"] == f"{manifest['date']}.{name}"
            assert artifact["size_bytes"] == artifact_path.stat().st_size
            assert artifact["sha256"] == hashlib.sha256(artifact_path.read_bytes()).hexdigest()


def test_stable_manifest_neutralises_only_volatile_values() -> None:
    manifest: dict[str, Any] = {
        "export_started_at": "2026-10-27T10:00:00+01:00",
        "export_finished_at": "2026-10-27T10:00:01+01:00",
        "duration_seconds": 1.0,
        "script_version": "1.4.0",
        "state_object_count": 5,
        "artifacts": {
            "jsonl": {"size_bytes": 1, "sha256": "a" * 64, "logical_sha256": "b" * 64},
            "csv": {"size_bytes": 2, "sha256": "c" * 64, "logical_sha256": "b" * 64},
            "parquet": None,
        },
    }
    stable = json.loads(golden.stable_manifest(json.dumps(manifest)))
    assert stable["state_object_count"] == 5
    assert stable["artifacts"]["csv"] == manifest["artifacts"]["csv"]
    assert stable["artifacts"]["jsonl"]["logical_sha256"] == "b" * 64
    for name in golden.VOLATILE_MANIFEST_FIELDS:
        assert stable[name] == golden.VOLATILE


def test_each_day_is_requested_from_one_microsecond_before_its_start(
    productions: dict[str, Production],
) -> None:
    """Home Assistant returns ``start < last_updated < end``; the day is ``[start, end)``."""
    calls = productions["midnight"].history_calls
    windows = [call.window for call in calls]
    assert [(w.query_start.isoformat(), w.end_utc.isoformat()) for w in windows] == [
        ("2026-10-22T21:59:59.999999+00:00", "2026-10-23T22:00:00+00:00"),
        ("2026-10-23T21:59:59.999999+00:00", "2026-10-24T22:00:00+00:00"),
    ]
