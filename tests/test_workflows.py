"""Contract tests for the GitHub Actions workflows.

Workflows cannot run under pytest, but their security-relevant properties can
be asserted from the YAML: no stored index token, OIDC only where it belongs,
every action pinned, and publishing gated behind a deliberate choice.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).parents[1] / ".github" / "workflows"
RELEASE = WORKFLOW_DIR / "release.yml"
TESTS = WORKFLOW_DIR / "tests.yml"

#: Secret names that would replace Trusted Publishing with a stored token.
FORBIDDEN_SECRET_PATTERNS = [
    re.compile(r"PYPI_API_TOKEN", re.IGNORECASE),
    re.compile(r"TWINE_PASSWORD", re.IGNORECASE),
    re.compile(r"TWINE_USERNAME", re.IGNORECASE),
    re.compile(r"secrets\.\w*PYPI\w*", re.IGNORECASE),
]

SHA_PIN = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def workflow_files() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml"))


def steps(job: dict) -> list[dict]:
    return job.get("steps", [])


# ── both workflows ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_workflow_is_valid_yaml(path):
    assert isinstance(load(path), dict)


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_workflow_grants_read_only_permissions_by_default(path):
    assert load(path)["permissions"] == {"contents": "read"}


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_no_workflow_stores_an_index_token(path):
    text = path.read_text(encoding="utf-8")
    for pattern in FORBIDDEN_SECRET_PATTERNS:
        assert not pattern.search(text), (
            f"{path.name} refers to {pattern.pattern}; publishing must stay on "
            "Trusted Publishing without a stored token"
        )


# ── release workflow ──────────────────────────────────────────────────────────

def test_release_runs_only_on_manual_dispatch():
    triggers = load(RELEASE)[True]
    assert set(triggers) == {"workflow_dispatch"}


def test_release_asks_for_the_publish_target_and_defaults_to_none():
    target = load(RELEASE)[True]["workflow_dispatch"]["inputs"]["publish_target"]
    assert target["default"] == "none"
    assert target["options"] == ["none", "testpypi", "pypi"]
    assert target["required"] is True


def test_publish_job_is_skipped_unless_a_target_was_chosen():
    job = load(RELEASE)["jobs"]["publish"]
    assert job["if"] == "inputs.publish_target != 'none'"


def test_publish_job_depends_on_validation_and_the_tested_build():
    job = load(RELEASE)["jobs"]["publish"]
    assert set(job["needs"]) == {"validate", "build"}


def test_only_the_publish_job_may_request_an_oidc_token():
    jobs = load(RELEASE)["jobs"]
    for name, job in jobs.items():
        permissions = job.get("permissions", {})
        has_oidc = permissions.get("id-token") == "write"
        assert has_oidc == (name == "publish"), name


def test_publish_job_runs_behind_a_github_environment():
    job = load(RELEASE)["jobs"]["publish"]
    assert job["environment"] == "${{ inputs.publish_target }}"


def test_publish_job_never_builds_its_own_distributions():
    """Building inside the publishing job is unsupported by the PyPA guidance."""
    job = load(RELEASE)["jobs"]["publish"]
    commands = " ".join(step.get("run", "") for step in steps(job))
    assert "python -m build" not in commands
    assert any(
        "download-artifact" in step.get("uses", "") for step in steps(job)
    )


def test_publish_job_uses_the_official_action():
    job = load(RELEASE)["jobs"]["publish"]
    uses = [step.get("uses", "") for step in steps(job)]
    assert any(item.startswith("pypa/gh-action-pypi-publish@") for item in uses)


def test_publish_job_verifies_the_checksums_before_uploading():
    job = load(RELEASE)["jobs"]["publish"]
    commands = " ".join(step.get("run", "") for step in steps(job))
    assert "sha256sum" in commands
    assert "WHEEL_SHA256" in commands
    assert "SDIST_SHA256" in commands


def test_release_validates_the_package_version_against_the_input():
    text = RELEASE.read_text(encoding="utf-8")
    assert "__version__" in text
    assert "package_version" in text


def test_beta_versions_must_be_pep440_normalised():
    """1.4.0-beta.1 would be rebuilt as 1.4.0b1 and no longer match."""
    text = RELEASE.read_text(encoding="utf-8")
    assert "(a|b|rc)[0-9]+" in text
    assert "beta\\.[0-9]+" not in text


# ── action pinning ────────────────────────────────────────────────────────────

def test_every_release_action_is_pinned_to_a_commit_sha():
    """A moving tag in the release path would be an unreviewed code change."""
    jobs = load(RELEASE)["jobs"]
    used = [
        step["uses"]
        for job in jobs.values()
        for step in steps(job)
        if "uses" in step
    ]
    assert used
    for reference in used:
        assert SHA_PIN.match(reference), reference
